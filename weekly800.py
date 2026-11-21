#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每周六出「800 单以上」两份开团清单（跟团 / 帮卖）。

流程：
  1) 按生效时间逐月扫帮卖广场（2019-01 至今），捞全部候选 actId
  2) 逐条查详情（**必须单查**，批量接口会串号）
  3) 门槛：订单 ≥ MIN_ORDERS、在售未暂停、未结束、activityType 分跟团/帮卖
  4) 去重（core.dedup_by_goods，规则同历史报表）
  5) 季节过滤（冬季剔除夏款）
  6) 近期有出单：与上一期快照比对订单数，零变化剔除
  7) 分 7 等分（周一~周日），写 2 个 xlsx

用法：
  python3 weekly800.py                 # 全流程
  python3 weekly800.py --rescan        # 忽略池缓存重新扫广场
  python3 weekly800.py --min 800       # 改门槛
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import core
import classify_cat

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
OUT_DIR = os.path.join(ROOT, "周报")
MIN_ORDERS = 800
SNAP_PREFIX = "weekly800_snap_"
RECHECK_DAYS = 30          # 低销老团最迟这么久必须复核一次，避免悄悄涨到 800 的团永远漏掉

# ---------------------------------------------------------------- 账号类型
GH_TYPE_LABEL = {10: "品牌号(自营)", 20: "选品团长", 105: "供货品牌"}


def kind_label(it):
    gh_type = it.get("ghType") or 0
    base = GH_TYPE_LABEL.get(gh_type, f"ghType{gh_type}")
    if it.get("starGh"):
        return "明星团长"
    return base


# ---------------------------------------------------------------- 季节过滤
# 先看冬款白名单（命中就保留，避免「加绒短袖」被误杀），再看夏款黑名单。
WINTER_KEEP = [
    "加绒", "加厚", "保暖", "羽绒", "羊毛", "羊绒", "抓绒", "摇粒绒", "德绒",
    "秋裤", "保暖内衣", "棉拖", "围巾", "手套", "毛线", "毛衣", "棉服", "羽绒服",
    "大衣", "雪地靴", "法兰绒", "珊瑚绒", "暖风机", "取暖器", "暖宝宝", "热水袋",
    "棉鞋", "雪地", "加长加厚", "秋冬", "冬季", "过冬",
    "毛绒", "护膝", "打底", "暖宫", "艾草", "热敷", "厚袜",
]

SUMMER_DROP = [
    # 纳凉 / 驱蚊
    "扇子", "蒲扇", "团扇", "折扇", "风扇", "空调扇", "冷风扇", "循环扇",
    "凉席", "冰丝席", "竹席", "藤席", "凉被", "夏凉被", "空调被", "凉枕",
    "蚊帐", "蚊香", "驱蚊", "花露水", "风油精", "清凉油", "痱子粉", "爽身粉",
    "防晒衣", "防晒霜", "防晒喷雾", "防晒帽", "遮阳", "太阳镜", "墨镜", "草帽",
    "冰袖", "冰凉", "冰感", "凉感", "降温贴", "退热贴", "冰垫", "制冰", "冰格",
    # 鞋
    "凉鞋", "沙滩鞋", "人字拖", "拖鞋", "洞洞鞋", "溯溪鞋",
    # 服饰
    "无袖", "吊带", "背心", "短袖", "短裤", "短裙", "半身裙", "连衣裙",
    "雪纺", "T恤", "t恤", "露肩", "一字肩", "沙滩裤", "泳衣", "泳装",
    "游泳", "比基尼", "冰丝", "睡裙",
    # 时令 / 场景
    "西瓜", "荔枝", "芒果", "水蜜桃", "冷饮", "冰淇淋", "雪糕", "冰镇",
    "水枪", "泡泡机", "游泳圈", "泳池",
]

# 夏季（5-9 月）反过来剔冬款
WINTER_DROP = [
    "加绒", "加厚保暖", "羽绒", "羊毛", "羊绒", "抓绒", "摇粒绒", "德绒",
    "秋裤", "保暖内衣", "棉拖", "围巾", "手套", "毛衣", "棉服", "羽绒服",
    "大衣", "雪地靴", "法兰绒", "珊瑚绒", "暖风机", "取暖器", "暖宝宝",
    "热水袋", "棉鞋", "加厚被", "棉被", "蚕丝被",
]

# 一年四季都卖的，命中夏款词也保留
SEASON_NEUTRAL = [
    "洗衣", "清洁", "纸", "抽纸", "洗发", "沐浴", "牙膏", "牙刷", "垃圾袋",
    "保鲜", "拖鞋架", "数据线", "耳机", "充电",
    "丝袜", "内裤", "袜子", "浇水", "太阳能", "摆件", "装饰", "花盆",
    # 四季通用，别被上面的夏款词误杀（2026-11-14 实测误杀）
    "眼镜夹", "制冰", "风油精", "清凉油",
]


def season_verdict(name, month=None):
    """返回 (是否剔除, 命中词)。冬季剔夏款、夏季剔冬款，其它季节不过滤。"""
    month = month or datetime.now().month
    low = name or ""
    if month in (11, 12, 1, 2):
        drop_words = SUMMER_DROP
    elif month in (5, 6, 7, 8, 9):
        drop_words = WINTER_DROP
    else:
        return False, ""
    if drop_words is SUMMER_DROP:
        for w in WINTER_KEEP:
            if w in low:
                return False, f"冬款({w})"
    for w in SEASON_NEUTRAL:
        if w in low:
            return False, ""
    for w in drop_words:
        if w in low:
            return True, w
    return False, ""


# ---------------------------------------------------------------- 1. 扫广场
class DummyQ:
    def put(self, *a, **k):
        pass


def _page(session, headers, gh_id, page, st, en, size=100):
    """单页（pageSize 放大到 100，比 core 的 10 快 10 倍）"""
    for attempt in range(3):
        try:
            body = {
                "page": page, "sortType": 3, "pageSize": size,
                "ghId": gh_id, "foldSameAct": True,
                "firstCategoryIdList": [], "pageParam": "",
                "filterFeedDTO": {"upStreamTypeList": [2, 3],
                                  "starTime": st, "endTime": en},
            }
            r = session.post(f"{core.BASE_URL}/help-sale-square/v6/relation_help_sale_feed/list",
                             json=body, headers=headers, timeout=20)
            d = r.json()
            if d.get("code") != 200:
                raise ValueError(d.get("msg"))
            feed = d.get("data") or {}
            return feed.get("groupHelpSaleDTOs") or [], bool(feed.get("hasNextPage"))
        except Exception:
            if attempt < 2:
                time.sleep(1.5 ** attempt)
            else:
                return [], False


def fetch_window(session, headers, gh_id, st, en, workers=8):
    items, _ = _page(session, headers, gh_id, 1, st, en)
    if not items:
        return items
    page = 2
    while True:
        batch = list(range(page, page + workers))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_page, core.make_session(), headers, gh_id, p, st, en)
                    for p in batch]
            has_next = False
            for f in as_completed(futs):
                got, hn = f.result()
                items.extend(got)
                if hn:
                    has_next = True
        if not has_next:
            break
        page += workers
    return items


def scan_pool(cfg, use_cache=True, rescan_all=False):
    path = os.path.join(DATA, "weekly800_pool.json")
    if use_cache and os.path.exists(path) and not rescan_all:
        age = time.time() - os.path.getmtime(path)
        if age < 12 * 3600:
            with open(path) as f:
                d = json.load(f)
            print(f"[pool] 复用缓存 {len(d)} 个 actId（{age/3600:.1f} 小时前）")
            return d

    headers = core.make_headers(cfg)
    q = DummyQ()
    session = core.make_session()
    seen, ordered = set(), []

    # 扫哪些号的广场：**两个汤团号都要扫**。
    # 帮卖广场是「按号看关系」的——同一个 token、不同 ghId，看到的供货商不一样。
    # config.json 那个号（汤团²）用于拉数据，但「洋葱 / 小洋葱」这类只对开团号（汤团）
    # 可见，只扫一个号就会整家漏掉（2026-11-20 实测：洋葱 51 条 actId 一条都没进池）。
    accounts = [cfg["gh_id"]]
    try:
        acc = json.load(open(os.path.join(DATA, "open_account.json"), encoding="utf-8"))
        other = (acc.get("ghId") or "").strip()
        if other and other not in accounts:
            accounts.append(other)
    except Exception:
        pass
    if len(accounts) > 1:
        print(f"[pool] 扫 {len(accounts)} 个号的广场: {', '.join(a[:10] + '…' for a in accounts)}")

    # 增量扫描：若历史池已存在，载入已有 actId，只扫描最近 60 天的新活动
    if os.path.exists(path) and not rescan_all:
        try:
            with open(path) as f:
                old_list = json.load(f)
            for aid in old_list:
                if aid and aid not in seen:
                    seen.add(aid)
                    ordered.append(aid)
            print(f"[pool] 继承已有池 {len(ordered)} 条，增量扫描近 2 个月广场...")
            d = (datetime.now() - timedelta(days=60)).replace(day=1)
        except Exception:
            d = datetime(2019, 1, 1)
    else:
        d = datetime(2019, 1, 1)

    end = datetime.now()
    for gh_id in accounts:
        cur = d
        while cur <= end:
            nxt = (cur + timedelta(days=32)).replace(day=1)
            st = int(cur.timestamp() * 1000)
            en = int(nxt.timestamp() * 1000)
            label = cur.strftime("%Y-%m")
            try:
                items = fetch_window(session, headers, gh_id, st, en)
            except Exception as e:
                print(f"  {gh_id[:8]} {label} 失败: {e}")
                items = []
            added = 0
            for it in items:
                aid = it.get("actId")
                if aid and aid not in seen:
                    seen.add(aid)
                    ordered.append(aid)
                    added += 1
            if added:
                print(f"  {gh_id[:8]} {label}: +{added} (累计 {len(ordered)})", flush=True)
            cur = nxt
    with open(path, "w") as f:
        json.dump(ordered, f)
    print(f"[pool] 共 {len(ordered)} 个 actId")
    return ordered


