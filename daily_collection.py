#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""汤团「日间集市」每日选品合集。

每天 19:00 生成一条合集，标题 `汤团日间集市MM.DD`，80~200 条，封面 6 款优先食品。

选品口径（用户 2026-11-21 定的五条）：
  1. 当天有出订单的商品 → 进；当天访问量排名前 35% 的也进
  2. 汤团自己开的团（ghId 是自己）作为必选
  3. 当天开的链接、当天有销量的 → 进
  4. 排在最前 6 款优先食品类
  5. 条数 80~200，标题带当天日期

数据来源（都实测过）：
  · 商品数据   POST /ghdata-biz/api/ghData/queryPageGhGoodsRank
               dataType=10 是「今天」；orderField=uv 按访问量排、=buyQuantity 按销量排。
               **每页最多 30 条、pageNum 翻不动**，所以只有「今天 uv 前 30」「今天销量前 30」。
               字段：goodsId / uv / buyQuantity / gmv / turnRate。
  · 主页在售团 POST /ghome-feed/ghome_feed/query_ghome_feed_back_v2 （ghType=20）
  · 活动商品   POST /activity-detail/activity/detail/general_goods  {activityId}
               → activityGoodsList[].goodsId / boughtCount / goodsPicList。
               ⚠️ 这是**唯一**能查到自己开的团的接口：帮卖广场的
               `query_list_by_act_ids` 对自己开的团一律返回空（实测连自家团都查不到）。
  · 累计单量   各商品 boughtCount 求和；每天落一份快照，隔日做差就是「今天出了多少单」。

用法：
  python3 daily_collection.py                # dry-run：只算不写
  python3 daily_collection.py --apply         # 真的新建一条合集
  python3 daily_collection.py --title 汤团日间集市11.21 --apply
