"""群接龙业务逻辑核心"""

import base64
import json
import os
import random
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://apipro.qunjielong.com"

CATEGORY_MAP = {
    368: "食品饮料", 369: "生鲜果蔬", 370: "家居百货", 371: "厨房日用",
    374: "服饰鞋包", 377: "珠宝饰品", 380: "保健健康", 379: "鲜花绿植",
    376: "文创礼品", 375: "收纳家居", 372: "虚拟服务", 378: "母婴亲子",
    381: "美妆护肤",
}
ALL_CATEGORY_IDS = list(CATEGORY_MAP.keys())


def make_session():
    """所有出站请求共用的 session。

    ⚠️ **刻意屏蔽环境里的 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY**（trust_env=False）。
    这个工具经常是「从某个会话里被启动」的，会继承到
    `http_proxy=http://127.0.0.1:<随机端口>` 这类**会话级代理**；会话一结束端口就失效，
    之后每个请求都会报：

        ProxyError('Unable to connect to proxy',
                   NewConnectionError("HTTPSConnection(host='127.0.0.1', port=63378):
                                      [Errno 61] Connection refused"))

    实测踩过：批量开团跑到第 8 条起连续失败，而每条之间还要干等 2 分钟。
    真要指定代理时用环境变量 `QJL_PROXY`，不再读系统/会话代理。
    """
    s = requests.Session()
    s.trust_env = False          # 忽略 *_PROXY / NO_PROXY / ~/.netrc
    s.proxies.clear()
    forced = os.environ.get("QJL_PROXY", "").strip()
    if forced:
        s.proxies.update({"http": forced, "https": forced})
    retry = Retry(total=3, backoff_factor=1.5,
                  status_forcelist=[500, 502, 503, 504],
                  allowed_methods=["GET", "POST"])
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def make_headers(config):
    return {
        "Host": "apipro.qunjielong.com",
        "Authorization": config["auth_token"],
        "appid": "wx059cd327295ab444",
        "client-version": "6.2.77",
        "companyId": "190",
        "content-type": "application/json",
        "device-type": "10",
        "feature-tag": "f0000",
        "mini-route": "pro/pages/data-analyse/data-analyse-v2-overview-page/data-analyse-v2-overview-page",
        "sceneCode": "1008",
        "uid": config["uid"],
        "version": "3.0.0",
        "charset": "utf-8",
        "Referer": "https://servicewechat.com/wx059cd327295ab444/2320/page-frame.html",
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 12; SM-F926U Build/V417IR; wv) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.189 "
            "Safari/537.36 XWEB/1500117 MMWEBSDK/20260604 MMWEBID/1213 "
            "MicroMessenger/8.0.77.3160(0x28004D30) WeChat/arm64 Weixin NetType/WIFI "
            "Language/zh_CN ABI/arm64 MiniProgramEnv/android"
        ),
    }


def _commission_val(v):
    """开团载荷里的 `commissionPercent`：**必须带真实值，绝不能是 None**。

    实测（2026-09-20）这一项原来被写死成 `None`，导致**开出来的团佣金全部是 0**：
    读回新建的团得到 commissionPercent=0 / followProfit=0，而源活动是 50 / 20。
    改成复制模板值后新建的团能正确保留（胖总家 50、🌟益能云天🌟 176 均验证通过）。
    抽查 24 家 39 条历史开团记录，佣金无一例外都是 0 —— 这是长期存在的系统性 bug。
    """
    if v is None or isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        n = _to_num(v)
        return int(n) if n == int(n) else n
    return 0


def token_info(config):
    try:
        token = config.get("auth_token", "")
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        exp_ts = decoded.get("exp", 0)
        exp_dt = datetime.fromtimestamp(exp_ts)
        remaining = (exp_dt - datetime.now()).total_seconds()
        return {
            "uid": decoded.get("uid"),
            "exp_str": exp_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "remaining_seconds": int(remaining),
            "valid": remaining > 0,
        }
    except Exception as e:
        return {"valid": False, "error": str(e), "remaining_seconds": 0}


def _to_num(v, default=0.0):
    """把接口字段安全地转成数字。

    实测这些字段的形态很不统一：
    - `earning` **恒为字符串**：'15.00'，也可能是**区间** '6.80~8.80'（取上界）
    - `followEarning` 可能是 dict，也可能是 **null**
    - `totalOrderNum` 是 int
    用统一的容错解析，避免 `int + str` 这类崩溃。
    """
    if v is None or isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return default
        vals = []
        for part in re.split(r"[~～\-—到至]", s):
            try:
                vals.append(float(part.strip()))
            except ValueError:
                pass
        if vals:
            return max(vals)
    return default


def _item_names(item):
    """一条活动的**全部**商品名（已归一化），用于「同款」判定。

    ⚠️ 以前只取第一个商品名，会漏：同一个活动被不同团长转发时 SKU 顺序可能不同，
    或者一方把配件/换购件排到了首位。
    实测（2026-09-23，一周 2260 条真实数据）：只用第一个名字能识别 452 对，
    用全部名字能识别 480 对；多出来的 28 对逐条核对**全是真同款**（同名或干净的包含），
    同时把「只用第一个名字」的 385 对全部保住，零回归。
    """
    out = []
    for g in (item.get("basicFeedGoodDTOList") or []):
        nm = _norm_name(g.get("goodsName"))
        if nm:
            out.append(nm)
    return out


def _item_goods_ids(item):
    return [g["goodsId"] for g in (item.get("basicFeedGoodDTOList") or [])
            if g.get("goodsId")]


def _item_sort_key(item):
    """同款去重时留哪条：① 单量高 ② 跟团佣金高 ③ 供货佣金高

    第 ③ 位是必要的：跨供应商同款常常「单量、跟团佣金都一样」（实测 GUU 插座就是
    两边都是 0 单 / ¥2.0），没有第三顺位就只能靠输入顺序决定，等于随机。
    """
    follow = 0.0
    supply = 0.0
    for g in (item.get("basicFeedGoodDTOList") or []):
        fe = g.get("followEarning")
        if isinstance(fe, dict):
            follow += _to_num(fe.get("maxEarning"))
        supply += _to_num(g.get("earning"))
    orders = _to_num((item.get("actStat") or {}).get("totalOrderNum"))
    return (orders, follow, supply)