def collect_seeds():
    """历史已知的 actId 全部当种子。

    为什么必须有这一步：广场 feed 的时间过滤挡不住「长期挂着的活动」——
    实测 2209130672317527（买东买西，effectTime=2022-09-14）在 2022-09 窗口
    和 2026-11 窗口都查不到，但单查详情仍然 5/10 在售、订单还在涨。
    只靠广场扫描，这类老团每周都会「凭空消失」。
    """
    seeds = set()

    def add(v):
        try:
            i = int(v)
        except (TypeError, ValueError):
            return
        if i > 0:
            seeds.add(i)

    def load_json(p):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            return None

    # 1) 活动缓存（前端抓过的）
    for name in ("activities.json", "open_log.json"):
        d = load_json(os.path.join(DATA, name))
        if isinstance(d, list):
            for it in d:
                if isinstance(it, dict):
                    add(it.get("actId"))
                else:
                    add(it)
    # 2) 短链缓存（历史报表全部链接）
    d = load_json(os.path.join(DATA, "short_link_cache.json"))
    if isinstance(d, dict):
        for k in d:
            add(k)
    # 3) 历史快照
    for fn in os.listdir(DATA):
        if fn.startswith(SNAP_PREFIX) and fn.endswith(".json"):
            d = load_json(os.path.join(DATA, fn))
            if isinstance(d, dict):
                for k in d:
                    add(k)
    # 4) 历史周报 / 旧报表
    import openpyxl
    l2a = {}
    d = load_json(os.path.join(DATA, "short_link_cache.json")) or {}
    for aid, link in d.items():
        l2a[link] = aid
    dirs = [ROOT, OUT_DIR]
    for folder in dirs:
        if not os.path.isdir(folder):
            continue
        for fn in os.listdir(folder):
            if not fn.endswith(".xlsx") or "汤团_" not in fn:
                continue
            try:
                wb = openpyxl.load_workbook(os.path.join(folder, fn), data_only=True)
                ws = wb[wb.sheetnames[0]]
            except Exception:
                continue
            head = [c.value for c in ws[1]]
            if "活动ID" in head:
                ai = head.index("活动ID")
                for row in ws.iter_rows(min_row=2, values_only=True):
                    if row[ai]:
                        add(row[ai])
                continue
            if "可点击链接" not in head:
                continue
            ci = head.index("可点击链接")
            for row in ws.iter_rows(min_row=2, values_only=True):
                add(l2a.get(row[ci]))
    return seeds


# ---------------------------------------------------------------- 2. 逐条详情
def fetch_one(cfg, session, headers, act_id):
    """取单个活动详情。

    服务端按 extraParam.from 做入口级可见性过滤：from=6 会漏掉一批长期团
    （家乡美/屿憩/淘天下等），必须用 8 兜底。先 8，再 6/9/11。
    """
    for fromv in (8, 6, 9, 11):
        try:
            out = core._post_detail_batch(session, headers, cfg["gh_id"],
                                          [int(act_id)], fromv=fromv)
        except Exception:
            out = []
        if out:
            return out[0]
    return None


def fetch_details(cfg, act_ids, workers=12, force_all=False):
    """逐条查详情。
    
    智能增量策略：
      1. 当天内重复运行：命中同日缓存，直接秒级复用。
      2. 跨天/跨周运行（增量）：
         - 历史不存在/已下架（None）：判定为失效死团，跳过单查。
         - 订单 < 400 且创建超过 90 天：历史极低销老团（1.8万+条），不可能突然冲上800单门槛，跳过单查并保留缓存。
         - 订单 >= 400 或创建 90 天内的新团、以及未见过的全新 actId：作为候选与近期活跃团，并发单查，更新最新订单数与在售状态！
      3. 如需强制全量查 23000+ 条，可传入 force_all=True。
    """
    cache_path = os.path.join(DATA, "weekly800_detail_cache.json")
    today = datetime.now().strftime("%Y%m%d")
    now_ms = time.time() * 1000
    cache = {}
    last_date = None
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                blob = json.load(f)
            last_date = blob.get("date")
            cache = blob.get("data") or {}
        except Exception:
            cache = {}

    is_same_day = (last_date == today) and not force_all
    todo = []
    skipped_ancient = 0
    skipped_not_found = 0

    for a in act_ids:
        k = str(a)
        if k not in cache:
            todo.append(a)
            continue
        if is_same_day:
            continue
        if force_all:
            todo.append(a)
            continue

        item = cache[k]
        if not item:
            skipped_not_found += 1
            continue

        orders = item.get("orders", 0)
        # flatten 存的是 startTime（生效时间），不是 effectTime —— 早先读错字段，
        # 导致 eff_ms 恒为 0、age_days 恒为天文数字，等于「订单<400 的历史条目永久不再查」。
        eff_ms = item.get("startTime") or item.get("effectTime") or 0
        age_days = (now_ms - eff_ms) / (86400 * 1000) if eff_ms else 9999
        checked_ms = item.get("checkedAt") or 0
        checked_age = (now_ms - checked_ms) / (86400 * 1000)

        # 90 天以上老团且订单低于 400：极冷死团，不必每周都查。
        # 但「不常查」≠「永不查」—— 30 天必须复核一次，否则悄悄涨到 800 的团永远进不了表。
        if orders < 400 and age_days > 90 and checked_age < RECHECK_DAYS:
            skipped_ancient += 1
            continue

        todo.append(a)

    if is_same_day:
        print(f"[detail] 命中同日缓存 {len(act_ids)-len(todo)} / 待查 {len(todo)}")
    else:
        print(f"[detail] 智能增量待查 {len(todo)} / 共 {len(act_ids)} "
              f"(跳过历史低销老团 {skipped_ancient}，跳过失效团 {skipped_not_found})")

    headers = core.make_headers(cfg)
    done = 0
    t0 = time.time()
    if todo:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(fetch_one, cfg, core.make_session(), headers, a): a for a in todo}
            for fut in as_completed(futs):
                a = futs[fut]
                det = fut.result()
                item = flatten(det) if det else None
                if item:
                    item["checkedAt"] = int(now_ms)      # 记一下「上次查过」，冻结规则靠它
                cache[str(a)] = item
                done += 1
                if done % 200 == 0 or done == len(todo):
                    el = time.time() - t0
                    print(f"  详情 {done}/{len(todo)}  {el:.0f}s  剩约 {el/max(done,1)*(len(todo)-done)/60:.1f} 分钟",
                          flush=True)
                    with open(cache_path, "w") as f:
                        json.dump({"date": today, "data": cache}, f, ensure_ascii=False)
        with open(cache_path, "w") as f:
            json.dump({"date": today, "data": cache}, f, ensure_ascii=False)
    return cache


