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


def scan_pool(cfg, use_cache=True):
    path = os.path.join(DATA, "weekly800_pool.json")
    if use_cache and os.path.exists(path):
        age = time.time() - os.path.getmtime(path)
        if age < 12 * 3600:
            with open(path) as f:
                d = json.load(f)
            print(f"[pool] 复用缓存 {len(d)} 个 actId（{age/3600:.1f} 小时前）")
            return d

    headers = core.make_headers(cfg)
    gh_id = cfg["gh_id"]
    q = DummyQ()
    session = core.make_session()
    seen, ordered = set(), []
    d = datetime(2019, 1, 1)
    end = datetime.now()
    while d <= end:
        nxt = (d + timedelta(days=32)).replace(day=1)
        st = int(d.timestamp() * 1000)
        en = int(nxt.timestamp() * 1000)
        label = d.strftime("%Y-%m")
        try:
            items = fetch_window(session, headers, gh_id, st, en)
        except Exception as e:
            print(f"  {label} 失败: {e}")
            items = []
        added = 0
        for it in items:
            aid = it.get("actId")
            if aid and aid not in seen:
                seen.add(aid)
                ordered.append(aid)
                added += 1
        if added:
            print(f"  {label}: +{added} (累计 {len(ordered)})", flush=True)
        d = nxt
        # 未来月份没有活动，扫到当月就够
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


def fetch_details(cfg, act_ids, workers=12):
    """逐条查详情。缓存**只在当天有效** —— 订单数每天会变，跨天复用会让
    「近期有出单」的判定失真。"""
    cache_path = os.path.join(DATA, "weekly800_detail_cache.json")
    today = datetime.now().strftime("%Y%m%d")
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                blob = json.load(f)
            if blob.get("date") == today:
                cache = blob.get("data") or {}
        except Exception:
            cache = {}
    todo = [a for a in act_ids if str(a) not in cache]
    print(f"[detail] 待查 {len(todo)} / 共 {len(act_ids)}（同日缓存命中 {len(act_ids)-len(todo)}）")
    headers = core.make_headers(cfg)
    done = 0
    t0 = time.time()
    if todo:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(fetch_one, cfg, core.make_session(), headers, a): a for a in todo}
            for fut in as_completed(futs):
                a = futs[fut]
                det = fut.result()
                cache[str(a)] = flatten(det) if det else None
                done += 1
                if done % 500 == 0 or done == len(todo):
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
    """找最近一期的快照（不含今天的），返回 {actId: orders}"""
    cands = sorted(f for f in os.listdir(DATA)
                   if f.startswith(SNAP_PREFIX) and f.endswith(".json"))
    today = datetime.now().strftime("%Y%m%d")
    cands = [c for c in cands if c != f"{SNAP_PREFIX}{today}.json"]
    if not cands:
        return {}, None
    path = os.path.join(DATA, cands[-1])
    with open(path) as f:
        d = json.load(f)
    return {str(k): v.get("orders", 0) for k, v in d.items()}, cands[-1]


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


def split_seven(rows):
    """7 天分块：条数均分 + 四类每天均匀 + 供应商打散。

    做法：先算出每天的目标条数（差不超过 1），再逐条贪心——
    优先给「这一类目前最少」且「这家目前最少」的那天。
    这样每天都是食品/日用/服饰美妆/杂货混着来，不会出现某天全是吃的。
    """
    from collections import Counter
    n = len(rows)
    base, rem = divmod(n, 7)
    target = [base + (1 if j < rem else 0) for j in range(7)]

    days = [[] for _ in range(7)]
    day_cat = [Counter() for _ in range(7)]
    day_sup = [Counter() for _ in range(7)]
    sup_total = Counter(r["ghName"] for r in rows)

    # 条目多的供货商先排（否则最后全堆在一天），同类里订单高的先排
    order = sorted(rows, key=lambda r: (-sup_total[r["ghName"]], r.get("cat", 4), -r["orders"]))
    for r in order:
        sup, cat = r["ghName"], r.get("cat", 4)
        cands = [j for j in range(7) if len(days[j]) < target[j]] or \
                [min(range(7), key=lambda j: len(days[j]))]
        j = min(cands, key=lambda x: (day_cat[x][cat], day_sup[x][sup], len(days[x]), x))
        days[j].append(r)
        day_cat[j][cat] += 1
        day_sup[j][sup] += 1

    return [_interleave(d) for d in days]


WEEK_LABEL = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

HOUR_START, HOUR_END = 7, 22          # 每天 07:00-22:00 每小时一波


def week_dates():
    """从**今天**开始的 7 天（周六出表，当天就开跑）。"""
    today = datetime.now().date()
    return [today + timedelta(days=i) for i in range(7)]