"""
import argparse
import datetime
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import core

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
TANG = "PspU4o9_bsxkBH0_6c6cef00"      # 汤团（开团号），合集也建在这个号上

GOODS_CACHE = os.path.join(DATA, "coll_goods_cache.json")
SELECTED = os.path.join(DATA, "coll_selected.json")       # 每天的选品结果留底
TEMPLATE_ID = 2098411                                     # 只为抄 extJson（背景图/同步开关）

MIN_ITEMS, MAX_ITEMS = 80, 200
UV_TOP_RATIO = 0.35            # 当天访问量前 35%
COVER_FOOD = 6                 # 封面 6 款优先食品
REFRESH_HOURS = 12             # 同一个团的商品明细，12 小时内不重复拉


def log(*a):
    print(f"[{datetime.datetime.now():%H:%M:%S}]", *a, flush=True)


# ---------------------------------------------------------------- 1. 今日商品数据
def goods_rank(session, headers, order_field, max_pages=40):
    """今天按 order_field 排序的商品榜。

    ⚠️ 翻页参数是 `page`（不是 pageNum，写 pageNum 会一直返回第一页）；
    每页固定 30 条（pageSize 无效）。实测「销量」榜 5 页就到底（当天有单的商品全量），
    访问榜能翻很多页，取前 max_pages 页足够算「前 35%」。
    """
    out, seen = [], set()
    for p in range(1, max_pages + 1):
        try:
            j = session.post(core.BASE_URL + "/ghdata-biz/api/ghData/queryPageGhGoodsRank",
                             json={"dateTag": "today", "ghId": TANG, "ghViewType": 10,
                                   "page": p, "pageSize": 30, "dataType": 10,
                                   "orderField": order_field}, headers=headers, timeout=25).json()
            lst = ((j.get("data") or {}).get("entityList")) or []
        except Exception as e:
            log("商品榜取数失败", order_field, p, e)
            break
        new = [x for x in lst if x.get("goodsId") not in seen]
        for x in new:
            seen.add(x.get("goodsId"))
        out.extend(new)
        if not new:
            break
    return out


# ---------------------------------------------------------------- 2. 主页在售池
def feed_acts(session, headers):
    out, pages = {}, 0
    for p in range(1, 61):
        try:
            r = session.post(core.BASE_URL + "/ghome-feed/ghome_feed/query_ghome_feed_back_v2",
                             json={"page": p, "pageSize": 50, "ghId": TANG, "keyword": "",
                                   "ghType": 20, "sortRule": 10}, headers=headers, timeout=25).json()
        except Exception:
            break
        d = r.get("data") or {}
        lst = d.get("feedItemDTOList") or []
        if not lst:
            break
        pages = p
        for it in lst:
            a = it.get("actItemDTO") or {}
            aid = a.get("actId")
            if not aid:
                continue
            aid = str(aid)
            if aid in out:
                continue
            pics = a.get("picList") or []
            out[aid] = {"actId": aid, "name": a.get("activityName") or "",
                        "create": a.get("createTime") or 0, "end": a.get("endTime") or 0,
                        "status": a.get("activityStatus"), "pause": a.get("pauseSw"),
                        "type": a.get("activityType"), "view": a.get("viewCount") or 0,
                        "pic": pics[0] if pics else "", "own": a.get("ghId") == TANG}
        if not d.get("hasNextPage"):
            break
    log(f"主页团 {len(out)} 个（翻了 {pages} 页）")
    return out


# ---------------------------------------------------------------- 3. 活动商品明细
def _one_act_goods(session, headers, aid):
    try:
        j = session.post(core.BASE_URL + "/activity-detail/activity/detail/general_goods",
                         json={"activityId": int(aid)}, headers=headers, timeout=15).json()
        cats = ((j.get("data") or {}).get("goodsCategoryList")) or []
        goods = []
        for c in cats:
            for g in (c.get("activityGoodsList") or []):
                goods.append({"goodsId": g.get("goodsId"),
                              "name": g.get("goodsName") or "",
                              "pic": (g.get("goodsPicList") or [""])[0],
                              "price": g.get("groupBuyPrice"),
                              "bought": g.get("boughtCount") or 0})
        return aid, goods
    except Exception:
        return aid, None


def fill_goods(session, headers, acts, cache):
    """给在售团补齐商品明细；12 小时内拉过的不重复拉。"""
    now = time.time()
    todo = [a for a in acts
            if a not in cache or now - (cache[a].get("ts") or 0) > REFRESH_HOURS * 3600]
    log(f"拉商品明细 {len(todo)} 个（缓存命中 {len(acts) - len(todo)}）")
    done = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        for aid, goods in ex.map(lambda a: _one_act_goods(session, headers, a), todo):
            done += 1
            if goods is None:
                continue
            cache[aid] = {"goods": goods,
                          "orders": sum(g["bought"] for g in goods),
                          "ts": int(now)}
            if done % 400 == 0:
                log(f"  …{done}/{len(todo)}")
    json.dump(cache, open(GOODS_CACHE, "w"), ensure_ascii=False)
    return cache


# ---------------------------------------------------------------- 4. 快照差
def snap_path(d):
    return os.path.join(DATA, f"actstat_{d.strftime('%Y%m%d')}.json")


def prev_snapshot(today):
    for back in range(1, 5):
        p = snap_path(today - datetime.timedelta(days=back))
        if os.path.exists(p):
            try:
                return json.load(open(p)), back
            except Exception:
                pass
    return {}, None


def save_snapshot(acts, cache, today):
    payload = {a: (cache.get(a) or {}).get("orders") or 0 for a in acts if a in cache}
    json.dump(payload, open(snap_path(today), "w"), ensure_ascii=False)
    log("已写今日快照", os.path.basename(snap_path(today)), len(payload), "条")


# ---------------------------------------------------------------- 5. 选品
def pick(acts, cache, uv_rank, buy_rank, prev_orders, today_ms):
    """→ (有序 actId 列表, 来源标记, 今日单)

    权重刻意拉开档次：有单 100 > 访问前列 90 > 今日开 50 > 补位 10。
    早先给「今天开的团」100 分，结果 244 个今日新开把 200 个位置全占满，
    真正出了单的反而被挤出去——所以「今天开」只能算资格，不算优先。
    """
    delta, today_order = {}, {}
    for aid in acts:
        c = cache.get(aid) or {}
        if not c:
            continue
        if acts[aid]["create"] >= today_ms:
            delta[aid] = c.get("orders") or 0          # 今天开的，累计单量就是今天的
        elif aid in prev_orders:
            delta[aid] = max(0, (c.get("orders") or 0) - prev_orders[aid])

    # 商品 → 团：一个商品可能在多个在售团里，优先「自己开的 + 开得晚的」
    g2a = {}
    for aid, c in cache.items():
        if aid not in acts:
            continue
        key = (1 if acts[aid]["own"] else 0, acts[aid]["create"])
        for g in c.get("goods") or []:
            gid = g.get("goodsId")
            if not gid:
                continue
            if gid not in g2a or key > g2a[gid][0]:
                g2a[gid] = (key, aid)

    uv_top = uv_rank[:max(1, round(len(uv_rank) * UV_TOP_RATIO))]
    picked, src, best_uv = {}, {}, {}

    def add(aid, tag, w):
        if not aid or aid not in acts or aid not in cache:
            return
        picked[aid] = max(picked.get(aid, 0), w)
        src.setdefault(aid, [])
        if tag not in src[aid]:
            src[aid].append(tag)

    for g in buy_rank:                                           # ① 今日有出单的商品
        n = g.get("buyQuantity") or 0
        if n > 0 and g.get("goodsId") in g2a:
            aid = g2a[g["goodsId"]][1]
            add(aid, "今日有单", 100)
            today_order[aid] = today_order.get(aid, 0) + n

    for g in uv_top:                                             # ② 今日访问前 35%
        hit = g2a.get(g.get("goodsId"))
        if hit:
            add(hit[1], "访问前列", 90)
            best_uv[hit[1]] = max(best_uv.get(hit[1], 0), g.get("uv") or 0)

    def key_of(aid):
        return (picked.get(aid, 0), today_order.get(aid, 0), best_uv.get(aid, 0),
                delta.get(aid, 0), acts[aid]["view"], acts[aid]["create"])

    order = sorted(picked, key=key_of, reverse=True)

    # 条数口径（用户 2026-11-21 定）：达标多少就放多少，超过 200 截 200；
    # 不到 80 先用「今天开的团」按累计浏览补，再不够用其余在售补。
    if len(order) < MIN_ITEMS:
        extra = [a for a in acts if a not in picked and a in cache
                 and acts[a]["create"] >= today_ms]
        extra.sort(key=lambda a: (acts[a]["view"], acts[a]["create"]), reverse=True)
        for a in extra:
            if len(order) >= MIN_ITEMS:
                break
            add(a, "今日新开", 50)
            order.append(a)
    if len(order) < MIN_ITEMS:
        rest = [a for a in acts if a not in picked and a in cache]
        rest.sort(key=lambda a: (acts[a]["view"], acts[a]["create"]), reverse=True)
        for a in rest:
            if len(order) >= MIN_ITEMS:
                break
            add(a, "补位", 10)
            order.append(a)

    order = sorted(picked, key=key_of, reverse=True)
    return order[:MAX_ITEMS], src, {**delta, **today_order}


def food_first(order, acts):
    """前 6 款换成食品类；其余顺序不动。"""
    try:
        import classify_cat
        cat = classify_cat.classify([acts[a]["name"] for a in order])
    except Exception as e:
        log("品类判定跳过：", e)
        return order, set()
    foods = [a for a in order if cat.get(acts[a]["name"]) == 1]
    if not foods:
        return order, set()
    head = foods[:COVER_FOOD]
    return head + [a for a in order if a not in head], set(head)


# ---------------------------------------------------------------- 6. 写合集
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
    body = {"title": title, "subTitle": "", "ghId": TANG, "ghViewType": 0,
            "extJson": ext, "actIdList": [str(a) for a in act_ids]}
    r = session.post(core.BASE_URL + "/ghome-feed/act_collection/act_collection_create",
                     json=body, headers=headers, timeout=60).json()
    log("创建结果", r.get("code"), r.get("msg"), r.get("data"))
    return r


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的新建合集")
    ap.add_argument("--title", default="")
    ap.add_argument("--no-fetch", action="store_true", help="只用商品明细缓存")
    ap.add_argument("--force", action="store_true", help="今天已经建过也再建一条（慎用，会重复）")
    a = ap.parse_args()

    # 同一天只建一条：建过就把 actCollectionId 记进留底，重跑直接退出
    done = {}
    try:
        done = json.load(open(SELECTED))
    except Exception:
        pass
    if done.get("date") == datetime.date.today().strftime("%Y-%m-%d") and done.get("collectionId") \
            and not a.force:
        log(f"今天已经建过了：合集 {done['collectionId']}（{done.get('title')}）。"
            f"确实要再建一条加 --force")
        return

    cfg = json.load(open(os.path.join(DATA, "config.json")))
    cfg["gh_id"] = TANG
    headers = core.make_headers(cfg)
    session = core.make_session()

    today = datetime.date.today()
    today_ms = int(datetime.datetime.combine(today, datetime.time()).timestamp() * 1000)

    uv_rank = goods_rank(session, headers, "uv", max_pages=30)
    buy_rank = goods_rank(session, headers, "buyQuantity", max_pages=10)
    log(f"今日商品数据：访问榜 {len(uv_rank)} 条（前 {round(len(uv_rank) * UV_TOP_RATIO)} 名进选品）"
        f" / 销量榜 {len(buy_rank)} 条（当天有单的商品全量）")

    acts_all = feed_acts(session, headers)
    now_ms = int(time.time() * 1000)
    acts = {k: v for k, v in acts_all.items()
            if v["status"] == 10 and v["pause"] != 10 and (v["end"] or 0) > now_ms}
    log(f"在售 {len(acts)}（今日新开 {sum(1 for v in acts.values() if v['create'] >= today_ms)}，"
        f"自己开的 {sum(1 for v in acts.values() if v['own'])}）")

    try:
        cache = json.load(open(GOODS_CACHE))
    except Exception:
        cache = {}
    if not a.no_fetch:
        cache = fill_goods(session, headers, list(acts), cache)

    prev_orders, back = prev_snapshot(today)
    log(f"商品明细 {len([x for x in acts if x in cache])} 个可用；上一份快照 "
        f"{len(prev_orders)} 条" + (f"（{back} 天前）" if back else "（无，今天只能靠商品榜）"))

    order, src, delta = pick(acts, cache, uv_rank, buy_rank, prev_orders, today_ms)
    order, cover = food_first(order, acts)

    log(f"选出 {len(order)} 条")
    for i, aid in enumerate(order, 1):
        v = acts[aid]
        log(f"{'★' if aid in cover else ' '}{i:>3}. {v['name'][:24]:<26} 今日单 {delta.get(aid, 0):>3} "
            f"{'/'.join(src.get(aid) or [])}")

    if not order:
        log("一条都没选出来，先不动")
        return

    title = a.title or f"汤团日间集市{today.month}.{today.day}"
    payload = {"date": today.strftime("%Y-%m-%d"), "title": title, "actIdList": order,
               "cover": sorted(cover), "source": src,
               "collectionId": done.get("collectionId") if done.get("date") == today.strftime("%Y-%m-%d") else None}
    json.dump(payload, open(SELECTED, "w"), ensure_ascii=False, indent=1)
    log(f"留底 {os.path.basename(SELECTED)}；标题「{title}」")

    if not a.apply:
        log("dry-run：加 --apply 才真的新建合集")
        return

    r = create_collection(session, headers, title, order)
    cid = r.get("data") if isinstance(r, dict) else None
    if cid:
        payload["collectionId"] = cid
        json.dump(payload, open(SELECTED, "w"), ensure_ascii=False, indent=1)
        log("合集 ID", cid, "已记入留底，今天再跑会被拦住")
    save_snapshot(acts, cache, today)


if __name__ == "__main__":
    main()