# ---------------------------------------------------------------- 3. 归一化
def flatten(det):
    """详情 → 报表用的一行原始数据"""
    goods = det.get("basicFeedGoodDTOList") or []
    names = []
    for g in goods:
        nm = (g.get("goodsName") or "").replace("\u200b", "").replace("\u200c", "").strip()
        if nm:
            names.append(nm)
    top = max(goods, key=lambda g: core._to_num(g.get("groupBuyPrice"))) if goods else {}
    start_ts = det.get("effectTime") or 0
    return {
        "actId": det.get("actId"),
        "actName": (det.get("actName") or "").replace("\u200b", "").replace("\u200c", "").strip(),
        "goodsName": (top.get("goodsName") or "").replace("\u200b", "").replace("\u200c", "").strip()[:80],
        "goodsNames": names,
        "goodsIds": sorted({g.get("goodsId") for g in goods if g.get("goodsId")}),
        "ghName": det.get("ghName") or "",
        "ghType": det.get("ghType") or 0,
        "starGh": bool(det.get("starGh")),
        "activityType": det.get("activityType") or 0,
        "pauseSw": det.get("pauseSw"),
        "activityStatus": det.get("activityStatus"),
        "orders": (det.get("actStat") or {}).get("totalOrderNum") or 0,
        "startActId": det.get("startActId") or 0,
        "startTime": start_ts,
        "startDate": datetime.fromtimestamp(start_ts / 1000).strftime("%Y-%m-%d") if start_ts else "",
        "price": core._to_num(top.get("groupBuyPrice")),
        "actPic": det.get("actPic") or "",
        # 保留原始规格结构：core 的同款判定按 basicFeedGoodDTOList 走，
        # 只留扁平字段会退化成「只比标题」，本周实测漏判「营养通用土」两条。
        "goods": [{
            "goodsId": g.get("goodsId"),
            "goodsName": g.get("goodsName") or "",
            "groupBuyPrice": g.get("groupBuyPrice"),
            "earning": g.get("earning"),
            "followEarning": g.get("followEarning"),
            "goodsPicList": g.get("goodsPicList") or [],
        } for g in goods],
    }


# ---------------------------------------------------------------- 4. 上期快照
def load_prev_snapshot():
    """找最近一期的快照（不含今天的），返回 {actId: orders}

    基线要**至少隔 20 小时**：一天里重跑两次的话，拿几小时前的快照当基线，
    大部分链接「订单没变」会被当成冷团剔掉——这不是数据变了，是间隔太短。
    太新的快照直接跳过，往更早的找。
    """
    cands = sorted(f for f in os.listdir(DATA)
                   if f.startswith(SNAP_PREFIX) and f.endswith(".json"))
    today = datetime.now().strftime("%Y%m%d")
    cands = [c for c in cands if c != f"{SNAP_PREFIX}{today}.json"]
    if not cands:
        return {}, None
    pick = None
    now_ts = time.time()
    for c in reversed(cands):
        p = os.path.join(DATA, c)
        try:
            age_sec = now_ts - os.path.getmtime(p)
        except OSError:
            continue
        if age_sec >= 20 * 3600:
            pick = c
            break
    if pick is None:                     # 全都太新：宁可不筛冷团，也别整表清空
        return {}, None
    path = os.path.join(DATA, pick)
    with open(path) as f:
        d = json.load(f)
    return {str(k): v.get("orders", 0) for k, v in d.items()}, pick


def prev_from_xlsx():
    """没有快照时，从上一期 xlsx 反查（用短链缓存把链接换回 actId）。
    跟团 + 帮卖两份都要读，否则一份会被当成「全部新上榜」。"""
    import openpyxl
    link2act = {}
    p0 = os.path.join(ROOT, "data", "short_link_cache.json")
    if os.path.exists(p0):
        with open(p0) as f:
            for aid, link in json.load(f).items():
                link2act[link] = str(aid)
    out = {}
    files = sorted(f for f in os.listdir(ROOT)
                   if f.startswith("汤团_千单以上") and f.endswith(".xlsx"))
    for fn in files:
        try:
            wb = openpyxl.load_workbook(os.path.join(ROOT, fn), data_only=True)
        except Exception:
            continue
        ws = wb[wb.sheetnames[0]]
        head = [c.value for c in ws[1]]
        try:
            ci_link = head.index("可点击链接")
            ci_ord = head.index("订单数量")
        except ValueError:
            continue
        for row in ws.iter_rows(min_row=2, values_only=True):
            aid = link2act.get(row[ci_link])
            if aid:
                out[aid] = row[ci_ord] or 0
    return out


# ---------------------------------------------------------------- 5. 去重
def dedup(rows):
    """复用 core.dedup_by_goods，入参需要 core.dedup 认识的字段"""
    items = []
    for r in rows:
        goods = r.get("goods")
        if not goods:                      # 老缓存没有规格结构时退化为 id/名字集合
            goods = [{"goodsId": g} for g in r["goodsIds"]] + \
                    [{"goodsName": n} for n in r["goodsNames"]]
        items.append({
            "actId": r["actId"],
            "actName": r["actName"],
            "actPic": r.get("actPic") or "",
            "startActId": r["startActId"],
            "actStat": {"totalOrderNum": r["orders"]},
            "basicFeedGoodDTOList": goods,
            "_row": r,
        })
    kept = core.dedup_by_goods(items)
    return [k["_row"] if isinstance(k, dict) and "_row" in k else k for k in kept]


# ---------------------------------------------------------------- 6. 分 7 组
CAT_ORDER = [1, 2, 3, 4]


def _interleave(items):
    """日内排序：四类轮转 + 相邻两条尽量不同供货商。

    上一版是把同一家塞在同一天方便开团，反而让每天的货长得一样。
    现在要的是「打散」：一天里吃了的、用的、穿的、杂的轮流出现，
    且相邻两个团不来自同一家。
    """
    from collections import deque
    buckets = {c: deque() for c in CAT_ORDER}
    for r in sorted(items, key=lambda x: -x["orders"]):
        buckets.setdefault(r.get("cat", 4), deque()).append(r)
    out, last_sup, i = [], None, 0
    while any(buckets.values()):
        c = CAT_ORDER[i % 4]
        i += 1
        dq = buckets.get(c)
        if not dq:
            continue
        k = next((j for j in range(min(6, len(dq))) if dq[j]["ghName"] != last_sup), 0)
        picked = dq[k]
        dq.remove(picked)
        out.append(picked)
        last_sup = picked["ghName"]
    return out


def load_override():
    """人工指定：actId -> (周几, 分钟)。优先于所有自动记忆。

    有些链接平台侧查不到上次开团记录（手动开的、或记录被淹没），
    只有你知道它上次是周几。`data/sched_override.json` 里写
    {"活动ID": 周几, ...}（0=周一 … 6=周日），或 {"活动ID": [周几, 分钟]}。
    """
    if not os.path.exists(OVERRIDE_PATH):
        return {}
    try:
        blob = json.load(open(OVERRIDE_PATH))
    except Exception:
        return {}
    out = {}
    for k, v in (blob or {}).items():
        if isinstance(v, (list, tuple)) and v:
            out[str(k)] = (int(v[0]), int(v[1]) if len(v) > 1 else 12 * 60)
        elif isinstance(v, (int, float)):
            out[str(k)] = (int(v), 12 * 60)
        elif isinstance(v, str) and v.strip().isdigit():
            out[str(k)] = (int(v.strip()), 12 * 60)
    return out


def load_prev_schedule():
    """上一期排期记忆：actId -> (周几 0-6, 当天第几分钟)。

    为什么要记：同一批链接如果每周换着日子开，两次开团间隔可能只有 2~4 天，
    平台会判「同款短期内重复开团」直接屏蔽。让它们尽量回到**上周同一天、
    同一个时段**，间隔就稳稳落在 7 天左右，既安全又保曝光节奏。

    只读最近一份「比今天早」的周报；没有就返回空（退回原来的打散逻辑）。
    """
    today = datetime.now().strftime("%Y%m%d")
    cand = []
    try:
        for fn in os.listdir(OUT_DIR):
            if not fn.endswith(".xlsx") or not fn.startswith("汤团_"):
                continue
            m = re.search(r"_(\d{8})\.xlsx$", fn)
            if m and m.group(1) < today:
                cand.append((m.group(1), fn))
    except Exception:
        return {}
    if not cand:
        return {}
    cand.sort()
    newest = cand[-1][0]
    # 同一期的「跟团 / 帮卖」两份表一起读——只看一份会让另一份的表全成了「没记忆」
    files = [fn for day, fn in cand if day == newest]
    wd = {name: i for i, name in enumerate(WEEK_LABEL)}
    out = {}
    try:
        import openpyxl
        for fn in files:
            wb = openpyxl.load_workbook(os.path.join(OUT_DIR, fn), read_only=True)
            for ws in wb.worksheets:
                rows = ws.iter_rows(values_only=True)
                head = list(next(rows, ()) or ())
                if "活动ID" not in head or "计划开团时间" not in head:
                    continue
                ai, di, ti = head.index("活动ID"), head.index("开团日"), head.index("计划开团时间")
                for row in rows:
                    if not row or not row[ai]:
                        continue
                    w = wd.get(str(row[di] or "").strip())
                    t = str(row[ti] or "").strip()
                    if w is None or " " not in t:
                        continue
                    try:
                        hh, mm = t.split(" ")[-1].split(":")
                        out[str(row[ai])] = (w, int(hh) * 60 + int(mm))
                    except Exception:
                        continue
    except Exception:
        return {}
    print(f"[prev_sched] 上周排期记忆 {len(out)} 条（{newest}，{'、'.join(files)}）")
    return out