def _norm_name(s):
    """商品名归一化：**只保留中英文数字**，emoji/标点/空格/零宽字符全部去掉。

    跨供应商的同款常常只是标题不同（实测）：
      「【光子抗菌技术】母婴A类❗️红豆10A满印莱卡180g套装」  （薛慢慢 / 狂奔的小绵羊）
      「红豆10A满印莱卡180g套装」                          （椰小服）
    归一化后配合 `_same_product` 的包含判断才能认出是同一款。
    """
    s = (s or "").replace("\u200b", "").replace("\u200c", "")
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", s)


# 包含匹配的最小长度：太短的名字（如「袜子」「套装」）容易被别的标题包含 → 只做精确比较
_SAME_MIN_LEN = 8


def _same_product(n1, n2):
    """两个**已归一化**的名字是否同一款：完全相同，或短的那个被长的完整包含。

    包含判定要求短名 ≥ `_SAME_MIN_LEN` 个字，避免「套装」这种通用词把一堆商品并成一条。
    实测（966 条真实数据）：精确同名能合并 12 条，加上包含判定可合并 29 条，
    多出来的 17 组人工核对过，**全是加了营销前后缀的真同款，没有误合并**。
    """
    if not n1 or not n2:
        return False
    if n1 == n2:
        return True
    short, long_ = (n1, n2) if len(n1) <= len(n2) else (n2, n1)
    return len(short) >= _SAME_MIN_LEN and short in long_


def _containment_pairs(names_of, k=_SAME_MIN_LEN):
    """找出可能互为同款的活动下标对：两边各有一个商品名共享长度 k 的连续片段。

    `names_of` 是「每条活动的全部商品名」的列表。
    用途是把 O(n²) 的包含比较降下来 —— 若短名被长名包含且短名 ≥ k，两者必然共享 k-gram，
    所以这个预筛**不会漏**（只会多）。
    """
    if not names_of:
        return set()
    idx = defaultdict(set)
    for i, names in enumerate(names_of):
        for n in names:
            if len(n) < k:
                continue
            for g in {n[p:p + k] for p in range(len(n) - k + 1)}:
                idx[g].add(i)
    pairs = set()
    for lst in idx.values():
        if len(lst) < 2:
            continue
        uniq = sorted(lst)
        for a in range(len(uniq)):
            for b in range(a + 1, len(uniq)):
                pairs.add((uniq[a], uniq[b]))
    return pairs


# 「同一款」里，匹配到的商品名至少要占少方的一半。
# 为什么需要：商家习惯把「换购 / 加赠」小件挂进每个活动，
# 实测 骄傲美食 的三个**不同主商品**活动（北极甜虾刺身 / 澳洲西冷牛排 / M9和牛）
# 都挂着同一个换购件「99秒杀福利京都板前特调鲜甜烧肉汁」，
# 不做比例限制就会被并成一条，等于把另外两个主商品藏起来（少开团）。
_NAME_OVERLAP_MIN = 0.5

# 多规格价格指纹（见 `_goods_groups` 注释）的启用条件与阈值，均由真实数据标定。
_MULTI_SKU_MIN = 2        # 至少 2 个规格
_MULTI_SKU_SIM = 0.45     # 名称集 Dice(2-gram) 阈值
_MULTI_SKU_BUCKET_MAX = 300   # 同一个「价格组合」桶超过这个规模就放弃指纹判定（防退化）

# 款号 / 货号：字母数字组合，如 XH3874、LK369、HTL4268、CG0049、D26158、10A
_CODE_RE = re.compile(r"(?:[A-Za-z]{1,5}\d{2,}|\d{2,}[A-Za-z]{1,5})")


def _item_price_key(item):
    """一条活动的「价格组合」：所有规格价格的排序元组（长度即规格数）"""
    return tuple(sorted(round(_to_num(g.get("groupBuyPrice")), 2)
                        for g in (item.get("basicFeedGoodDTOList") or [])))


def _code_tokens(names):
    """从商品名里抽出款号/货号 token（用于「款号互斥」否决）"""
    out = set()
    for n in names:
        out |= set(_CODE_RE.findall(n))
    return out


def _gram_set(names, k=2):
    """一组名字合并后的 k-gram 集合（短于 k 的名字整体作为一个 gram）"""
    out = set()
    for n in names:
        if len(n) < k:
            out.add(n)
        else:
            out.update(n[i:i + k] for i in range(len(n) - k + 1))
    return out


def _dice(g1, g2):
    if not g1 and not g2:
        return 0.0
    return 2 * len(g1 & g2) / (len(g1) + len(g2))


def _name_match(a_names, b_names):
    """两边商品名的匹配情况 → (a 中被匹配到的名字数, b 中被匹配到的名字数)"""
    ma = sum(1 for x in a_names if any(_same_product(x, y) for y in b_names))
    mb = sum(1 for y in b_names if any(_same_product(x, y) for x in a_names))
    return ma, mb


def _names_same(a_names, b_names):
    """两条活动是否靠商品名就能判定为同款：有名字互相同款，且匹配到的名字占少方 ≥ 一半。"""
    if not a_names or not b_names:
        return False
    ma, mb = _name_match(a_names, b_names)
    if not ma:
        return False
    return ma / min(len(a_names), len(b_names)) >= _NAME_OVERLAP_MIN