def build_slots(days, dates, skip_map=None):
    """把每天**要开**的条目摊到整点上：07-22 共 16 个整点。

    今天特殊：只排「现在之后的整点」，不然一上来就把今天全放出去。
    被标记跳过（已在售）的条目不占整点，返回 None。
    """
    skip_map = skip_map or {}
    now = datetime.now()
    out = []
    for di, chunk in enumerate(days):
        hours = list(range(HOUR_START, HOUR_END + 1))
        if dates[di] == now.date():
            hours = [h for h in hours if h > now.hour]
        if not hours:
            hours = [HOUR_END]
        exec_n = sum(1 for r in chunk if str(r["actId"]) not in skip_map)
        m = len(hours)
        base, extra = divmod(exec_n, m)
        counts = [base] * m
        if extra:
            # 多出来的几条均匀插到整点里（不是全堆在第一小时）
            for k in range(extra):
                counts[(k * m) // extra] += 1
        slots = []
        for h, c in zip(hours, counts):
            slots.extend([h] * c)
        slots.extend([None] * (len(chunk) - exec_n))
        out.append(slots)
    return out


def add_skips(days, rows):
    """把「已在售、不重复开」的条目补到最空的那天，只影响展示与表格均衡。"""
    days = [list(d) for d in days]
    for r in sorted(rows, key=lambda x: -x["orders"]):
        cat = r.get("cat", 4)
        k = min(range(7), key=lambda j: (sum(1 for x in days[j] if x.get("cat", 4) == cat),
                                         len(days[j]), j))
        days[k].append(r)
    return days


def day_label(d):
    """11-14 是周六 —— 用真实星期，别再用固定的周一到周日。"""
    return "周" + "一二三四五六日"[d.weekday()]


# ---------------------------------------------------------------- 7. 写表
def write_xlsx(path, title, days, dates, prev_map, note_lines, min_orders, dead=None,
               season_dropped=None, slots=None, skip_map=None):
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = title
    head = ["序号", "开团日", "日期", "品名", "可点击链接", "供货商", "账号类型", "品类",
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
            when = f"{dates[di]:%m-%d} {hour:02d}:00" if hour else ""
            st = (skip_map or {}).get(str(r["actId"]), "")
            ws.append([n, day_label(dates[di]), f"{dates[di]:%m-%d}",
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
                if c in (1, 2, 3, 7, 8, 9, 10, 11, 12, 13):
                    cell.alignment = Alignment(horizontal="center")
            ws.cell(row=row, column=5).hyperlink = link
            ws.cell(row=row, column=5).font = Font(color="0B5FFF", size=9)
            ws.cell(row=row, column=4).alignment = Alignment(wrap_text=False)
            ws.cell(row=row, column=9).number_format = "#,##0"
            if st:
                ws.cell(row=row, column=13).font = Font(color="C00000", size=10)
    widths = [6, 8, 8, 60, 34, 18, 12, 10, 10, 12, 10, 14, 16]
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
        d.append(["序号", "品名", "可点击链接", "供货商", "账号类型", "订单数量", "初始开团日期"])
        for c in d[1]:
            c.font = Font(bold=True)
            c.fill = PatternFill("solid", fgColor="EEEEEE")
        for i, r in enumerate(sorted(dead, key=lambda x: -x["orders"]), 1):
            d.append([i, r["goodsName"] or r["actName"][:60], core_link(r["actId"]),
                      r["ghName"], kind_label(r), r["orders"], r["startDate"]])
            d.cell(row=d.max_row, column=3).hyperlink = core_link(r["actId"])
        for i, w in enumerate([6, 60, 34, 18, 12, 10, 12], 1):
            d.column_dimensions[get_column_letter(i)].width = w
        d.freeze_panes = "A2"

    # 季节剔除清单（供核对，不参与开团排期）
    if season_dropped:
        z = wb.create_sheet("季节剔除")
        z.append(["序号", "品名", "供货商", "订单数量", "命中词"])
        for c in z[1]:
            c.font = Font(bold=True)
            c.fill = PatternFill("solid", fgColor="EEEEEE")
        for i, r in enumerate(sorted(season_dropped, key=lambda x: -x["orders"]), 1):
            z.append([i, r["goodsName"] or r["actName"][:60], r["ghName"],
                      r["orders"], r.get("seasonHit", "")])
        for i, w in enumerate([6, 70, 18, 10, 10], 1):
            z.column_dimensions[get_column_letter(i)].width = w

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


def tang_detail():
    """汤团号下的逐条核对结果（verify_bs_tang.py 产出）。没有就当空。"""
    p = os.path.join(DATA, "bs234_detail_tang.json")
    try:
        return json.load(open(p))
    except Exception:
        return {}


def load_skip_map():
    """已有在售同名团 → 不重复开。

    202/234 条在这号上 isFollow=True（平台记录里早就跟过），但真正还能看到
    在售同名团的只有 50 多条。重复开会把同一个品挂成两个团，所以这里以
    「主页是否存在同名且在售的团」为准。
    """
    p = os.path.join(DATA, "bs234_mine_check.json")
    try:
        chk = json.load(open(p))
    except Exception:
        return {}
    return {k: "已在售，跳过" for k, v in chk.items() if v.get("live")}


def export_plan(days, dates, slots, skip_map, detail=None):
    """把帮卖那份排期落成执行计划（自动开团的唯一数据源）。

    重跑生成器时不覆盖已有的执行进度：同 actId 的 status 若是 opened/failed，
    保留原状态与结果，只更新排期时间。
    """
    old = {}
    if os.path.exists(PLAN_PATH):
        try:
            blob = json.load(open(PLAN_PATH))
            old = {str(x["actId"]): x for x in blob.get("items", [])}
        except Exception:
            old = {}

    items = []
    for di, chunk in enumerate(days):
        for ii, r in enumerate(chunk):
            hour = slots[di][ii] if ii < len(slots[di]) else HOUR_END
            key = str(r["actId"])
            prev = old.get(key) or {}
            done = prev.get("status") in ("opened", "failed", "skip_live", "gone")
            items.append({
                "actId": r["actId"],
                "name": (r["goodsName"] or r["actName"])[:60],
                "supplier": r["ghName"],
                "cat": classify_cat.CAT_NAME.get(r.get("cat", 4), "杂货"),
                "orders": r["orders"],
                "date": dates[di].isoformat(),
                "slot": hour,
                "status": prev.get("status") if done else
                          ("skip_live" if key in skip_map else "pending"),
                "newActId": prev.get("newActId"),
                "note": prev.get("note") or skip_map.get(key, ""),
                "doneAt": prev.get("doneAt"),
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
    ap.add_argument("--rescan", action="store_true")
    ap.add_argument("--no-season", action="store_true")
    ap.add_argument("--offline", action="store_true", help="只用今天已抓的详情缓存，不联网")
    args = ap.parse_args()

    t0 = time.time()
    os.chdir(ROOT)          # hot_acts_report 的短链缓存用的是相对路径
    cfg = json.load(open(os.path.join(DATA, "config.json")))
    os.makedirs(OUT_DIR, exist_ok=True)

    pool = scan_pool(cfg, use_cache=not args.rescan)

    # 广场扫描会漏「长期挂着的老团」，把历史已知 actId 全部并进来兜底
    seeds = collect_seeds()
    seen = {str(a) for a in pool}
    extra = [a for a in sorted(seeds) if str(a) not in seen]
    if extra:
        print(f"[seed] 历史池补 {len(extra)} 条（广场扫不到的）")
        pool = list(pool) + extra
    else:
        print("[seed] 历史池无新增")

    cache = fetch_details(cfg, [] if args.offline else pool)
    print(f"[detail] 拿到 {len(cache)} 条详情，用时 {time.time()-t0:.0f}s")

    rows = []
    season_map = {"跟团": [], "帮卖": []}
    stat = {"total": 0, "paused": 0, "ended": 0, "low": 0, "season": 0, "nodata": 0}
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
        skip_map = {} if label == "跟团模式" else load_skip_map()
        if skip_map:
            ex = [r for r in alive if str(r["actId"]) not in skip_map]
            sk = [r for r in alive if str(r["actId"]) in skip_map]
            # 只在「要开的」里面均分，再把跳过的塞回去展示——不然被跳过的日子会显得很空
            days = add_skips(split_seven(ex), sk)
        else:
            days = split_seven(alive)
        slots = build_slots(days, dates, skip_map)
        path = os.path.join(OUT_DIR, f"汤团_{label}_{args.min}单以上_{today}.xlsx")
        notes = [
            f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
            f"数据源：群接龙帮卖广场（汤团号），按活动生效时间逐月扫 2019-01 至今，共 {len(pool)} 个活动，逐条单查详情 {len(cache)} 条。",
            f"门槛：累计订单 ≥{args.min}，且当前在售、未暂停、未结束。",
            f"去重：同款（goodsId 交集 / 商品名同款 / 价格指纹）只留订单最高的一条；已去掉 {len(group)-len(d)} 条重复。",
            f"季节过滤：冬季剔除夏款（扇子/凉鞋/无袖/短袖/凉席等），本期剔除 {len(season_map['跟团' if label == '跟团模式' else '帮卖'])} 条（见「季节剔除」页）。",
            f"近期出单：与上一期（{prev_file}）比对订单数，零变化 {len(dead)} 条已剔除；『新上榜』= 上期没有、本期新进池。",
            f"排期：{len(alive)} 条排到 {dates[0]:%m-%d}~{dates[-1]:%m-%d}（今天起 7 天），"
            f"每天条数差不超过 1；同一天的团按 食品→日用→服饰美妆→杂货 轮转、相邻两个不同供货商。",
            f"开团时间：每天 {HOUR_START}:00-{HOUR_END}:00 按整点摊开，见『计划开团时间』列。",
        ]
        if label == "帮卖模式":
            notes += [
                f"自动执行：这 {len(alive)} 条由「汤团」号（PspU4o9…）按『计划开团时间』自动帮卖开团，"
                f"每小时一波、条数已均摊。",
                f"跳过规则：{len(skip_map)} 条在汤团号上已有**在售的同名团**，标『已在售』不重复开（避免同一款挂两个团）。",
                "停止方法：在项目目录建一个 data/open_plan_STOP 空文件，下一次整点检查就会停。",
            ]
        n = write_xlsx(path, f"{args.min}单以上{label[:2]}", days, dates, prev_map, notes,
                       args.min, dead=dead, slots=slots, skip_map=skip_map,
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