def load_actual_plan(fresh_days=21):
    """上周「真正照着开」的那份复团表：actId -> (周几, 当天第几分钟)。

    为什么单独一份：那份表是人排的、当天实际执行的，但它往往**只有链接、
    没有活动ID**。只按活动ID记记忆时，这一整批会被判成「没有上周记录」，
    再被贪心算法重新打散到别的日子——用户核对的就是这一批。
    这里用短链缓存把「可点击链接」反查成活动ID，跟新一期对齐。
    优先级排在第一期周报之上：它才是真正开过的一版。
    """
    import openpyxl
    link2act = {}
    try:
        with open(os.path.join(DATA, "short_link_cache.json")) as f:
            for aid, link in json.load(f).items():
                link2act[str(link)] = str(aid)
    except Exception:
        pass
    if not link2act:
        return {}
    dirs = [ROOT, OUT_DIR, os.path.expanduser("~/Desktop"), os.path.expanduser("~/Downloads")]
    cand = []
    for folder in dirs:
        if not os.path.isdir(folder):
            continue
        for fn in os.listdir(folder):
            if not fn.endswith(".xlsx") or "复团" not in fn or fn.startswith("~$"):
                continue
            path = os.path.join(folder, fn)
            try:
                age = (datetime.now() - datetime.fromtimestamp(os.path.getmtime(path))).days
            except Exception:
                continue
            if age <= fresh_days:
                cand.append((os.path.getmtime(path), fn, path))
    if not cand:
        return {}
    cand.sort()
    _mt, fname, path = cand[-1]
    wd = {name: i for i, name in enumerate(WEEK_LABEL)}
    out = {}
    try:
        wb = openpyxl.load_workbook(path, data_only=True)
        for ws in wb.worksheets:
            rows = ws.iter_rows(values_only=True)
            head = [str(c or "") for c in (next(rows, ()) or ())]
            if "可点击链接" not in head or "开团日" not in head:
                continue
            ai = head.index("活动ID") if "活动ID" in head else None
            ci, di = head.index("可点击链接"), head.index("开团日")
            ti = head.index("计划开团时间") if "计划开团时间" in head else None
            for row in rows:
                if not row or ci >= len(row) or not row[ci]:
                    continue
                aid = str(row[ai]) if ai is not None and row[ai] else link2act.get(str(row[ci]).strip())
                w = wd.get(str(row[di] or "").strip())
                if not aid or w is None:
                    continue
                minute = None
                if ti is not None and row[ti]:
                    t = str(row[ti]).strip()
                    try:
                        hh, mm = t.split(" ")[-1].split(":")
                        minute = int(hh) * 60 + int(mm)
                    except Exception:
                        minute = None
                out[aid] = (w, minute if minute is not None else 12 * 60)
    except Exception as e:
        print(f"[actual_plan] 读取失败：{e}")
        return {}
    print(f"[actual_plan] 上周实际执行表记忆 {len(out)} 条（{fname}，{len(link2act)} 条短链反查）")
    return out


def load_plan_memory(dates=None):
    """现有执行计划里已经定好的日期：actId -> (周几, 分钟)。

    同一期反复重跑（改文案、补数据、重出表）时，没有「上周记忆」的链接
    （新上榜的）不该每次都被贪心算法挪到别的日子——沿用上一版排期即可。
    """
    if not dates or not os.path.exists(PLAN_PATH):
        return {}
    try:
        blob = json.load(open(PLAN_PATH))
    except Exception:
        return {}
    wd_index = {d.weekday(): i for i, d in enumerate(dates)}
    out = {}
    for x in blob.get("items") or []:
        try:
            d = datetime.fromisoformat(str(x.get("date")))
        except Exception:
            continue
        if d.weekday() not in wd_index:
            continue
        out[str(x.get("actId"))] = (d.weekday(), int(x.get("slotMin") or 0))
    return out


def load_follow_memory():
    """跟团表上一次排在哪天：actId -> (周几, 分钟)。

    跟团不走自动开团、没有计划文件，重跑生成器时如果没有这份记忆，
    新上榜的链接每次都会被贪心算法挪到别的日子。存一份就稳了。
    """
    if not os.path.exists(FOLLOW_MEM_PATH):
        return {}
    try:
        blob = json.load(open(FOLLOW_MEM_PATH))
    except Exception:
        return {}
    return {str(k): (int(v[0]), int(v[1])) for k, v in blob.items() if v}


