#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""汤团² 的日间集市合集（每天 19:00 建一条）。

和汤团主号（daily_collection.py）的区别：汤团² 是纯帮卖号，没有自己供货的团，
所以口径变成「今天开的帮卖链接全都要」，而不是「自营必选 + 有单挑」。

选品口径（用户 2026-11-21 定）：
  1. 今天开的**所有帮卖链接**（activityType=150）必进，不管有没有订单
  2. 访问量前 35% 的商品也作为候选补进来（原来是 10%，这个号方宽）
  3. **食品类不进**；9.9 元及以下的低值品不进
  4. 严格去重：同款只留一条，留下访问量更高的那条
  5. 条数 80~200，标题「汤团日间集市M.D」

用法：
  python3 daily_collection2.py            # 只看结果（写预览表，不动线上）
  python3 daily_collection2.py --apply    # 真的新建一条合集
"""
import argparse
import datetime
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import core
import classify_cat as cc
import daily_collection as dc

TANG2 = "PsHHJN8_bsMtSbS_b09f0984"      # 汤团²（纯帮卖号）
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

MIN_ITEMS, MAX_ITEMS = 80, 200
UV_TOP_RATIO = 0.35                     # 访问量前 35%
LOW_PRICE_CENTS = 990                   # 9.9 元（价格字段单位是分）
POOL_HOURS = 48                         # 候选池：今天开的必进，近两天开的用来补位
FOOD_CAT = 1                            # classify_cat：1=食品

CACHE = os.path.join(DATA, "coll2_goods_cache.json")
SELECTED = os.path.join(DATA, "coll2_selected.json")
PREVIEW = os.path.join(DATA, "coll2_preview.json")
TEMPLATE_ID = 2157915                   # 拿背景图配置用（汤团主号那条）

log = dc.log


# ---------------------------------------------------------------- 1. 今日帮卖链接
def today_follows(session, headers, cutoff_ms, max_pages=25):
    """今天开的帮卖链接（activityType=150）。

    ⚠️ 汤团² 主页要按 ghType=10 才看到 150；ghType=20 只给 160（子团那套）。
    同一个团 150/160 成对，商品一样，合集里放 150（跟团链接，能邀请二级跟团）。
    ⚠️ 不能按单条 createTime 判翻页结束：每页开头混着置顶老团，整页都早于 cutoff 才收工。
    """
    got = {}

    def one(p):
        try:
            r = session.post(core.BASE_URL + "/ghome-feed/ghome_feed/query_ghome_feed_back_v2",
                             json={"page": p, "pageSize": 50, "ghId": TANG2, "keyword": "",
                                   "ghType": 10, "sortRule": 10}, headers=headers, timeout=25).json()
            d = r.get("data") or {}
            return (d.get("feedItemDTOList") or []), d.get("hasNextPage")
        except Exception:
            return [], False

    page = 1
    while page <= max_pages:
        lst, has_next = one(page)
        if not lst:
            break
        newest = 0
        for it in lst:
            a = it.get("actItemDTO") or {}
            c = a.get("createTime") or 0
            newest = max(newest, c)
            if a.get("activityType") != 150 or not a.get("actId"):
                continue
            if c < cutoff_ms:
                continue
            pics = [p for p in (a.get("picList") or []) if p]
            got[str(a["actId"])] = {
                "actId": str(a["actId"]), "name": a.get("activityName") or "",
                "create": c, "end": a.get("endTime") or 0,
                "status": a.get("activityStatus"), "pause": a.get("pauseSw"),
                "type": 150, "view": a.get("viewCount") or 0,
                "pic": pics[0] if pics else "", "pics": pics,
                "today": True,
            }
        if newest < cutoff_ms or not has_next:
            break
        page += 1
    log(f"今天开的帮卖链接 {len(got)} 条（翻了 {page} 页）")
    return got


# ---------------------------------------------------------------- 2. 价格
def fetch_price(session, headers, aid):
    """→ 最低价（分）。汤团² 这些都是帮卖团，走 help_sale_root。"""
    for _ in range(3):
        try:
            j = session.post(core.BASE_URL + "/activity-biz/activity/detail/frame/help_sale_root",
                             json={"activityId": int(aid)}, headers=headers, timeout=20).json()
            a = (j.get("data") or {}).get("activity") or {}
            if a.get("minPrice") is not None:
                return aid, a.get("minPrice")
            if j.get("code") == 200:
                return aid, None
        except Exception:
            pass
        time.sleep(0.5)
    try:                                   # 兜底：有些活动要 child 那套
        j = session.post(core.BASE_URL + "/activity-biz/activity/detail/frame/help_sale_child",
                         json={"activityId": int(aid)}, headers=headers, timeout=20).json()
        a = (j.get("data") or {}).get("activity") or {}
        return aid, a.get("minPrice")
    except Exception:
        return aid, None


def fill_prices(session, headers, acts, store):
    todo = [a for a in acts if a not in store]
    if not todo:
        return store
    log(f"拉最低价 {len(todo)} 条")
    with ThreadPoolExecutor(max_workers=8) as ex:
        for aid, mn in ex.map(lambda a: fetch_price(session, headers, a), todo):
            store[aid] = mn
    return store


# ---------------------------------------------------------------- 3. 食品判定
def food_acts(acts, cache):
    """→ 食品类活动集合（团内 ≥50% 商品判成食品）。"""
    names = []
    for aid in acts:
        for g in (cache.get(aid) or {}).get("goods") or []:
            if g.get("name"):
                names.append(g["name"])
    names = list(dict.fromkeys(names))
    if not names:
        return set()
    try:
        cmap = cc.classify(names)
    except Exception as e:
        log("品类判定失败，跳过食品过滤：", e)
        return set()
    out = set()
    for aid in acts:
        goods = (cache.get(aid) or {}).get("goods") or []
        cats = [cmap.get(g.get("name")) for g in goods if g.get("name")]
        cats = [c for c in cats if c]
        if cats and sum(1 for c in cats if c == FOOD_CAT) / len(cats) >= 0.5:
            out.add(aid)
    log(f"食品类 {len(out)} 条（剔除）")
    return out


# ---------------------------------------------------------------- 4. 选品
def pick(acts, must, cache, uv_rank, foods, lows):
    """→ (有序 actId, 来源, 访问量)

    · must：**今天开的全部帮卖链接**（排除食品和 9.9 以下），一条都不能少
    · extra：访问榜前 35% 命中的团，用来补位到上限
    · 顺序按访问量从高到低排（封面就是前 6 条），但 must 的每一条都会在
    """
    g2a = {}
    for aid in acts:
        for g in (cache.get(aid) or {}).get("goods") or []:
            if g.get("goodsId"):
                g2a.setdefault(str(g["goodsId"]), aid)

    uv_top = uv_rank[:max(1, round(len(uv_rank) * UV_TOP_RATIO))]
    log(f"访问榜 {len(uv_rank)} 条，前 {round(UV_TOP_RATIO * 100)}% = {len(uv_top)} 条")

    ok = {a for a in acts if a not in foods and a not in lows}
    must_ok = [a for a in must if a in ok]
    log(f"候选池 {len(acts)} 条（今天开的 {len(must)}）→ 去掉食品/低值后可入 {len(ok)} 条，"
        f"其中今天开的 {len(must_ok)} 条")

    src, view = {}, {}
    for aid in ok:
        if aid in must:
            src.setdefault(aid, []).append("今日帮卖")
        view[aid] = acts[aid].get("view") or 0
    hit_uv = set()
    for g in uv_top:
        hit = g2a.get(str(g.get("goodsId")))
        if hit and hit in ok:
            hit_uv.add(hit)
            view[hit] = max(view.get(hit, 0), g.get("uv") or 0)
    for aid in hit_uv:
        src.setdefault(aid, []).append("访问前列")
    log(f"访问榜前 {round(UV_TOP_RATIO * 100)}% 命中 {len(hit_uv)} 个团")

    # 补位：访问榜里命中、但不是今天开的团，按访问量从高到低填到上限
    extra = sorted([a for a in hit_uv if a not in set(must_ok)],
                   key=lambda a: (-view.get(a, 0), -(acts[a].get("create") or 0)))
    room = max(0, MAX_ITEMS - len(must_ok))
    keep = set(must_ok) | set(extra[:room])
    if len(extra) > room:
        log(f"补位名额 {room} 条，访问榜还有 {len(extra) - room} 条没放进来")
    order = sorted(keep, key=lambda a: (-view.get(a, 0), -(acts[a].get("create") or 0)))
    return order, {a: src.get(a) or [] for a in order}, view


# ---------------------------------------------------------------- 5. 建合集
def create_collection(session, headers, title, act_ids):
    try:
        tpl = session.post(core.BASE_URL + "/ghome-feed/act_collection/act_collection_query_by_id",
                           params={"actCollectionId": TEMPLATE_ID}, headers=headers,
                           timeout=20).json().get("data") or {}
    except Exception:
        tpl = {}
    ext = tpl.get("extJson") or (
        '{"backgroundImgObj":{"bgImgId":27,"bgImgType":20,'
        '"bgImgUrl":"/ss/app/image/plus-bg/plus-bg031.jpg","isLightText":false},'
        '"realPublishGhViewType":10,"isSyncOtherView":true}')
    body = {"title": title, "subTitle": "", "ghId": TANG2, "ghViewType": 0,
            "extJson": ext, "actIdList": [str(a) for a in act_ids]}
    r = session.post(core.BASE_URL + "/ghome-feed/act_collection/act_collection_create",
                     json=body, headers=headers, timeout=60).json()
    log("创建结果", r.get("code"), r.get("msg"), r.get("data"))
    return r


# ---------------------------------------------------------------- 6. 预览表
def dump_xlsx(payload, path):
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill
    except Exception as e:
        log("不导 Excel：", e)
        return
    acts = payload.get("acts") or {}
    src = payload.get("source") or {}
    order = payload.get("actIdList") or []
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "选品预览"
    ws.append(["序号", "封面", "标题", "来源", "最低价", "商品数", "累计浏览", "活动ID"])
    for i, aid in enumerate(order, 1):
        v = acts.get(aid) or {}
        mn = v.get("min_price")
        ws.append([i, "★" if i <= 6 else "", (v.get("name") or "")[:120],
                   "/".join(src.get(aid) or []),
                   round(mn / 100, 2) if mn else "", v.get("goods") or 0,
                   v.get("view") or 0, aid])
    ws2 = wb.create_sheet("去重剔除")
    ws2.append(["被剔除的团", "剔除原因", "和谁重复"])
    for d in payload.get("dedup_detail") or []:
        ws2.append([(d.get("name") or "")[:120], d.get("why"), str(d.get("dupWith"))])
    ws3 = wb.create_sheet("说明")
    for line in [
        f"账号：汤团²（{TANG2}）",
        f"标题：{payload.get('title')}",
        f"总条数：{len(order)} 条（要求 80~200）",
        f"帮卖必选：{sum(1 for a in order if '今日帮卖' in (src.get(a) or []))} 条（今天开的全部帮卖链接）",
        f"访问量口径：前 {int(UV_TOP_RATIO * 100)}%",
        f"剔除：食品类 {payload.get('food_count')} 条、9.9 元及以下 "
        f"{payload.get('low_count')} 条",
        f"去重剔除：{len(payload.get('dedup_detail') or [])} 条",
        "生成脚本：daily_collection2.py",
    ]:
        ws3.append([line])
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="DDDDDD")
    ws.freeze_panes = "A2"
    ws.column_dimensions["C"].width = 90
    ws.column_dimensions["D"].width = 24
    ws.column_dimensions["H"].width = 20
    wb.save(path)
    log("预览表已写", os.path.basename(path))


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的新建合集")
    ap.add_argument("--title", default="")
    ap.add_argument("--force", action="store_true", help="今天建过也再建一条（会重复）")
    a = ap.parse_args()

    today = datetime.date.today()
    title = a.title or f"汤团日间集市{today.month}.{today.day}"

    done = {}
    try:
        done = json.load(open(SELECTED))
    except Exception:
        pass
    if a.apply and done.get("date") == today.strftime("%Y-%m-%d") \
            and done.get("collectionId") and not a.force:
        log(f"今天已经建过了：合集 {done['collectionId']}（{done.get('title')}）。"
            f"确实要再建一条加 --force；只想看结果就去掉 --apply")
        return

    cfg = json.load(open(os.path.join(DATA, "config.json")))
    cfg["gh_id"] = TANG2
    headers = core.make_headers(cfg)
    session = core.make_session()
    # dc 里的 goods_rank / fill_goods 用的是模块级 TANG，借过来跑汤团² 的榜
    dc.TANG = TANG2

    cutoff_ms = int((time.time() - POOL_HOURS * 3600) * 1000)
    acts = today_follows(session, headers, cutoff_ms)

    now_ms = int(time.time() * 1000)
    acts = {k: v for k, v in acts.items()
            if v["status"] == 10 and v["pause"] != 10 and (v["end"] or 0) > now_ms}
    today_ms = int(datetime.datetime.combine(today, datetime.time()).timestamp() * 1000)
    must = {a for a, v in acts.items() if (v.get("create") or 0) >= today_ms}
    log(f"在售 {len(acts)} 条，其中今天开的 {len(must)} 条（必进）")

    try:
        cache = json.load(open(CACHE))
    except Exception:
        cache = {}
    dc.GOODS_CACHE = CACHE
    cache = dc.fill_goods(session, headers, list(acts), cache)

    prices = {}
    try:
        prices = json.load(open(os.path.join(DATA, "coll2_prices.json")))
    except Exception:
        prices = {}
    prices = fill_prices(session, headers, list(acts), prices)
    json.dump(prices, open(os.path.join(DATA, "coll2_prices.json"), "w"))

    foods = food_acts(acts, cache)
    lows = {x for x in acts if prices.get(x) is not None and prices[x] <= LOW_PRICE_CENTS}
    log(f"9.9 元及以下 {len(lows)} 条（剔除）")

    uv_rank = dc.goods_rank(session, headers, "uv", max_pages=30)
    log(f"今日访问榜 {len(uv_rank)} 条")

    order, src, view = pick(acts, must, cache, uv_rank, foods, lows)

    # 严格去重：同款只留一条（上面已按访问量从高到低排好，留下的就是访问高的）
    deduper = dc.Deduper(acts, cache)
    order, dropped = deduper.filter(order)
    log(f"去重剔除 {len(dropped)} 条 → 剩 {len(order)} 条")

    if len(order) < MIN_ITEMS:
        log(f"⚠️ 只有 {len(order)} 条，不足 {MIN_ITEMS}；"
            f"今天开的帮卖链接就这么多，可用 --title 或调 UV 口径再看")

    payload = {
        "date": today.strftime("%Y-%m-%d"), "title": title, "gh_id": TANG2,
        "actIdList": order,
        "cover": order[:6],
        "source": {a: src.get(a) or [] for a in order},
        "food_count": len(foods), "low_count": len(lows),
        "dedup": {"dropped": len(dropped), "by_reason": deduper.stats},
        "acts": {a: {"name": acts[a]["name"], "pics": acts[a].get("pics") or [],
                     "view": view.get(a) or 0, "min_price": prices.get(a),
                     "goods": len((cache.get(a) or {}).get("goods") or [])}
                 for a in order},
        "dedup_detail": [{"actId": x, "why": w, "dupWith": o,
                          "name": (acts.get(x) or {}).get("name") or ""}
                         for x, w, o in dropped],
        "collectionId": (done.get("collectionId")
                         if done.get("date") == today.strftime("%Y-%m-%d") else None),
    }
    out = SELECTED if a.apply else PREVIEW
    json.dump(payload, open(out, "w"), ensure_ascii=False, indent=1)
    dump_xlsx(payload, os.path.join(HERE, "周报", f"汤团2日间集市预览_{today.strftime('%Y%m%d')}.xlsx"))
    log(f"留底 {os.path.basename(out)}；标题「{title}」；{len(order)} 条")

    if not a.apply:
        log("dry-run：加 --apply 才真的新建合集")
        return
    r = create_collection(session, headers, title, order)
    cid = None
    if isinstance(r.get("data"), dict):
        cid = r["data"].get("actCollectionId") or r["data"].get("id")
    elif isinstance(r.get("data"), int):
        cid = r["data"]
    if cid:
        payload["collectionId"] = cid
        json.dump(payload, open(SELECTED, "w"), ensure_ascii=False, indent=1)
        log(f"合集已建：{cid}")
    else:
        log("⚠️ 没拿到合集 ID，请到小程序里确认")


if __name__ == "__main__":
    main()