def _goods_groups(items):
    """把 items 按「同款」分组。三条判据，命中任一即同组：

    ① **goodsId 有交集** —— 同一个平台商品，最硬。
    ② **商品名同款**（全部规格两两比，完全相同 或 短名被长名完整包含），
       且匹配到的名字占少方 ≥ `_NAME_OVERLAP_MIN`。
    ③ **多规格价格指纹** —— 规格数相同 + 价格组合完全相同 + 名称集 Dice ≥ `_MULTI_SKU_SIM`，
       但两边都带款号且互不相交时**否决**。

    为什么需要 ②：同一款货常被不同供应商各自上架，平台给的是**不同 goodsId**，
    而且标题还会各加营销词（见 `_same_product` 的实测例子）。
    为什么需要 ③（用户 2026-09-23 报的漏判）：跨供应商同款连标题都被大改，
    既没有包含关系、goodsId 也不同，只有**规格结构**还一致 ——
        `.孟孟`  「NOUSAKU 深睡睡衣套装男款 / 女款 / 儿童款」  ¥149 / 139 / 129
        `斗不斗®`「【26秋冬-女款】NOUSAKU无感深睡睡衣长款LK-369 / 男款 / 儿童款」 同价
    归一化名字集的 Dice 只有 0.52、最长公共片段只有 "NOUSAKU"，名字判据抓不到。

    用并查集而非单一 key：一条链上可能 A≈B（同 goodsId）、B≈C（同商品名），三者也应归一组。
    """
    n = len(items)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    names_of = [_item_names(it) for it in items]

    # ── ① goodsId 交集 ──
    by_goods_id = {}
    for i, item in enumerate(items):
        for gid in _item_goods_ids(item):
            if gid in by_goods_id:
                union(i, by_goods_id[gid])
            else:
                by_goods_id[gid] = i

    # ── ② 商品名完全相同 ──
    by_name = {}
    for i, names in enumerate(names_of):
        for nm in names:
            if nm in by_name:
                union(i, by_name[nm])
            else:
                by_name[nm] = i

    # ── ② 商品名包含（先 8-gram 预筛，再按「重合占比」确认） ──
    for i, j in _containment_pairs(names_of):
        if find(i) != find(j) and _names_same(names_of[i], names_of[j]):
            union(i, j)

    # ── ③ 多规格价格指纹 ──
    # 误伤控制在真实数据上标定过（一周 2260 条）：
    #   · 只对「规格数 ≥2 且价格组合完全相同」的活动做对比 —— 单规格时价格组合就是「一个价」，
    #     一天能撞出 700 对（¥39.9 满大街），噪声太大；
    #   · 名称集 Dice(2-gram) ≥ 0.45；
    #   · 款号互斥否决：实测正是它挡住了「欧迪芬家居服货号 XH3874×5」误并到「XH3873×5」。
    # 标定结果：名称判据抓不到的候选里，≥0.45 的 14 对**全部是真同款**；
    # 最近的误伤样本在 0.33（同品牌不同商品，如 独特艾琳沐浴乳 vs 发膜）。
    buckets = defaultdict(list)
    for i, item in enumerate(items):
        gs = item.get("basicFeedGoodDTOList") or []
        if len(gs) < _MULTI_SKU_MIN:
            continue
        buckets[_item_price_key(item)].append(i)
    for bucket in buckets.values():
        if len(bucket) < 2 or len(bucket) > _MULTI_SKU_BUCKET_MAX:
            continue
        grams = {i: _gram_set(names_of[i]) for i in bucket}
        codes = {i: _code_tokens(names_of[i]) for i in bucket}
        for x in range(len(bucket)):
            for y in range(x + 1, len(bucket)):
                a, b = bucket[x], bucket[y]
                if find(a) == find(b):
                    continue
                if codes[a] and codes[b] and not (codes[a] & codes[b]):
                    continue          # 款号互斥 → 判为不同款
                if _dice(grams[a], grams[b]) >= _MULTI_SKU_SIM:
                    union(a, b)

    groups = defaultdict(list)
    for i, item in enumerate(items):
        groups[find(i)].append(item)
    return list(groups.values())


def _item_best(group):
    """一组同款里留哪条：单量 → 跟团佣金 → 供货佣金，取最大者"""
    return max(group, key=_item_sort_key)


def dedup_by_goods(items):
    """同款去重：每组只留 `_item_sort_key` 最大的那条"""
    return [_item_best(g) for g in _goods_groups(items)]


def parse_item(item):
    goods_list = item.get("basicFeedGoodDTOList", [])
    first_goods = goods_list[0] if goods_list else {}
    # earning 是字符串，可能是 '15.00'，也可能是区间 '6.80~8.80'（取上界）
    earning_val = _to_num(first_goods.get("earning"))
    follow_earning = first_goods.get("followEarning")
    max_follow = _to_num(follow_earning.get("maxEarning")) / 100 \
        if isinstance(follow_earning, dict) else 0
    price = _to_num(first_goods.get("groupBuyPrice"))
    stats = item.get("actStat", {})
    start_ts = item.get("effectTime", 0) or item.get("startTime", 0) or 0
    return {
        "actId": item.get("actId"),
        "actName": (item.get("actName", "") or "").replace("​", "").replace("‌", "").strip()[:80],
        "ghName": item.get("ghName", ""),
        "starGh": item.get("starGh", False),
        "price": price,
        "earning": earning_val,
        "followEarning": max_follow,
        # 帮卖广场会混进两类活动：
        #   activityType=150 / ghType=20|105，followEarning 有值 → 品牌供货活动，已开跟团功能，能开团
        #   activityType=10  / ghType=10，     followEarning 为 null → 团长自营团，未开跟团功能，开团必失败
        # 注意要区分 followEarning 是 null 还是 0：前者才代表"没有跟团关系"。
        "hasFollowRelation": bool(follow_earning),
        "activityType": item.get("activityType", 0),
        "commissionPct": first_goods.get("commissionPercent", ""),
        "totalOrders": stats.get("totalOrderNum", 0),
        "helpSaleCount": item.get("helpSaleCount", 0),
        "goodsName": (first_goods.get("goodsName", "") or "")[:50],
        "goodsIds": sorted({
            g["goodsId"] for g in goods_list if g.get("goodsId")
        }),
        # 规格数（这条活动里有多少个商品/SKU）。前端用它提示「多规格」，
        # 也用于诊断同款判定。
        "skuCount": len(goods_list),
        # 活动里所有商品的名称（归一化）。
        # 用途：开团成功后写进 open_log，冷却判定靠它认出「不同供应商的同款」——
        # 同一款货被别的供应商重新上架时 goodsId 会变，只有名字仍然相同。
        "goodsNames": sorted({
            _norm_name(g.get("goodsName")) for g in goods_list
            if _norm_name(g.get("goodsName"))
        }),
        "hadHelpSale": item.get("hadHelpSale", False),
        "isFollow": item.get("isFollow", False),
        # 「已跟过」= 平台记录里你已经跟过/开过这个团。
        # 这是**工具唯一能看到的「我自己开过」的证据** —— 工具只能看到帮卖广场，
        # 看不到你自己在群接龙里开的团（全库 ghId == 自己账号的活动数 = 0）。
        # 前端据此把这类行**默认不勾选**，避免同一款被反复开。
        "alreadyFollowed": bool(item.get("isFollow")),
        "actPic": item.get("actPic", ""),
        "startTime": start_ts,
        "startTimeStr": datetime.fromtimestamp(start_ts / 1000).strftime("%m-%d") if start_ts else "",
        "upStreamType": item.get("upStreamType", 0),
    }