def load_openlog_memory(days=14):
    """开团日志里最近一次成功开团：actId -> (周几, 分钟)。

    只补「上一期表里没有、但确实开过」的链接，让它们回到上次那个周几，
    两次间隔自然落在 7 天左右，不会被平台判 5 天内重复开团。
    """
    if not os.path.exists(LOG_PATH):
        return {}
    try:
        rows = json.load(open(LOG_PATH))
    except Exception:
        return {}
    cut = datetime.now() - timedelta(days=days)
    best = {}
    for r in rows:
        if not isinstance(r, dict) or r.get("status") != "ok":
            continue
        try:
            t = datetime.strptime(str(r.get("time"))[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if t < cut:
            continue
        k = str(r.get("actId"))
        if k not in best or t > best[k]:
            best[k] = t
    return {k: (t.weekday(), t.hour * 60 + t.minute) for k, t in best.items()}


def split_seven(rows, dates=None, prev=None, hard=None):
    """7 天分块：条数均分 + 四类每天均匀 + 供应商打散 + **尽量回到上周同一天**。

    做法分三轮：
      1a) 上一期表里就在「周三」的，先按周三排回去（当天还有空位才放）；
      1b) 上一期表里没有、但计划/开团日志里有日期的，也按那个周几放回去；
      2)  剩下的再贪心——优先给「这一类目前最少」且「这家目前最少」的那天。
    这样既保持每天 食品/日用/服饰美妆/杂货 混着来，又不会把同一批链接
    每周打散到不同日子、导致 5 天内重复开团被平台屏蔽。
    """
    from collections import Counter
    prev = prev or {}
    hard = hard if hard is not None else prev
    n = len(rows)
    base, rem = divmod(n, 7)
    target = [base + (1 if j < rem else 0) for j in range(7)]

    days = [[] for _ in range(7)]
    day_cat = [Counter() for _ in range(7)]
    day_sup = [Counter() for _ in range(7)]
    sup_total = Counter(r["ghName"] for r in rows)

    def put(r, j):
        days[j].append(r)
        day_cat[j][r.get("cat", 4)] += 1
        day_sup[j][r["ghName"]] += 1

    # 第一轮：有记忆的按原周几放回去（上一期表 > 计划/开团日志，前者先占位）
    pinned = 0
    if dates and prev:
        wd_index = {d.weekday(): i for i, d in enumerate(dates)}
        tier1 = [r for r in rows if str(r["actId"]) in hard]
        tier2 = [r for r in rows if str(r["actId"]) in prev and str(r["actId"]) not in hard]
        for group in (tier1, tier2):
            group.sort(key=lambda r: (-sup_total[r["ghName"]], -r["orders"]))
            for r in group:
                j = wd_index.get(prev[str(r["actId"])][0])
                if j is None or len(days[j]) >= target[j]:
                    continue
                put(r, j)
                pinned += 1
    if pinned:
        global _LAST_PINNED
        _LAST_PINNED = pinned
        print(f"[stable] 沿用原周几的 {pinned} 条（上一期表 {len(hard)} 条优先）")


    # 第二轮：剩下的贪心填
    done_ids = {str(r["actId"]) for d in days for r in d}
    rest = [r for r in rows if str(r["actId"]) not in done_ids]
    rest.sort(key=lambda r: (-sup_total[r["ghName"]], r.get("cat", 4), -r["orders"]))
    for r in rest:
        sup, cat = r["ghName"], r.get("cat", 4)
        cands = [j for j in range(7) if len(days[j]) < target[j]] or \
                [min(range(7), key=lambda j: len(days[j]))]
        j = min(cands, key=lambda x: (day_cat[x][cat], day_sup[x][sup], len(days[x]), x))
        put(r, j)

    return [_interleave(d) for d in days]


WEEK_LABEL = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

HOUR_START, HOUR_END = 7, 22          # 每天 07:00-22:00 之间开团
GAP_MIN, GAP_MAX = 15, 30             # 两条之间间隔锁在 15~30 分钟（均匀曝光）
_LAST_PINNED = 0                      # 上一次 split_seven 沿用上周同一天了几条（写说明用）


def _today_capacity(now=None):
    """今天还剩几个开团位（按最小间隔算）。窗口外返回 0。"""
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    start = max(HOUR_START * 60, ((cur + 9) // 5) * 5)
    end = HOUR_END * 60
    return max(0, (end - start) // GAP_MIN + 1) if start <= end else 0


def week_dates(start=None):
    """7 天窗口。

    默认从今天算（下午/上午出表，当天还能开几条就排几条）；
    但**今天基本过完了**（剩余位置 < 3，比如晚上出表）就从明天起排满 7 天，
    否则第一天只有一两条，后面几天白被摊薄。
    """
    start = start or datetime.now().date()
    if start == datetime.now().date() and _today_capacity() < 3:
        start = start + timedelta(days=1)
    return [start + timedelta(days=i) for i in range(7)]


def _spread(n, date, now=None):
    """把 n 条铺在今天的开团窗口里，返回 **分钟数** 列表（0 点起算）。

    目标是「半小时一条」那种均匀曝光，而不是整点扎堆：
      · 间隔先按 窗口/(n-1) 算，再夹到 15~30 分钟、向下取 5 分钟的整数倍；
      · 未来几天两头留白居中（07:00-22:00 正中间铺开）；
      · 今天从「现在之后的下一个 5 分钟点」直接开始，不居中，免得白白空等；
      · 窗口塞不下就压缩间隔（下限 15 分钟），宁可密一点也不跨出窗口。
    """
    if n <= 0:
        return []
    start, end = HOUR_START * 60, HOUR_END * 60
    today_mode = False
    if now is not None and date == now.date():
        cur = now.hour * 60 + now.minute
        start = max(start, ((cur + 4) // 5) * 5 + 5)     # 下一个 5 分钟点，留 5 分钟余量
        today_mode = True
    if start >= end:
        return [end] * n
    span = end - start
    if n == 1:
        return [start]
    gap = span / (n - 1)
    gap = int(min(GAP_MAX, max(GAP_MIN, gap)) // 5 * 5)
    if gap * (n - 1) > span:                              # 塞不下 → 再压缩到 5 分钟
        gap = max(5, int(span / (n - 1)) // 5 * 5)
    used = gap * (n - 1)
    base = start if today_mode else start + max(0, (span - used) // 2)
    return [base + i * gap for i in range(n)]


def hhmm(minutes):
    return f"{int(minutes) // 60:02d}:{int(minutes) % 60:02d}"


def build_slots(days, dates, skip_map=None, prev_time=None):
    """每天「要开」的条目各占一个分钟级时间点；跳过的条目不占时间（返回 None）。

    prev_time: actId -> 上一期的时间点（当天第几分钟）。
    保持与前一周开团顺序一致：
      · 老链接（前一周有排期记忆的）严格按前一周开团先后排序，保持相对顺序 100% 不变；
      · 本周新增链接排在老链接之后（按品类打散顺序），不插队打乱前一周的老链接顺序；
      · 排完时间后同步按时间先后重排当天 days[di] 与 slots[di]，让 Excel 和执行计划都严格按开团时间顺序展示。
    """
    skip_map = skip_map or {}
    prev_time = prev_time or {}
    now = datetime.now()
    out_slots = []
    for di, chunk in enumerate(days):
        idx = [i for i, r in enumerate(chunk) if str(r["actId"]) not in skip_map]
        row = [None] * len(chunk)
        if not idx:
            out_slots.append(row)
            continue
        times = _spread(len(idx), dates[di], now)
        if prev_time:
            keyed = []
            for pos, i in enumerate(idx):
                p = prev_time.get(str(chunk[i]["actId"]))
                if p is not None:
                    keyed.append((0, p, pos, i))     # 老链接优先，严格沿用上周先后顺序
                else:
                    keyed.append((1, pos, pos, i))   # 本周新增追加在后，不打乱上周顺序
            keyed.sort()
            order = [i for _, _, _, i in keyed]
        else:
            order = idx
        for i, t in zip(order, times):
            row[i] = t
        # 同步按开团时间先后重排当天的条目与时间（无时间点的跳过/已开条目排在当天末尾）
        paired = sorted(zip(chunk, row), key=lambda pair: (pair[1] is None, pair[1] if pair[1] is not None else 0))
        days[di] = [p[0] for p in paired]
        out_slots.append([p[1] for p in paired])
    return out_slots


def rebalance_today(days, dates, now=None):
    """出表当天已经过了早上 7 点：只留「窗口还塞得下」的条数，其余顺延到后面几天。

    以前窗口不够就把间隔压到 5 分钟，结果当天的团全挤在下午连着开——
    用户要的是 7:00-22:00 均匀曝光，不是「下午一波流」。
    这里按 15 分钟一条算容量，多出来的挪到后面最空的一天（并重排该天）。
    """
    now = now or datetime.now()
    if not days or not dates or dates[0] != now.date():
        return days, 0
    cur = now.hour * 60 + now.minute
    start = max(HOUR_START * 60, ((cur + 9) // 5) * 5)      # 现在之后的下一个 5 分钟点
    end = HOUR_END * 60
    cap = max(0, (end - start) // GAP_MIN + 1) if start < end else 0
    days = [list(d) for d in days]
    moved = 0
    while len(days[0]) > cap:
        r = days[0].pop()                                    # 从尾部挪（订单最低的先动）
        j = min(range(1, 7), key=lambda k: len(days[k]))
        days[j].append(r)
        days[j] = _interleave(days[j])
        moved += 1
    if moved:
        print(f"[today] 今天只剩 {cap} 个位置，{moved} 条顺延到后几天（不压缩间隔）")
    return days, moved


def add_skips(days, rows, dates=None, prev=None):
    """把「已在售、不重复开」的条目补回表里，同时尽量放回上周那一天。

    这些条不占开团时间，但用户是拿这张表看「哪家在周几」的，位置飘了会以为漏了。
    """
    days = [list(d) for d in days]
    prev = prev or {}
    wd_index = {d.weekday(): i for i, d in enumerate(dates)} if dates else {}
    for r in sorted(rows, key=lambda x: -x["orders"]):
        cat = r.get("cat", 4)
        k = None
        if wd_index and str(r["actId"]) in prev:
            k = wd_index.get(prev[str(r["actId"])][0])
        if k is None:
            k = min(range(7), key=lambda j: (sum(1 for x in days[j] if x.get("cat", 4) == cat),
                                             len(days[j]), j))
        days[k].append(r)
    return days


def day_label(d):
    """11-14 是周六 —— 用真实星期，别再用固定的周一到周日。"""
    return "周" + "一二三四五六日"[d.weekday()]


# ---------------------------------------------------------------- 7. 写表
def write_xlsx(path, title, days, dates, prev_map, note_lines, min_orders, dead=None,
               season_dropped=None, slots=None, skip_map=None, idle_label="待开", done_map=None,
               black_dropped=None):
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = title
    head = ["序号", "活动ID", "开团日", "日期", "品名", "可点击链接", "供货商", "账号类型", "品类",
            "订单数量", "初始开团日期", "本期新增", "计划开团时间", "执行状态"]
    ws.append(head)
    head_fill = PatternFill("solid", fgColor="1F6F4A")
    thin = Side(style="thin", color="D9D9D9")
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF", size=11)
        c.fill = head_fill
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = Border(bottom=thin)
    ws.freeze_panes = "A2"

    day_colors = ["FFF3E0", "E8F5E9", "E3F2FD", "F3E5F5", "FFFDE7", "E0F7FA", "FCE4EC"]
    n = 0
    for di, chunk in enumerate(days):
        for ii, r in enumerate(chunk):
            n += 1
            link = core_link(r["actId"])
            prev = prev_map.get(str(r["actId"]))
            delta = "" if prev is None else (r["orders"] - prev)
            if prev is None:
                delta = "新上榜"
            hour = (slots[di][ii] if slots and ii < len(slots[di]) else None)
            when = f"{dates[di]:%m-%d} {hhmm(hour)}" if hour is not None else ""
            st = (done_map or {}).get(str(r["actId"])) or \
                 (skip_map or {}).get(str(r["actId"])) or idle_label
            ws.append([n, r["actId"], day_label(dates[di]), f"{dates[di]:%m-%d}",
                       r["goodsName"] or r["actName"][:60],
                       link, r["ghName"], kind_label(r),
                       classify_cat.CAT_NAME.get(r.get("cat", 4), "杂货"),
                       r["orders"], r["startDate"], delta, when, st])
            row = ws.max_row
            fill = PatternFill("solid", fgColor=day_colors[di])
            for c in range(1, len(head) + 1):
                cell = ws.cell(row=row, column=c)
                cell.fill = fill
                cell.border = Border(bottom=thin)
                if c in (1, 2, 3, 4, 8, 9, 10, 11, 12, 13, 14):
                    cell.alignment = Alignment(horizontal="center")
            ws.cell(row=row, column=6).hyperlink = link
            ws.cell(row=row, column=6).font = Font(color="0B5FFF", size=9)
            ws.cell(row=row, column=5).alignment = Alignment(wrap_text=False)
            ws.cell(row=row, column=10).number_format = "#,##0"
            if str(r["actId"]) in (skip_map or {}) or str(r["actId"]) in (done_map or {}):
                cell = ws.cell(row=row, column=14)
                cell.font = Font(color="1F6F4A" if str(r["actId"]) in (done_map or {}) else "C00000",
                                 size=10, bold=True)
    widths = [6, 18, 8, 8, 60, 34, 18, 12, 10, 10, 12, 10, 14, 16]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.auto_filter.ref = f"A1:{get_column_letter(len(head))}{ws.max_row}"

    # 分日汇总（含四类分布）
    s = wb.create_sheet("分日统计")
    s.append(["开团日", "日期", "条数", "食品", "日用", "服饰美妆", "杂货",
              "覆盖供货商", "合计订单"])
    for c in s[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="EEEEEE")
    for di, chunk in enumerate(days):
        cnt = {c: sum(1 for x in chunk if x.get("cat", 4) == c) for c in CAT_ORDER}
        s.append([day_label(dates[di]), f"{dates[di]:%m-%d}", len(chunk),
                  cnt[1], cnt[2], cnt[3], cnt[4],
                  len({x["ghName"] for x in chunk}), sum(x["orders"] for x in chunk)])
    s.append(["合计", "", sum(len(d) for d in days),
              sum(1 for d in days for x in d if x.get("cat", 4) == 1),
              sum(1 for d in days for x in d if x.get("cat", 4) == 2),
              sum(1 for d in days for x in d if x.get("cat", 4) == 3),
              sum(1 for d in days for x in d if x.get("cat", 4) == 4),
              "", sum(x["orders"] for d_ in days for x in d_)])
    for i, w in enumerate([10, 10, 8, 8, 8, 10, 8, 14, 12], 1):
        s.column_dimensions[get_column_letter(i)].width = w

    # 零变化备查（上期有、本期订单数没动过 → 不进主表）
    if dead:
        d = wb.create_sheet("零变化备查")
        d.append(["序号", "活动ID", "品名", "可点击链接", "供货商", "账号类型", "订单数量", "初始开团日期"])
        for c in d[1]:
            c.font = Font(bold=True)
            c.fill = PatternFill("solid", fgColor="EEEEEE")
        for i, r in enumerate(sorted(dead, key=lambda x: -x["orders"]), 1):
            d.append([i, r["actId"], r["goodsName"] or r["actName"][:60], core_link(r["actId"]),
                      r["ghName"], kind_label(r), r["orders"], r["startDate"]])
            d.cell(row=d.max_row, column=4).hyperlink = core_link(r["actId"])
        for i, w in enumerate([6, 18, 60, 34, 18, 12, 10, 12], 1):
            d.column_dimensions[get_column_letter(i)].width = w
        d.freeze_panes = "A2"

    # 季节剔除清单（供核对，不参与开团排期）
    if season_dropped:
        z = wb.create_sheet("季节剔除")
        z.append(["序号", "活动ID", "品名", "供货商", "订单数量", "命中词"])
        for c in z[1]:
            c.font = Font(bold=True)
            c.fill = PatternFill("solid", fgColor="EEEEEE")
        for i, r in enumerate(sorted(season_dropped, key=lambda x: -x["orders"]), 1):
            z.append([i, r["actId"], r["goodsName"] or r["actName"][:60], r["ghName"],
                      r["orders"], r.get("seasonHit", "")])
        for i, w in enumerate([6, 18, 70, 18, 10, 10], 1):
            z.column_dimensions[get_column_letter(i)].width = w

    # 黑名单剔除（永久不开的供货商，只列出来备查）
    if black_dropped:
        k = wb.create_sheet("黑名单剔除")
        k.append(["序号", "活动ID", "品名", "可点击链接", "供货商", "账号类型", "订单数量"])
        for c in k[1]:
            c.font = Font(bold=True)
            c.fill = PatternFill("solid", fgColor="F2DCDB")
        for i, r in enumerate(sorted(black_dropped, key=lambda x: -x["orders"]), 1):
            k.append([i, r["actId"], r["goodsName"] or r["actName"][:60], core_link(r["actId"]),
                      r["ghName"], kind_label(r), r["orders"]])
            k.cell(row=k.max_row, column=4).hyperlink = core_link(r["actId"])
        for i, w in enumerate([6, 18, 60, 34, 18, 12, 10], 1):
            k.column_dimensions[get_column_letter(i)].width = w
        k.freeze_panes = "A2"

    # 说明
    e = wb.create_sheet("说明")
    for line in note_lines:
        e.append([line])
    e.column_dimensions["A"].width = 120
    for row in e.iter_rows():
        row[0].alignment = Alignment(wrap_text=True, vertical="top")
    wb.save(path)
    return n


LINK_CACHE = None


def core_link(act_id):
    global LINK_CACHE
    if LINK_CACHE is None:
        p = os.path.join(DATA, "short_link_cache.json")
        LINK_CACHE = {}
        if os.path.exists(p):
            with open(p) as f:
                LINK_CACHE = json.load(f)
    return LINK_CACHE.get(str(act_id), f"https://www.qunjielong.com/#/dashboard/{act_id}")


# ---------------------------------------------------------------- main
TANG_GH = "PspU4o9_bsxkBH0_6c6cef00"
PLAN_PATH = os.path.join(DATA, "open_plan_tang.json")
LOG_PATH = os.path.join(DATA, "open_log.json")
FOLLOW_MEM_PATH = os.path.join(DATA, "sched_follow.json")
OVERRIDE_PATH = os.path.join(DATA, "sched_override.json")
BLACKLIST_FILE = os.path.join(DATA, "blacklist_suppliers.json")
# 永久拉黑的供货商（子串匹配 ghName）。用户 2026-11-14 指定：这两家以后每周都不开。
BLACKLIST_DEFAULT = ["简小姐", "悦宝"]


def load_blacklist():
    """永久不开的供货商名单。文件存在就以文件为准，不存在用内置默认。

    文件长这样：{"suppliers": ["简小姐", "悦宝"], "note": "..."}
    匹配方式是「名字里包含」，所以「简小姐优选」「悦宝2号」都会被挡住。
    """
    try:
        with open(BLACKLIST_FILE) as f:
            b = json.load(f)
        lst = b.get("suppliers") if isinstance(b, dict) else b
        if lst:
            return [str(x) for x in lst if str(x).strip()]
    except Exception:
        pass
    return list(BLACKLIST_DEFAULT)


def is_blacklisted(name, bl):
    n = (name or "").strip()
    return any(k and k in n for k in bl)


def tang_detail():
    """汤团号下的逐条核对结果（verify_bs_tang.py 产出）。没有就当空。"""
    p = os.path.join(DATA, "bs234_detail_tang.json")
    try:
        return json.load(open(p))
    except Exception:
        return {}


def load_skip_map():
    """开团前查重的结果（dedup_open.py 产出）→ {actId: 短原因}。

    规则（2026-11-14 用户口径）：
      · 汤团号上 5 天内开过同款 → 不开；
      · 两份表之间重复 → 只留订单高的一条，其余不开。
    以前按「主页有在售同名团就跳过」是错的：这个号本来就把同一个母团每 7~20 天
    重开一次，371/620 条都挂着在售子团，按在售判会一条都开不出来。
    """
    p = os.path.join(DATA, "open_check.json")
    try:
        chk = json.load(open(p))
    except Exception:
        return {}
    out = {}
    for k, v in chk.items():
        if v.get("open"):
            continue
        out[k] = "重复，不开" if v.get("why") == "重复" else "5天内已开"
    return out


def _is_done_in_window(item, win_start_iso):
    """判断旧计划里的已执行状态（opened/failed/gone）是否属于「本周排期窗口」内。

    上周开过的团到了新一周必须重新进入待开（pending），不能沿用上周的 opened 状态永久跳过；
    只有本周窗口内（doneAt 或 date >= win_start_iso）已经执行过的，重跑时才保留进度。
    """
    if not item or item.get("status") not in ("opened", "failed", "gone"):
        return False
    done_at = (item.get("doneAt") or "").strip()[:10]
    if done_at:
        return done_at >= win_start_iso
    item_date = (item.get("date") or "").strip()[:10]
    return bool(item_date and item_date >= win_start_iso)


def export_plan(days, dates, slots, skip_map, detail=None):
    """把帮卖那份排期落成执行计划（自动开团的唯一数据源）。

    重跑生成器时不覆盖「本周窗口内」已有的执行进度：同 actId 若本周已 opened/failed，
    保留原状态与结果，只更新排期时间；上周已开过的在新一周一律重置为 pending 正常重开。
    另外，不提前按静态「5天内已开」标死 skip_recent（只跳过跨表「重复」），
    5 天冷却一律交给执行器在真正开团那一刻现场复核。
    """
    old = {}
    if os.path.exists(PLAN_PATH):
        try:
            blob = json.load(open(PLAN_PATH))
            old = {str(x["actId"]): x for x in blob.get("items", [])}
        except Exception:
            old = {}

    win_start = dates[0].isoformat()
    items = []
    for di, chunk in enumerate(days):
        for ii, r in enumerate(chunk):
            mins = slots[di][ii] if ii < len(slots[di]) else None
            if mins is None:
                mins = HOUR_END * 60
            key = str(r["actId"])
            prev = old.get(key) or {}
            done = _is_done_in_window(prev, win_start)
            skip_reason = skip_map.get(key, "")
            is_dup = skip_reason.startswith("重复")
            items.append({
                "actId": r["actId"],
                "name": (r["goodsName"] or r["actName"])[:60],
                "actName": r["actName"][:120],
                "supplier": r["ghName"],
                "cat": classify_cat.CAT_NAME.get(r.get("cat", 4), "杂货"),
                "orders": r["orders"],
                "date": dates[di].isoformat(),
                "time": hhmm(mins),          # HH:MM，执行器按这个到点开
                "slotMin": int(mins),        # 0 点起的分钟数，方便比较
                "slot": int(mins) // 60,     # 兼容老字段
                "status": prev.get("status") if done else
                          ("skip_recent" if is_dup else "pending"),
                "newActId": prev.get("newActId") if done else None,
                "note": prev.get("note") if done else (skip_reason if is_dup else ""),
                "doneAt": prev.get("doneAt") if done else None,
            })
    blob = {
        "account": "汤团",
        "ghId": TANG_GH,
        "createdAt": old.get("createdAt") or datetime.now().isoformat(timespec="seconds"),
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
        "hours": [HOUR_START, HOUR_END],
        "items": items,
    }
    with open(PLAN_PATH, "w") as f:
        json.dump(blob, f, ensure_ascii=False, indent=1)
    pend = sum(1 for x in items if x["status"] == "pending")
    return pend


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=int, default=MIN_ORDERS)
    ap.add_argument("--rescan", action="store_true", help="忽略池缓存重新全量扫广场")
    ap.add_argument("--force-all", action="store_true", help="强制全量重新查所有历史活动的详情（耗时较长），默认只增量查在售/潜力/新活动")
    ap.add_argument("--no-season", action="store_true")
    ap.add_argument("--offline", action="store_true", help="只用今天已抓的详情缓存，不联网")
    args = ap.parse_args()

    t0 = time.time()
    os.chdir(ROOT)          # hot_acts_report 的短链缓存用的是相对路径
    cfg = json.load(open(os.path.join(DATA, "config.json")))
    os.makedirs(OUT_DIR, exist_ok=True)

    pool = scan_pool(cfg, use_cache=not args.rescan, rescan_all=args.rescan)

    # 广场扫描会漏「长期挂着的老团」，把历史已知 actId 全部并进来兜底
    seeds = collect_seeds()
    seen = {str(a) for a in pool}
    extra = [a for a in sorted(seeds) if str(a) not in seen]
    if extra:
        print(f"[seed] 历史池补 {len(extra)} 条（广场扫不到的）")
        pool = list(pool) + extra
    else:
        print("[seed] 历史池无新增")

    cache = fetch_details(cfg, [] if args.offline else pool, force_all=args.force_all)
    print(f"[detail] 拿到 {len(cache)} 条详情，用时 {time.time()-t0:.0f}s")

    rows = []
    season_map = {"跟团": [], "帮卖": []}
    bl = load_blacklist()
    black_map = {"跟团": [], "帮卖": []}
    stat = {"total": 0, "paused": 0, "ended": 0, "low": 0, "season": 0, "nodata": 0, "black": 0}
    for aid, r in cache.items():
        stat["total"] += 1
        if not r or not r.get("actId"):
            stat["nodata"] += 1
            continue
        if r["activityStatus"] != 10:
            stat["ended"] += 1
            continue
        if r["pauseSw"] != 5:
            stat["paused"] += 1
            continue
        if r["orders"] < args.min:
            stat["low"] += 1
            continue
        if is_blacklisted(r.get("ghName"), bl):
            black_map["跟团" if r["activityType"] == 10 else "帮卖"].append(r)
            stat["black"] += 1
            continue
        if not args.no_season:
            drop, why = season_verdict(r["goodsName"] or r["actName"])
            if drop:
                r["seasonHit"] = why
                season_map["跟团" if r["activityType"] == 10 else "帮卖"].append(r)
                stat["season"] += 1
                continue
        rows.append(r)
    print(f"[filter] {stat}")

    # 老缓存只有扁平字段，同款判定会退化 → 对入表的候选补查一次规格结构（几百条，很快）
    need = [r for r in rows if r and not r.get("goods")]
    if need:
        print(f"[enrich] 补查规格结构 {len(need)} 条…", flush=True)
        headers = core.make_headers(cfg)
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(fetch_one, cfg, core.make_session(), headers, r["actId"]): r
                    for r in need}
            for f in as_completed(futs):
                r = futs[f]
                det = f.result()
                if det:
                    r.update(flatten(det))

    fu = [r for r in rows if r["activityType"] == 10]
    bs = [r for r in rows if r["activityType"] in (150, 160, 210)]
    other = [r for r in rows if r not in fu and r not in bs]
    print(f"[split] 跟团 {len(fu)} / 帮卖 {len(bs)} / 其它 {len(other)}")

    # 四分类（食品/日用/服饰美妆/杂货）—— 每天要混着来，先给每条打上类
    names = [r["goodsName"] or r["actName"][:40] for r in fu + bs]
    if names:
        cats = classify_cat.classify(names)
        for r in fu + bs:
            r["cat"] = cats.get(r["goodsName"] or r["actName"][:40], 4)
        from collections import Counter
        print(f"[cat] {dict(Counter(classify_cat.CAT_NAME[r['cat']] for r in fu + bs))}")

    prev_map, prev_file = load_prev_snapshot()
    if not prev_map:
        prev_map = prev_from_xlsx()
        prev_file = prev_file or "上一期 xlsx"
    print(f"[prev] 基线 {prev_file}，{len(prev_map)} 条")

    today = datetime.now().strftime("%Y%m%d")
    dates = week_dates()
    prev_sched = load_prev_schedule()            # actId -> (周几, 分钟)：上一期表里的
    actual_mem = load_actual_plan()               # actId -> (周几, 分钟)：上周实际执行的那份复团表
    plan_mem = load_plan_memory(dates)           # 现有计划已定的日期（重跑别乱动）
    log_mem = load_openlog_memory()              # 开团日志里上次开的周几
    follow_mem = load_follow_memory()            # 跟团表上次排的周几
    # 优先级：人工指定 > 上周实际执行表 > 上一期周报 > 现有计划 > 跟团表记忆 > 开团日志
    override = load_override()
    schedule_mem = dict(override)
    for src in (actual_mem, prev_sched, plan_mem, follow_mem, log_mem):
        for k, v in src.items():
            schedule_mem.setdefault(k, v)
    print(f"[stable] 记忆库：人工指定 {len(override)} / 实际执行表 {len(actual_mem)} / 上一期表 {len(prev_sched)} / "
          f"现有计划 {len(plan_mem)} / 跟团表 {len(follow_mem)} / 开团日志 {len(log_mem)} → 合计 {len(schedule_mem)} 条")
    prev_time = {k: v[1] for k, v in schedule_mem.items()}

    # 生成小程序短链（只有入表的活动需要，缓存复用）
    global LINK_CACHE
    ids = [str(r["actId"]) for r in fu + bs]
    try:
        import hot_acts_report as H
        lm, fail = H.build_links(ids, core.make_headers(cfg), workers=6)
        LINK_CACHE = lm
        print(f"[link] 短链可用 {len(lm)} 条（本期新生成失败 {fail}）")
    except Exception as e:
        print(f"[link] 短链生成跳过: {e}")

    out = []
    skip_all = load_skip_map()          # 两份表共用一套查重结果
    if skip_all:
        print(f"[skip] 查重跳过 {len(skip_all)} 条（5天内已开 / 重复）")
    # 已执行的（帮卖自动开团，且仅限本周排期窗口内已执行）→ 表里显示「已开」
    # 上周已开过的团在新一周必须重新排期并正常待开，不能沿用上周的「已开」状态
    done_map = {}
    win_start = dates[0].isoformat()
    try:
        blob = json.load(open(PLAN_PATH))
        for x in blob.get("items", []):
            if not _is_done_in_window(x, win_start):
                continue
            if x.get("status") == "opened":
                done_map[str(x["actId"])] = "已开"
            elif x.get("status") in ("failed", "gone"):
                done_map[str(x["actId"])] = "开团失败"
        if done_map:
            print(f"[done] 本周已执行 {len(done_map)} 条")
    except Exception:
        pass
    for label, group in (("跟团模式", fu), ("帮卖模式", bs)):
        d = dedup(group)
        print(f"[dedup] {label}: {len(group)} → {len(d)}")
        # 近期有出单
        alive, dead, fresh = [], [], []
        for r in d:
            p = prev_map.get(str(r["actId"]))
            if p is None:
                fresh.append(r)
                alive.append(r)
            elif r["orders"] != p:
                alive.append(r)
            else:
                dead.append(r)
        print(f"[active] {label}: 有变动 {len(alive)-len(fresh)} / 新上榜 {len(fresh)} / 零变化剔除 {len(dead)}")
        # 帮卖自动开团只静态跳过跨表「重复」，不提前按静态「5天内已开」剥夺排期，
        # 5 天冷却统一由执行器在真正开团当时现场复核（有开过才跳过）。
        if label == "帮卖模式":
            skip_map = {k: v for k, v in skip_all.items()
                        if k in {str(r["actId"]) for r in alive} and v.startswith("重复")}
        else:
            skip_map = {k: v for k, v in skip_all.items() if k in {str(r["actId"]) for r in alive}}
        dm = done_map if label == "帮卖模式" else {}
        hard_mem = {**prev_sched, **actual_mem, **override}
        if skip_map:
            ex = [r for r in alive if str(r["actId"]) not in skip_map]
            sk = [r for r in alive if str(r["actId"]) in skip_map]
            # 只在「要开的」里面均分，再把跳过的塞回去展示——不然被跳过的日子会显得很空
            days = add_skips(split_seven(ex, dates, schedule_mem, hard_mem), sk, dates, schedule_mem)
        else:
            days = split_seven(alive, dates, schedule_mem, hard_mem)
        # 今天已经过了早上，窗口塞不下的顺延到后面几天（不压缩间隔、不下午一波流）
        days, _moved = rebalance_today(days, dates)
        # 已开 / 跳过的都不占时间点，时间点全留给真正要开的
        slots = build_slots(days, dates, {**skip_map, **{k: "已开" for k in dm}}, prev_time)
        path = os.path.join(OUT_DIR, f"汤团_{label}_{args.min}单以上_{today}.xlsx")
        n_new = sum(1 for v in skip_map.values() if v.startswith("5天"))
        n_dup = len(skip_map) - n_new
        # 已开的里面有些同时又在 skip_map 里（比如今天刚开、5 天内算已开），别重复扣
        n_todo = len(alive) - len(skip_map) - sum(1 for k in dm if k not in skip_map)
        notes = [
            f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
            f"数据源：群接龙帮卖广场（汤团 + 汤团² 两个号都扫——两个号看到的供货商不一样），按活动生效时间逐月扫 2019-01 至今，共 {len(pool)} 个活动，逐条单查详情 {len(cache)} 条。",
            f"门槛：累计订单 ≥{args.min}，且当前在售、未暂停、未结束。",
            f"去重：同款（goodsId 交集 / 商品名同款 / 价格指纹）只留订单最高的一条；已去掉 {len(group)-len(d)} 条重复。",
            f"黑名单：{'、'.join(bl)} 永久不开团，本期剔除 {len(black_map['跟团' if label == '跟团模式' else '帮卖'])} 条（见「黑名单剔除」页）。",
            f"季节过滤：冬季剔除夏款（扇子/凉鞋/无袖/短袖/凉席等），本期剔除 {len(season_map['跟团' if label == '跟团模式' else '帮卖'])} 条（见「季节剔除」页）。",
            f"近期出单：与上一期（{prev_file}）比对订单数，零变化 {len(dead)} 条已剔除；『新上榜』= 上期没有、本期新进池。",
            f"排期：{len(alive)-len(skip_map)} 条要开的按 7 天均分（{dates[0]:%m-%d}~{dates[-1]:%m-%d}），"
            f"每天条数差不超过 1（出表时今天若已基本过完，窗口从明天起算满 7 天；"
            f"否则当天只排窗口放得下的，多出的顺延到后面几天）；"
            f"同一天按 食品→日用→服饰美妆→杂货 轮转、相邻两个不同供货商。",
            f"稳定排期：同一条链接尽量排回它原来那个周几、同一时段（本期沿用 {_LAST_PINNED} 条；"
            f"记忆来源＝上周实际执行表 {len(actual_mem)} 条（最优先，按短链反查活动ID）+ 上一期表 {len(prev_sched)} 条 + "
            f"现有计划 {len(plan_mem)} 条 + 跟团表 {len(follow_mem)} 条 + 开团日志 {len(log_mem)} 条），"
            f"让同一条链接两次开团的间隔稳稳落在 7 天左右，避免少于 5 天被平台判重复开团。",
            f"开团时间：每天 {HOUR_START}:00-{HOUR_END}:00 均匀摊开（间隔 {GAP_MIN}~{GAP_MAX} 分钟），"
            f"见『计划开团时间』列；出表时今天若已基本过完（剩余位置不足 3 个），窗口直接从明天起算满 7 天。",
            f"开前查重（两份表一起查）：{n_new} 条『5天内已开』（汤团号 5 天内给同一母团开过子团，或主页近 5 天有同名团）、"
            f"{n_dup} 条『重复』（与另一份表同款）——共 {len(skip_map)} 条不开，其余 {n_todo} 条才开。",
        ]
        if label == "帮卖模式":
            notes += [
                f"自动执行：待开 {n_todo} 条（已开 {len(dm)} 条）由「汤团」号（PspU4o9…）"
                f"按『计划开团时间』自动帮卖开团，每天 {HOUR_START}:00-{HOUR_END}:00 分钟级均匀摊开（间隔 {GAP_MIN}~{GAP_MAX} 分钟）、条数已按天均分。",
                "开每一条之前还会现场再查一次「5 天内有没有开过」，有就跳过并记在『执行状态』列，不会靠旧数据硬开。",
                "停止方法：在项目目录建一个 data/open_plan_STOP 空文件，下一次整点检查就会停。",
            ]
        if label == "跟团模式":          # 记下跟团排期，下次重跑别再挪日子
            try:
                mem = {str(r["actId"]): [dates[di].weekday(), int(slots[di][ii])]
                       for di, chunk in enumerate(days)
                       for ii, r in enumerate(chunk)
                       if ii < len(slots[di]) and slots[di][ii] is not None}
                with open(FOLLOW_MEM_PATH, "w") as f:
                    json.dump(mem, f, ensure_ascii=False)
                print(f"[stable] 记下跟团排期 {len(mem)} 条 → data/sched_follow.json")
            except Exception as e:
                print(f"[stable] 跟团排期记忆写入失败: {e}")
        bl_this = black_map["跟团" if label == "跟团模式" else "帮卖"]
        n = write_xlsx(path, f"{args.min}单以上{label[:2]}", days, dates, prev_map, notes,
                       args.min, dead=dead, slots=slots, skip_map=skip_map,
                       idle_label="待开" if label == "帮卖模式" else "可开",
                       done_map=done_map if label == "帮卖模式" else None,
                       black_dropped=bl_this,
                       season_dropped=season_map["跟团" if label == "跟团模式" else "帮卖"])
        out.append((path, n))
        if label == "帮卖模式":
            n_plan = export_plan(days, dates, slots, skip_map, detail=tang_detail())
            print(f"[plan] 写入待开团 {n_plan} 条 → data/open_plan_tang.json")

    # 存本期快照
    snap = {str(r["actId"]): {"orders": r["orders"], "name": r["goodsName"]} for r in rows}
    with open(os.path.join(DATA, f"{SNAP_PREFIX}{today}.json"), "w") as f:
        json.dump(snap, f, ensure_ascii=False)
    # 池缓存保留
    for p, cnt in out:
        print(f"[out] {p} ({cnt} 条)")
    print(f"总用时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