def _fetch_one_page(session, headers, gh_id, page, category_ids, upstream_types,
                    start_ts, end_ts, keyword=None):
    """拉单页 feed list，返回 (page, items, has_next) 或抛异常。

    keyword : 顶层参数，服务端搜索。实测传供应商名可精确命中该供应商的全部活动
              （如 "狂奔的小绵羊" → 10/10 全属该家）；传部分词会跨供应商模糊匹配。

    ⚠️ **刻意不发 `firstCategoryIdList`**（参数 `category_ids` 保留仅为兼容调用方）。
    实测这一条会砍掉大半数据（2026-09-12：传我们那 13 个分类 → 99 条；
    完全不传 → 256 条，**漏 61%**）。而且它并不是严格的"子集过滤"：
    不传时多出 150 条，传时另多出 6 条（像是服务端在带分类时插了推荐位）。
    平台的一级分类不止我们硬编码的那 13 个，所以这个过滤既不全也不纯。
    客户端另有按关键词的「分类」视图筛选（CAT_KEYWORDS），不依赖它。
    """
    for attempt in range(3):
        try:
            body = {
                "page": page, "sortType": 3, "pageSize": 10,
                "ghId": gh_id, "foldSameAct": True,
                "firstCategoryIdList": [],
                "pageParam": "",
                "filterFeedDTO": {
                    "upStreamTypeList": upstream_types,
                    "starTime": start_ts,
                    "endTime": end_ts,
                },
            }
            if keyword:
                body["keyword"] = keyword
            resp = session.post(
                f"{BASE_URL}/help-sale-square/v6/relation_help_sale_feed/list",
                json=body,
                headers=headers, timeout=15,
            )
            data = resp.json()
            if data.get("code") != 200:
                raise ValueError(f"API错误: {data.get('msg', data)}")
            feed = data["data"]
            return page, feed.get("groupHelpSaleDTOs", []), feed.get("hasNextPage", False)
        except Exception as e:
            if attempt < 2:
                time.sleep(1.5 ** attempt)
            else:
                raise


def _fetch_details_batch(session, headers, gh_id, act_ids):
    """批量拉详情，返回 detail list。

    ⚠️ 这个接口是「**整批同生共死**」的：批里只要有一个无效 / 已删除的 actId，
    整批都返回 0 条（实测 `[有效]` → 1 条，`[有效, 无效]` → 0 条）。
    拉广场时 actId 都来自列表接口，所以不会踩到；但「手动标记已开过」这类
    用户可能粘贴历史 actId 的场景会踩，所以整批失败时**逐个重试一次**把有效的救回来。
    """
    ids = [a for a in (act_ids or []) if a]
    if not ids:
        return []
    out = _post_detail_batch(session, headers, gh_id, ids)
    if not out and len(ids) > 1:
        for one in ids:
            out.extend(_post_detail_batch(session, headers, gh_id, [one]))
    return out


def _post_detail_batch(session, headers, gh_id, act_ids):
    """发一次批量详情请求（带重试）"""
    for attempt in range(3):
        try:
            resp = session.post(
                f"{BASE_URL}/help-sale-square/feed_base/feed_base_search/query_list_by_act_ids",
                json={
                    "actIds": act_ids, "ghId": gh_id,
                    "returnDataList": [10, 20, 40, 50, 80],
                    "filterParam": json.dumps({
                        "filterSensitive": False, "filterFollowSupplyAct": False,
                        "filterSamePact": False, "filterBlackGh": False,
                        "filterOffShelvesAct": False, "filterStartGroupAct": True,
                    }),
                    "extraParam": json.dumps({"from": 6}),
                },
                headers=headers, timeout=15,
            )
            data = resp.json()
            return data.get("data", []) if data.get("code") == 200 else []
        except Exception:
            if attempt < 2:
                time.sleep(1.5 ** attempt)
            else:
                return []


def _fetch_window_pages(session, headers, gh_id, category_ids, upstream_types,
                        start_ts, end_ts, q, label, batch=5, keyword=None):
    """拉一个时间窗口的全部分页，返回 items 列表"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    q.put({"type": "page", "page": f"{label} 第1页", "total": 0})
    try:
        _, first_items, has_next = _fetch_one_page(
            session, headers, gh_id, 1, category_ids, upstream_types, start_ts, end_ts,
            keyword=keyword)
    except Exception as e:
        q.put({"type": "error", "msg": f"{label} 首页请求失败: {e}"})
        return []

    items = list(first_items)
    if not first_items:
        return items
    q.put({"type": "progress", "page": label, "added": len(first_items), "total": len(items)})
    if not has_next:
        return items

    page_num = 2
    while True:
        pages = list(range(page_num, page_num + batch))
        futures = {}
        with ThreadPoolExecutor(max_workers=batch) as ex:
            for p in pages:
                futures[ex.submit(_fetch_one_page, make_session(), headers, gh_id, p,
                                  category_ids, upstream_types, start_ts, end_ts,
                                  keyword)] = p
            batch_has_next = False
            for fut in as_completed(futures):
                try:
                    _, page_items, hn = fut.result()
                    items.extend(page_items)
                    if hn:
                        batch_has_next = True
                except Exception as e:
                    q.put({"type": "error", "msg": f"{label} Page 请求失败: {e}"})
        q.put({"type": "progress", "page": f"{label} ~{pages[-1]}",
               "added": len(items), "total": len(items)})
        if not batch_has_next:
            break
        page_num += batch
    return items


def fetch_activities(config, q, category_ids=None, upstream_types=None, days_range=7,
                     log_entries=None, dates=None, include_followed=True, keywords=None):
    """并发拉取帮卖活动，通过队列 q 推送进度事件。

    dates : 可选，["YYYY-MM-DD", ...]，按"具体某几天"拉取（每天一个窗口）。
            给了 dates 就忽略 days_range。
    include_followed : 默认 True —— 「我已跟过」的活动**保留**（category='followed'），
            复团要用它（实测已跟过的源活动仍可再开一次团）。设 False 可主动排除。
    keywords : 可选，["供应商名", ...]，服务端搜索。给了就每个关键词 × 每个窗口
            各拉一次再合并（用于「只复团某几家供应商」）。实测按供应商名可精确命中。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    headers = make_headers(config)
    gh_id = config["gh_id"]
    category_ids = category_ids or ALL_CATEGORY_IDS
    upstream_types = upstream_types or [2, 3]
    keywords = [str(k).strip() for k in (keywords or []) if str(k).strip()]

    now = datetime.now()
    windows = []
    if dates:
        for ds in dates:
            try:
                d0 = datetime.strptime(str(ds).strip(), "%Y-%m-%d")
            except (ValueError, TypeError):
                continue
            d1 = d0 + timedelta(days=1)
            windows.append((int(d0.timestamp() * 1000), int(d1.timestamp() * 1000),
                            d0.strftime("%m-%d")))

    if not windows:
        start_ts = int((now - timedelta(days=days_range)).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        end_ts = int((now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        windows = [(start_ts, end_ts, f"近{days_range}天")]

    # Step 1+2: 逐窗口拉全部分页（有 keywords 时每个关键词再各拉一遍）
    session = make_session()
    raw_items = []
    for st, en, label in windows:
        if keywords:
            for kw in keywords:
                raw_items.extend(_fetch_window_pages(
                    session, headers, gh_id, category_ids, upstream_types, st, en,
                    q, f"{label}·{kw}", keyword=kw))
        else:
            raw_items.extend(_fetch_window_pages(
                session, headers, gh_id, category_ids, upstream_types, st, en, q, label))

    # 跨窗口去重 actId
    seen_ids = set()
    ordered_act_ids = []
    for item in raw_items:
        aid = item.get("actId")
        if aid and aid not in seen_ids:
            seen_ids.add(aid)
            ordered_act_ids.append(aid)

    if not ordered_act_ids:
        _finish_fetch(q, [], log_entries, include_followed)
        return

    total_acts = len(ordered_act_ids)
    q.put({"type": "page", "page": "详情", "total": total_acts})

    # Step 3: 并发拉详情（每批20个 actId，5并发）
    DETAIL_BATCH = 20
    DETAIL_WORKERS = 5
    all_items = []
    batches = [ordered_act_ids[i:i+DETAIL_BATCH]
               for i in range(0, len(ordered_act_ids), DETAIL_BATCH)]

    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as ex:
        futs = {ex.submit(_fetch_details_batch, make_session(), headers, gh_id, b): b
                for b in batches}
        done_count = 0
        for fut in as_completed(futs):
            details = fut.result()
            all_items.extend(details)
            done_count += len(futs[fut])
            q.put({"type": "progress", "page": "详情",
                   "added": len(details), "total": done_count,
                   "grand_total": total_acts})

    _finish_fetch(q, all_items, log_entries, include_followed)


REOPEN_COOLDOWN_DAYS = 5


def _finish_fetch(q, all_items, log_entries=None, include_followed=True):
    """把拉到的原始活动分桶 + **跨桶合并同款** + 标注「5 天内开过同款」。

    分桶：
    - new      : hadHelpSale=False 且 isFollow=False  → 全新活动
    - followed : hadHelpSale=False 且 isFollow=True   → 我已跟过（可再开一次，实测通过）
    - reopen   : hadHelpSale=True                     → 被帮卖过（成交 0 单也保留）

    丢弃规则**只剩一条**：被帮卖过（hadHelpSale=True）且 5 天内我自己开过
    （actId / goodsId / 商品名 任一命中 open_log）。

    「5 天内开过同款」对 new / followed **不丢弃**，而是标 `recentlyOpened=True`：
    列表里仍可见（灰标签标注），但**开团范围与「可开团」筛选都会排除它**，
    满 5 天后自动恢复可开 —— 这样既能防止重复开团，又不丢信息。
    """
    now = datetime.now()

    # 成功开团记录 → 三张表：actId / goodsId / 归一化商品名。
    # actId 会因同款最优者的变化而漂移；同一款货被不同供应商重新上架时 goodsId 也会不同，
    # 所以商品名必须一起记，否则「不同供应商的同款」认不出来。
    last_opened = {}         # actId -> datetime
    last_opened_goods = {}   # goodsId -> datetime
    last_opened_names = {}   # 归一化商品名 -> datetime
    # 名字匹配是 O(已开名字数) 的包含比较，只收「冷却窗口附近」的记录即可（规则本身只关心 5 天），
    # 这样日志涨到几千条也不会拖慢拉取。
    name_cutoff = now - timedelta(days=REOPEN_COOLDOWN_DAYS + 9)
    for entry in (log_entries or []):
        if not entry.get("actId") or entry.get("status") != "ok":
            continue
        try:
            t = datetime.strptime(entry["time"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        aid = entry["actId"]
        if aid not in last_opened or t > last_opened[aid]:
            last_opened[aid] = t
        for gid in (entry.get("goodsIds") or []):
            if gid not in last_opened_goods or t > last_opened_goods[gid]:
                last_opened_goods[gid] = t
        if t >= name_cutoff:
            for nm in (entry.get("goodsNames") or []):
                nm = _norm_name(nm)
                if nm and (nm not in last_opened_names or t > last_opened_names[nm]):
                    last_opened_names[nm] = t

    def _name_hit(nm):
        """这个商品名是否与我开过的某个商品名同款（精确 或 一方包含另一方）。

        跨供应商的同款标题常常不同（「红豆10A满印莱卡180g套装」 vs
        「【光子抗菌技术】母婴A类❗️红豆10A满印莱卡180g套装」），所以不能只比相等。
        """
        if not nm:
            return None
        hit = last_opened_names.get(nm)
        for ln, lt in last_opened_names.items():
            if ln == nm:
                continue
            # 先比时间：只有当这条记录更晚、且可能同款时才做包含判定（省掉大量比较）
            if (hit is None or lt > hit) and _same_product(nm, ln):
                hit = lt
        return hit

    # 同款组里**被合并掉**那些来源的 goodsId / 商品名。
    # 冷却判定必须把它们也算进来：同款去重后会留「数据最好的那条」，
    # 而它未必是我上次开过的那条（两边 goodsId 和标题都可能不同），
    # 只看留下的那条会得出「没开过」→ 5 天内重复开团。
    merged_extra = {}        # actId(留下的那条) -> (extra_goods_ids, extra_names)

    def last_opened_at(item):
        """该活动最近一次被我开团的时间：actId / goodsId / 商品名 三种命中的最晚者"""
        stamps = []
        aid = item.get("actId")
        if aid in last_opened:
            stamps.append(last_opened[aid])
        extra_ids, extra_names = merged_extra.get(aid, ((), ()))
        for gid in list(_item_goods_ids(item)) + list(extra_ids):
            if gid in last_opened_goods:
                stamps.append(last_opened_goods[gid])
        for nm in list(_item_names(item)) + list(extra_names):
            t = _name_hit(nm)
            if t:
                stamps.append(t)
        return max(stamps) if stamps else None

    # split: new / already-followed / re-openable
    new_raw = [p for p in all_items if not p.get("hadHelpSale") and not p.get("isFollow")]
    # isFollow=True 且 hadHelpSale=False = "我已经跟过这个团"。
    # 复团恰恰要它 —— 实测已跟过的源活动可以再开一次。默认**不丢弃**；
    # include_followed=False 时才排除（可选的主动排除开关）。
    followed_raw = []
    if include_followed:
        followed_raw = [p for p in all_items
                        if not p.get("hadHelpSale") and p.get("isFollow")]
    # hadHelpSale=True = 被帮卖过。**唯一还生效的丢弃规则**：5 天内我自己开过。
    reopen_raw = []
    for p in all_items:
        if not p.get("hadHelpSale"):
            continue
        lo = last_opened_at(p)
        if lo and (now - lo).days < REOPEN_COOLDOWN_DAYS:
            continue
        reopen_raw.append(p)

    # ── 跨分桶合并同款 ──
    # 三个桶各自去重会漏掉「同一款同时在 新活动 和 已跟过 里」的情况，
    # 表现为同一个商品被开两次（用户报过）。这里对三个桶合起来做一次并查集，
    # 被合并掉的来源记进 altSources，前端会显示「同款另有 N 个来源」，不丢信息。
    tagged = ([(it, "new") for it in new_raw]
              + [(it, "followed") for it in followed_raw]
              + [(it, "reopen") for it in reopen_raw])
    cat_of = {id(it): c for it, c in tagged}
    buckets = {"new": [], "followed": [], "reopen": []}
    alts_of = {}
    for grp in _goods_groups([it for it, _ in tagged]):
        best = _item_best(grp)
        buckets[cat_of[id(best)]].append(best)
        others = [x for x in grp if x is not best]
        if others:
            alts_of[best.get("actId")] = [(x.get("ghName") or "", cat_of[id(x)])
                                          for x in others]
            # 把被合并来源的 goodsId / 商品名也挂到留下的那条上，供冷却判定使用
            merged_extra[best.get("actId")] = (
                [gid for x in others for gid in _item_goods_ids(x)],
                [nm for x in others for nm in _item_names(x)],
            )

    # best totalOrders per goodsName across ALL marketplace items
    goods_best = {}  # goodsName -> (totalOrders, actId)
    for item in all_items:
        gn = (item.get("basicFeedGoodDTOList") or [{}])[0].get("goodsName", "") or ""
        gn = gn[:50]
        orders = item.get("actStat", {}).get("totalOrderNum", 0)
        if gn and (gn not in goods_best or orders > goods_best[gn][0]):
            goods_best[gn] = (orders, item.get("actId"))

    def annotate_and_sort(items, category):
        result = []
        for item in items:
            p = parse_item(item)
            p["category"] = category
            gn = p.get("goodsName", "")
            best_orders, best_id = goods_best.get(gn, (0, None))
            if best_id and best_id != p["actId"] and best_orders > p["totalOrders"]:
                p["bestActId"] = best_id
                p["bestOrders"] = best_orders
            else:
                p["bestActId"] = None
                p["bestOrders"] = None
            # 距我上次开这款商品的天数（actId / goodsId / 商品名 取最晚一次）
            lo = last_opened_at(item)
            d = (now - lo).days if lo else None
            p["daysSinceOpen"] = d
            p["recentlyOpened"] = d is not None and d < REOPEN_COOLDOWN_DAYS
            if item.get("actId") in alts_of:
                p["altSources"] = [{"ghName": g, "category": c}
                                   for g, c in alts_of[item["actId"]]]
            result.append(p)
        return sorted(result, key=lambda x: x.get("startTime", 0), reverse=True)

    new_items = annotate_and_sort(buckets["new"], "new")
    followed_items = annotate_and_sort(buckets["followed"], "followed")
    reopen_items = annotate_and_sort(buckets["reopen"], "reopen")

    # 分桶必须能加减对上
    dropped = len(all_items) - len(new_raw) - len(reopen_raw) - len(followed_raw)
    raw_tagged = len(new_raw) + len(reopen_raw) + len(followed_raw)
    total_available = len(new_items) + len(followed_items) + len(reopen_items)
    deduped = raw_tagged - total_available
    all_out = new_items + followed_items + reopen_items
    cooldown_count = sum(1 for a in all_out if a.get("recentlyOpened"))
    assert len(all_items) - dropped == raw_tagged
    assert total_available == raw_tagged - deduped

    q.put({
        "type": "done",
        "total_raw": len(all_items),
        "dropped": dropped,
        "new_raw": len(new_raw),
        "reopen_raw": len(reopen_raw),
        "followed_raw": len(followed_raw),
        "deduped": deduped,
        "new_count": len(new_items),
        "reopen_count": len(reopen_items),
        "followed_count": len(followed_items),
        "cooldown_count": cooldown_count,
        "cooldown_days": REOPEN_COOLDOWN_DAYS,
        "total_available": total_available,
        "items": all_out,
    })


def build_release_payload(template, parent_act_id, gh_id):
    act_dto = template["activityDTO"]
    act_info = template["activityInfoDTO"]
    act_settings = template["activitySettingsDTO"]
    act_goods = template["activityGoodsList"]
    act_logistics = template["actLogisticsInfos"]
    svc_guarantee = template["servicesGuarantee"]

    act_goods_params = []
    for goods in act_goods:
        sku_list = goods.get("mutiActGoodsInfoDTO", {})
        sku_params = [
            {
                "goodsId": sku["goodsId"],
                "startActGoodsId": sku["startActGoodsId"],
                "goodsPicList": sku.get("goodsPicList", []),
                "groupBuyPrice": sku["groupBuyPrice"],
                "originalPrice": sku.get("originalPrice", 0),
                "costPrice": sku["costPrice"],
                # ⚠️ 必须复制模板值，写 None 会让开出来的团佣金变成 0（历史 bug）
                "commissionPercent": _commission_val(sku.get("commissionPercent")),
                "totalStock": sku["totalStock"],
                "useStock": sku["useStock"],
                "goodsTotalStock": sku["goodsTotalStock"],
                "goodsUseStock": sku["goodsUseStock"],
                "goodsDimensionInfoList": [
                    {
                        "goodsDimensionId": d["goodsDimensionId"],
                        "goodsDimensionValueId": d["goodsDimensionValueId"],
                        "goodsDimensionFrontId": d.get("actGoodsDimensionId", 0),
                        "goodsDimensionValueFrontId": d.get("actGoodsDimensionValueId", 0),
                    }
                    for d in sku.get("goodsDimensionInfoList", [])
                ],
                "goodsWeight": sku.get("goodsWeight", 0),
                "supplyPrice": sku.get("supplyPrice", 0),
                "actGoodsStatus": sku.get("actGoodsStatus", 10),
                "goodsCost": sku.get("goodsCost", 0),
                "leftStock": sku.get("leftStock", 0),
                "followCommissionPercent": sku.get("followCommissionPercent", 0),
                "followHsCommission": sku.get("followHsCommission", 0),
                "followActCostPrice": sku.get("followActCostPrice", 0),
                "followProfit": sku.get("followProfit", 0),
                "useStockCount": sku.get("useStockCount", 0),
                "minLeftStock": sku.get("minLeftStock", 0),
                "goodsSupplyPrice": sku.get("goodsSupplyPrice", 0),
                "evaluationPrice": 0,
            }
            for sku in sku_list.get("actGoodsSkuPropertyDTOS", [])
        ]
        dim_list = [
            {
                "goodsDimensionId": d["goodsDimensionId"],
                "dimensionName": d["dimensionName"],
                "dimensionValueList": [
                    {
                        "goodsDimensionValueId": dv["goodsDimensionValueId"],
                        "valueName": dv["valueName"],
                        "dimensionValueDeleteFlag": dv.get("dimensionValueDeleteFlag", 0),
                        "goodsDimensionValueFrontId": dv.get("actGoodsDimensionValueId", 0),
                    }
                    for dv in d.get("dimensionValueList", [])
                ],
                "dimensionDeleteFlag": d.get("dimensionDeleteFlag", 0),
                "goodsDimensionFrontId": d.get("actGoodsDimensionId", 0),
            }
            for d in sku_list.get("dimensionList", [])
        ]
        act_goods_params.append({
            "actGoodsIntro": goods.get("actGoodsIntro", ""),
            "actGoodsName": goods.get("actGoodsName", ""),
            "actGoodsPicList": goods.get("actGoodsPicList", []),
            "actGoodsUnit": goods.get("actGoodsUnit", ""),
            "payLimitCount": goods.get("payLimitCount", 0),
            "payLimitModel": goods.get("payLimitModel", 10),
            "goodsCategoryId": goods.get("goodsCategoryId", 0),
            "goodsId": goods["goodsId"],
            "actGoodsStatus": goods.get("actGoodsStatus", 10),
            "startActGoodsId": goods.get("startActGoodsId", 0),
            "showGoodsAllOrder": goods.get("showGoodsAllOrder", 5),
            "showGoodsGroupNum": goods.get("showGoodsGroupNum", 5),
            "actGroupBuyPrice": goods.get("actGroupBuyPrice", 0),
            "totalStock": goods.get("totalStock", 0),
            "actGoodsExtStatus": goods.get("actGoodsExtStatus", 10),
            "priceLimitStart": goods.get("priceLimitStart", 10),
            "priceLimitEnd": goods.get("priceLimitEnd", 0),
            "dimensionModel": goods.get("dimensionModel", 10),
            "mutiActGoodsInfoParam": {"actGoodsSkuParam": sku_params, "dimensionList": dim_list},
            "evaluationQuota": None,
            "notDiscount": goods.get("notDiscount", 10),
        })

    return {
        "actDiscountParam": {"discountItemParams": []},
        "actGhGroupIdList": [],
        "actSettingsParam": {
            "richTextShowMode": act_settings.get("richTextShowMode", 20),
            "goodsSelectLimitCount": act_settings.get("goodsSelectLimitCount", 0),
            "goodsSelectLimitModel": act_settings.get("goodsSelectLimitModel", 10),
            "activityTpiLimitCount": act_settings.get("activityTpiLimitCount", 0),
            "activityTpiLimitModel": act_settings.get("activityTpiLimitModel", 10),
            "tpiLeastAmount": act_settings.get("tpiLeastAmount", 0),
            "tpiLeastAmountModel": act_settings.get("tpiLeastAmountModel", 10),
            "tpiUserPrivacyType": act_settings.get("tpiUserPrivacyType", 60),
            "tpiImportantTips": act_settings.get("tpiImportantTips", ""),
            "contractSelect": act_settings.get("contractSelect", 10),
            "phoneSelect": act_settings.get("phoneSelect", 10),
            "addressSelect": act_settings.get("addressSelect", 10),
            "redPacketRecord": act_settings.get("redPacketRecord", 5),
            "shardRewardRecord": act_settings.get("shardRewardRecord", 5),
            "allowSignIn": act_settings.get("allowSignIn", 5),
            "actRedemptionSettingId": act_settings.get("actRedemptionSettingId", 0),
            "titleEditModel": act_settings.get("titleEditModel", 20),
            "synchronizedModel": act_settings.get("synchronizedModel", 10),
            "allowGroupSelectGoods": act_settings.get("allowGroupSelectGoods", 5),
            "allowCancelOrder": act_settings.get("allowCancelOrder", 5),
            "parentSettlingModel": act_settings.get("parentSettlingModel", 40),
            "groupImportantTips": act_settings.get("groupImportantTips", ""),
            "transmitModel": act_settings.get("transmitModel", 10),
            "payJoinModel": act_settings.get("payJoinModel", 10),
            "carriageTemplateId": act_settings.get("carriageTemplateId", 0),
            "lowerDeliverySw": act_settings.get("lowerDeliverySw", 5),
            "showGoodsWeightToCustomer": act_settings.get("showGoodsWeightToCustomer", 5),
            "carriageCollectGhId": act_settings.get("carriageCollectGhId", ""),
            "materialIds": act_settings.get("materialIds", []),
            "allowFollow": act_settings.get("allowFollow", 5),
            "featureVersion": act_settings.get("featureVersion", 5),
            "syncRootTypes": act_settings.get("syncChildTypes", act_settings.get("syncRootTypes", {})),
            "actRecommendTypes": act_settings.get("actRecommendTypes", []),
            "showGoodsStockToCustomer": act_settings.get("showGoodsStockToCustomer", 5),
            "onlyFirstOrderRequireFeeSwitch": act_settings.get("onlyFirstOrderRequireFeeSwitch", False),
            "exchangeSwitch": None,
            "syncRootContextTypes": [10, 20, 30],
            "promoterSw": 5,
            "gactSw": 5,
            "groupSalerSelectModel": 10,
            "syncRestartSw": 10,
            "returnAddrType": 0,
            "recommendActIds": [],
            "childExchangeSwitch": 5,
        },
        "activityAttrList": [],
        "activityInfoParam": {
            "activityDetail": act_info.get("activityDetail", ""),
            "bgImgId": act_info.get("bgImgId", 0),
        },
        "addActParam": {
            "ghViewType": act_dto.get("ghViewType", 10),
            "activityName": act_dto["activityName"],
            "activityStatus": 10,
            "startTime": act_dto["startTime"],
            "endTime": act_dto["endTime"],
            "ghId": gh_id,
            "activityModel": act_dto.get("activityModel", 20),
            "isDoubtSubmit": False,
            "isCertificationSubmit": False,
            "logisticsModel": act_dto.get("logisticsModel", 10),
            "displayType": act_dto.get("displayType", 10),
            "copyScene": 10,
            "startActId": parent_act_id,
        },
        "actGoodsParams": act_goods_params,
        "isSelectAllGroup": 1,
        "sourceInfo": {"tag": "", "data": "", "infos": ""},
        "isDoubtSubmit": False,
        "isCertificationSubmit": False,
        "actLogisticsInfos": [
            {
                "logisticsModel": li.get("logisticsModel", 10),
                "actLogisticsAttrs": [
                    {"attrType": a["attrType"], "attrName": a["attrName"]}
                    for a in li.get("actLogisticsAttrs", [])
                ],
            }
            for li in act_logistics
        ],
        "servicesGuarantee": {
            "guaranteeStatus": svc_guarantee.get("guaranteeStatus", 10),
            "id": svc_guarantee.get("id", 0),
            "deliveryModel": svc_guarantee.get("deliveryModel", 10),
            "deliveryLogistics": svc_guarantee.get("deliveryLogistics", ""),
            "deliveryPlace": svc_guarantee.get("deliveryPlace", ""),
            "customerServiceRemark": svc_guarantee.get("customerServiceRemark", ""),
            "timelinessModel": svc_guarantee.get("timelinessModel", 10),
            "deliveryDeadline": svc_guarantee.get("deliveryDeadline", 3),
            "allowEditCsRemarkSw": svc_guarantee.get("allowEditCsRemarkSw", 10),
        },
        "startSelectFollowSettings": {
            "followSelectMode": 0, "backwardOption": True, "followGhIdList": [],
        },
        "labelIdList": [],
    }


def open_one_group(config, parent_act_id, dry_run=False):
    """开单个团。dry_run=True 只拉模板不提交。返回 (ok, new_act_id, act_name, error)"""
    session = make_session()
    headers = make_headers(config)
    gh_id = config["gh_id"]

    url = f"{BASE_URL}/activity-biz/help_sale_root/act/for_follow/{parent_act_id}/{gh_id}"
    resp = session.get(url, headers=headers, timeout=15)
    data = resp.json()
    if data.get("code") != 200:
        return False, None, "", f"获取模板失败: {data.get('msg', data)}"

    template = data["data"]
    act_name = template["activityDTO"]["activityName"]

    if dry_run:
        return True, None, act_name, ""

    payload = build_release_payload(template, parent_act_id, gh_id)
    result = session.post(
        f"{BASE_URL}/activity-biz/help_sale_root/act/release",
        json=payload, headers=headers, timeout=30,
    ).json()

    if result.get("code") != 200:
        return False, None, act_name, f"开团失败: {result.get('msg', result)}"

    new_act_id = result["data"]["actId"]

    try:
        session.post(
            f"{BASE_URL}/activity-biz/try_order/act/release",
            json={"ghGroupId": gh_id, "pactId": new_act_id, "sysGeneratedType": 90},
            headers=headers, timeout=15,
        )
    except Exception:
        pass

    return True, new_act_id, act_name, ""


def batch_open_groups(config, act_ids, q, dry_run=False, delay=1.0, jitter=0.0,
                      should_stop=None):
    """批量开团，通过队列 q 推送进度。

    delay  : 两条之间的基准等待秒数
    jitter : 在 delay 基础上 ±jitter 秒随机浮动
    should_stop : 可选回调，返回 True 则中断（含等待期间）
    """
    total = len(act_ids)
    success = 0
    fail = 0

    def stopped():
        return bool(should_stop and should_stop())

    for i, act_id in enumerate(act_ids):
        if stopped():
            q.put({"type": "aborted", "index": i + 1, "total": total})
            break
        try:
            ok, new_id, act_name, err = open_one_group(config, act_id, dry_run)
            entry = {
                "type": "item",
                "index": i + 1,
                "total": total,
                "actId": act_id,
                "actName": act_name[:50],
                "ok": ok,
                "newActId": new_id,
                "error": err,
                "dryRun": dry_run,
            }
            if ok:
                success += 1
            else:
                fail += 1
            q.put(entry)
        except Exception as e:
            fail += 1
            q.put({
                "type": "item", "index": i + 1, "total": total,
                "actId": act_id, "actName": "", "ok": False,
                "newActId": None, "error": str(e), "dryRun": dry_run,
            })

        if i < total - 1:
            wait = delay + (random.uniform(-jitter, jitter) if jitter > 0 else 0.0)
            wait = max(0.0, wait)
            q.put({"type": "wait", "index": i + 1, "next_index": i + 2,
                   "seconds": round(wait, 1)})
            deadline = time.time() + wait
            while True:
                if stopped():
                    break
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                time.sleep(min(0.25, remaining))

    q.put({"type": "done", "success": success, "fail": fail, "total": total})
