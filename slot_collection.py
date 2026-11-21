#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""晨间 / 午间 合集（汤团、汤团² 各一条）。

    晨间 06:00   标题「11.22汤团主推榜」          窗口 昨天 06:00 ~ 今天 06:00（24 小时）
    午间 12:30   标题「11.22汤团午间好品不错过」   窗口 今天 06:00 ~ 12:30

和日间集市（19:00）的区别只在「看哪一段的数据」和「标题」，选品口径一样：
  · 汤团：自营跟团链接全部必进 + 本时段有出单 + 访问量前列 → 去重 → ≤200 条，封面 6 款食品
  · 汤团²：本时段开的帮卖链接全部必进（剔食品、剔 9.9 以下）+ 访问量前列 → 去重 → ≤200 条，
          封面 6 款要主图有真人（半身/大头照）

⚠️ 数据口径（实测，平台做不到逐小时）：
  queryPageGhGoodsRank 的 dataType 只认 10/20/30/40/50 —— 10=今天（0 点至今）、20=昨天全天，
  30/40/50 当前账号取不到数据，也没有 startTime/endTime 参数。所以：
    晨间 = 「昨天全天」+「今天 0 点至今」≈ 24 小时（多算了昨天 00:00~06:00 那 6 小时）
    午间 = 「今天 0 点至今」（多算了今天 00:00~06:00 那 6 小时）
  只能做近似，宁可多放不可漏放。

用法：
  python3 slot_collection.py --account tang  --slot morning            # 只看结果
  python3 slot_collection.py --account tang2 --slot noon --apply       # 真的建合集
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
import daily_collection2 as d2

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
log = dc.log

MAX_ITEMS = 200
MIN_ITEMS = 80
LOW_PRICE_CENTS = 990
POOL_DAYS_TANG = 30          # 汤团：主页翻到 30 天前就够（榜单里的老团也就这么久）
POOL_HOURS_TANG2 = 48        # 汤团²：候选池往回看 48 小时（补位用）

ACCOUNTS = {
    "tang": {"name": "汤团", "gh": dc.TANG, "cover": "food",
             "self_must": True, "uv": dc.UV_TOP_RATIO},
    "tang2": {"name": "汤团2", "gh": d2.TANG2, "cover": "model",
              "self_must": False, "uv": d2.UV_TOP_RATIO},
}

SLOTS = {
    "morning": {"label": "晨间", "suffix": "主推榜", "start": (-1, 6, 0),
                "end": (0, 6, 0), "yesterday": True, "uv": None},
    "noon": {"label": "午间", "suffix": "午间好品不错过", "start": (0, 6, 0),
             "end": (0, 12, 30), "yesterday": False, "uv": 0.50},
}


def ms(d, h, m):
    return int(datetime.datetime.combine(
        d, datetime.time(h, m)).timestamp() * 1000)


# ---------------------------------------------------------------- 1. 商品榜
def rank(session, headers, gh, field, data_type=10, max_pages=30):
    """按 field 排的商品榜。dtype: 10=今天、20=昨天。翻页参数是 page。"""
    out, seen = [], set()
    for p in range(1, max_pages + 1):
        try:
            j = session.post(core.BASE_URL + "/ghdata-biz/api/ghData/queryPageGhGoodsRank",
                             json={"dateTag": "today", "ghId": gh, "ghViewType": 10,
                                   "page": p, "pageSize": 30, "dataType": data_type,
                                   "orderField": field}, headers=headers, timeout=25).json()
            lst = ((j.get("data") or {}).get("entityList")) or []
        except Exception as e:
            log("商品榜取数失败", field, p, e)
            break
        new = [x for x in lst if x.get("goodsId") not in seen]
        for x in new:
            seen.add(x.get("goodsId"))
        out.extend(new)
        if not new:
            break
    return out


# ---------------------------------------------------------------- 2. 主页在售池
def feed(session, headers, gh, gh_type, cutoff_ms, only_type=None, max_pages=40):
    """翻主页 feed，取创建时间 ≥ cutoff 的活动；整页最新一条都早于 cutoff 才收工。

    ⚠️ 每页开头混着置顶老团，不能按单条 createTime 判停止。
    """
    out, page = {}, 0

    def one(p):
        try:
            r = session.post(core.BASE_URL + "/ghome-feed/ghome_feed/query_ghome_feed_back_v2",
                             json={"page": p, "pageSize": 50, "ghId": gh, "keyword": "",
                                   "ghType": gh_type, "sortRule": 10},
                             headers=headers, timeout=25).json()
            d = r.get("data") or {}
            return (d.get("feedItemDTOList") or []), d.get("hasNextPage")
        except Exception:
            return [], False

    while page < max_pages:
        page += 1
        lst, has_next = one(page)
        if not lst:
            break
        newest = 0
        for it in lst:
            a = it.get("actItemDTO") or {}
            c = a.get("createTime") or 0
            newest = max(newest, c)
            aid = a.get("actId")
            if not aid or c < cutoff_ms:
                continue
            if only_type and a.get("activityType") != only_type:
                continue
            aid = str(aid)
            if aid in out:
                continue
            pics = [p for p in (a.get("picList") or []) if p]
            out[aid] = {"actId": aid, "name": a.get("activityName") or "",
                        "create": c, "end": a.get("endTime") or 0,
                        "status": a.get("activityStatus"), "pause": a.get("pauseSw"),
                        "type": a.get("activityType"), "view": a.get("viewCount") or 0,
                        "pic": pics[0] if pics else "", "pics": pics,
                        "own": a.get("ghId") == gh}
        if newest < cutoff_ms or not has_next:
            break
    log(f"主页活动 {len(out)} 个（ghType={gh_type}，翻了 {page} 页）")
    return out


def live(acts):
    now_ms = int(time.time() * 1000)
    return {k: v for k, v in acts.items()
            if v["status"] == 10 and v["pause"] != 10 and (v["end"] or 0) > now_ms}


# ---------------------------------------------------------------- 3. 选品：汤团
def uv_top_of(uv_ranks, ratio):
    """每个榜各自取前 ratio，再合并（晨间有「昨天」「今天」两个榜，
    直接拼起来再按比例截会让今天的 0-6 点占满名额）。"""
    out, seen = [], set()
    for lst in uv_ranks:
        for g in lst[:max(1, round(len(lst) * ratio))]:
            gid = g.get("goodsId")
            if gid in seen:
                continue
            seen.add(gid)
            out.append(g)
    return out


def pick_tang(acts, cache, buy_rank, uv_ranks, must, ratio, start_ms, deduper):
    """必选 110 > 本时段有单 100 > 访问前列 90；同档按单量、访问量排。"""
    g2a = {}
    for aid, c in cache.items():
        if aid not in acts:
            continue
        key = (1 if acts[aid].get("own") else 0, acts[aid]["create"])
        for g in c.get("goods") or []:
            gid = g.get("goodsId")
            if not gid:
                continue
            if gid not in g2a or key > g2a[gid][0]:
                g2a[gid] = (key, aid)

    uv_top = uv_top_of(uv_ranks, ratio)
    picked, src, sales, best_uv = {}, {}, {}, {}

    def add(aid, tag, w):
        if not aid or aid not in acts or aid not in cache:
            return
        picked[aid] = max(picked.get(aid, 0), w)
        src.setdefault(aid, [])
        if tag not in src[aid]:
            src[aid].append(tag)

    for aid in must:
        add(aid, "自营必选", 110)
    for g in buy_rank:
        n = g.get("buyQuantity") or 0
        if n > 0 and g.get("goodsId") in g2a:
            aid = g2a[g["goodsId"]][1]
            add(aid, "本时段有单", 100)
            sales[aid] = sales.get(aid, 0) + n
    for g in uv_top:
        hit = g2a.get(g.get("goodsId"))
        if hit:
            add(hit[1], "访问前列", 90)
            best_uv[hit[1]] = max(best_uv.get(hit[1], 0), g.get("uv") or 0)

    def key_of(aid):
        return (picked.get(aid, 0), sales.get(aid, 0), best_uv.get(aid, 0),
                acts[aid]["view"], acts[aid]["create"])

    order = sorted(picked, key=key_of, reverse=True)
    if deduper is not None:
        order, _ = deduper.filter(order)

    if len(order) < MIN_ITEMS:                       # 不够 80 用窗口内新开的补
        pool = sorted([a for a in acts if a not in picked and a in cache
                       and acts[a]["create"] >= start_ms],
                      key=lambda a: (acts[a]["view"], acts[a]["create"]), reverse=True)
        for a in pool:
            if len(order) >= MIN_ITEMS:
                break
            if deduper is not None:
                why = deduper.dup_of(a)
                if why:
                    deduper.stats[why[0]] += 1
                    deduper.dropped.append((a, why[0], why[1]))
                    continue
                deduper.take(a)
            add(a, "本时段新开", 50)
            order.append(a)
        order = sorted(order, key=key_of, reverse=True)

    return order[:MAX_ITEMS], {a: src.get(a) or [] for a in order[:MAX_ITEMS]}, sales


# ---------------------------------------------------------------- 4. 选品：汤团²
def pick_tang2(acts, must, cache, buy_rank, uv_ranks, ratio, foods, lows):
    """本时段开的帮卖链接全进；访问前列的团补位到 200 上限。"""
    g2a, sales, uvv = {}, {}, {}
    for aid, c in cache.items():
        if aid not in acts:
            continue
        for g in c.get("goods") or []:
            gid = g.get("goodsId")
            if gid:
                g2a.setdefault(str(gid), aid)
    for g in buy_rank:
        aid = g2a.get(str(g.get("goodsId")))
        if aid:
            sales[aid] = sales.get(aid, 0) + (g.get("buyQuantity") or 0)

    uv_top = uv_top_of(uv_ranks, ratio)
    ok = {a for a in acts if a not in foods and a not in lows}
    must_ok = [a for a in must if a in ok]
    log(f"候选池 {len(acts)} 条（本时段开的 {len(must)}）→ 去掉食品/低值后可入 {len(ok)} 条，"
        f"其中本时段开的 {len(must_ok)} 条")

    src = {a: ["本时段帮卖"] for a in must_ok}
    hit_uv = set()
    for g in uv_top:
        hit = g2a.get(str(g.get("goodsId")))
        if hit and hit in ok:
            hit_uv.add(hit)
            uvv[hit] = max(uvv.get(hit, 0), g.get("uv") or 0)
    for a in hit_uv:
        src.setdefault(a, []).append("访问前列")
    log(f"访问榜合计 {sum(len(x) for x in uv_ranks)} 条，各取前 {round(ratio * 100)}% "
        f"→ {len(uv_top)} 条，命中 {len(hit_uv)} 个团")

    extra = sorted([a for a in hit_uv if a not in set(must_ok)],
                   key=lambda a: (-(sales.get(a, 0)), -uvv.get(a, 0)))
    room = max(0, MAX_ITEMS - len(must_ok))
    if len(extra) > room:
        log(f"补位名额 {room} 条，访问榜还有 {len(extra) - room} 条没放进来")
    keep = set(must_ok) | set(extra[:room])

    def key_of(a):
        return (1 if sales.get(a, 0) > 0 else 0, sales.get(a, 0), uvv.get(a, 0),
                acts[a]["view"], acts[a]["create"])

    order = sorted(keep, key=key_of, reverse=True)
    return order, {a: src.get(a) or [] for a in order}, sales


# ---------------------------------------------------------------- 5. 封面
def cover_food(order, acts, n=dc.COVER_FOOD):
    """汤团：封面 6 款从优先级最高的 24 条里挑食品类。"""
    head = order[:dc.COVER_SCAN]
    if not head:
        return []
    try:
        cat = cc.classify([acts[a]["name"] for a in head])
    except Exception as e:
        log("品类判定跳过：", e)
        return []
    picked = [a for a in head if cat.get(acts[a]["name"]) == 1][:n]
    if len(picked) < n:
        picked += [a for a in head if a not in picked][:n - len(picked)]
    log(f"封面 6 款：食品 {len([a for a in picked if cat.get(acts[a]['name']) == 1])} 个")
    return picked[:n]


# ---------------------------------------------------------------- 6. 导出与写合集
def dump_xlsx(payload, path, notes):
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill
    except Exception as e:
        log("不导 Excel：", e)
        return
    acts = payload.get("acts") or {}
    src = payload.get("source") or {}
    cover = set(payload.get("cover") or [])
    order = payload.get("actIdList") or []
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "选品预览"
    ws.append(["序号", "封面", "标题", "来源", "本时段单", "商品数", "累计浏览", "活动ID"])
    for i, aid in enumerate(order, 1):
        v = acts.get(aid) or {}
        ws.append([i, "★" if aid in cover else "", (v.get("name") or "")[:120],
                   "/".join(src.get(aid) or []), v.get("today") or 0, v.get("goods") or 0,
                   v.get("view") or 0, aid])
    ws2 = wb.create_sheet("去重剔除")
    ws2.append(["被剔除的团", "剔除原因", "和谁重复"])
    for d in payload.get("dedup_detail") or []:
        ws2.append([(d.get("name") or "")[:120], d.get("why"), str(d.get("dupWith"))])
    ws3 = wb.create_sheet("说明")
    for line in [f"标题：{payload.get('title')}",
                 f"总条数：{len(order)} 条（上限 200）"] + notes + \
                [f"去重剔除：{len(payload.get('dedup_detail') or [])} 条",
                 "生成脚本：slot_collection.py"]:
        ws3.append([line])
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="DDDDDD")
    ws.freeze_panes = "A2"
    ws.column_dimensions["C"].width = 90
    ws.column_dimensions["D"].width = 22
    ws.column_dimensions["H"].width = 20
    wb.save(path)
    log("预览表已写", os.path.basename(path))


def create_collection(session, headers, gh, title, act_ids):
    # 抄一份现成合集的背景图配置；那几条随时可能被用户删掉，所以按顺序试
    tpl = {}
    for tid in (2158859, dc.TEMPLATE_ID, d2.TEMPLATE_ID):
        try:
            tpl = session.post(core.BASE_URL + "/ghome-feed/act_collection/act_collection_query_by_id",
                               params={"actCollectionId": tid}, headers=headers,
                               timeout=20).json().get("data") or {}
        except Exception:
            tpl = {}
        if tpl:
            break
    ext = tpl.get("extJson") or (
        '{"backgroundImgObj":{"bgImgId":27,"bgImgType":20,'
        '"bgImgUrl":"/ss/app/image/plus-bg/plus-bg031.jpg","isLightText":false},'
        '"realPublishGhViewType":10,"isSyncOtherView":true}')
    body = {"title": title, "subTitle": "", "ghId": gh, "ghViewType": 0,
            "extJson": ext, "actIdList": [str(a) for a in act_ids]}
    r = session.post(core.BASE_URL + "/ghome-feed/act_collection/act_collection_create",
                     json=body, headers=headers, timeout=60).json()
    log("创建结果", r.get("code"), r.get("msg"), r.get("data"))
    return r


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", choices=list(ACCOUNTS), required=True)
    ap.add_argument("--slot", choices=list(SLOTS), required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--title", default="")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    acc, slot = ACCOUNTS[a.account], SLOTS[a.slot]
    today = datetime.date.today()
    start_ms = ms(today + datetime.timedelta(days=slot["start"][0]), *slot["start"][1:])
    end_ms = ms(today + datetime.timedelta(days=slot["end"][0]), *slot["end"][1:])
    ratio = slot["uv"] if slot["uv"] else acc["uv"]
    title = a.title or f"{today.month}.{today.day}{acc['name']}{slot['suffix']}"

    store = os.path.join(DATA, f"slot_{a.account}_{a.slot}.json")
    done = {}
    try:
        done = json.load(open(store))
    except Exception:
        pass
    if a.apply and done.get("date") == today.strftime("%Y-%m-%d") \
            and done.get("collectionId") and not a.force:
        log(f"今天这个时段已经建过了：合集 {done['collectionId']}（{done.get('title')}）")
        return

    cfg = json.load(open(os.path.join(DATA, "config.json")))
    cfg["gh_id"] = acc["gh"]
    headers = core.make_headers(cfg)
    session = core.make_session()

    log(f"== {acc['name']} · {slot['label']} == 标题「{title}」")
    log(f"窗口 {datetime.datetime.fromtimestamp(start_ms / 1000):%m-%d %H:%M}"
        f" ~ {datetime.datetime.fromtimestamp(end_ms / 1000):%m-%d %H:%M}"
        f"（访问量取前 {round(ratio * 100)}%）")

    # ① 商品榜：晨间用「昨天全天 + 今天至今」，午间只用「今天至今」
    buy_rank = rank(session, headers, acc["gh"], "buyQuantity", 10, max_pages=10)
    uv_ranks = [rank(session, headers, acc["gh"], "uv", 10, max_pages=30)]
    if slot["yesterday"]:
        buy_y = rank(session, headers, acc["gh"], "buyQuantity", 20, max_pages=10)
        uv_y = rank(session, headers, acc["gh"], "uv", 20, max_pages=30)
        log(f"昨天榜：销量 {len(buy_y)} 条 / 访问 {len(uv_y)} 条")
        seen = {x.get("goodsId") for x in buy_rank}
        buy_rank += [g for g in buy_y if g.get("goodsId") not in seen]
        uv_ranks.append(uv_y)
    log(f"销量榜 {len(buy_rank)} 条 / 访问榜 " +
        " + ".join(str(len(x)) for x in uv_ranks) + " 条")

    # ② 在售池
    if acc["self_must"]:
        cutoff = start_ms - POOL_DAYS_TANG * 86400 * 1000
        acts = feed(session, headers, acc["gh"], 20, cutoff)
        must_acts = dc.own_follow_acts(session, headers, start_ms)
        merged = 0
        for aid, v in must_acts.items():
            if aid not in acts:
                acts[aid] = v
                merged += 1
        log(f"自营跟团链接 {len(must_acts)} 条（{merged} 条主页 feed 里没有，单独补进来）")
        must = [aid for aid in must_acts if aid in acts]
    else:
        cutoff = start_ms - POOL_HOURS_TANG2 * 3600 * 1000
        acts = feed(session, headers, acc["gh"], 10, cutoff, only_type=150)
        must_acts = {k: v for k, v in acts.items() if v["create"] >= start_ms}
        must = list(must_acts)
        log(f"本时段开的帮卖链接 {len(must)} 条（必进）")

    acts = live(acts)
    must = [aid for aid in must if aid in acts]
    log(f"在售 {len(acts)} 条，必选 {len(must)} 条")

    # ③ 商品明细 / 价格 / 食品
    cache_path = d2.CACHE if a.account == "tang2" else dc.GOODS_CACHE
    dc.GOODS_CACHE = cache_path                    # fill_goods 写的是模块级常量
    try:
        cache = json.load(open(cache_path))
    except Exception:
        cache = {}
    cache = dc.fill_goods(session, headers, list(acts), cache)

    prices, foods, lows = {}, set(), set()
    if a.account == "tang2":
        try:
            prices = json.load(open(os.path.join(DATA, "coll2_prices.json")))
        except Exception:
            prices = {}
        prices = d2.fill_prices(session, headers, list(acts), prices)
        json.dump(prices, open(os.path.join(DATA, "coll2_prices.json"), "w"))
        foods = d2.food_acts(acts, cache)
        lows = {x for x in acts if prices.get(x) is not None and prices[x] <= LOW_PRICE_CENTS}
        log(f"食品类 {len(foods)} 条、9.9 以下 {len(lows)} 条（都剔除）")

    # ④ 选品 + 去重 + 封面
    deduper = dc.Deduper(acts, cache)
    if acc["self_must"]:
        order, src, sales = pick_tang(acts, cache, buy_rank, uv_ranks, must, ratio,
                                      start_ms, deduper)
        cover = cover_food(order, acts)
    else:
        order, src, sales = pick_tang2(acts, must, cache, buy_rank, uv_ranks, ratio,
                                       foods, lows)
        order, dropped = deduper.filter(order)
        log(f"去重剔除 {len(dropped)} 条 → 剩 {len(order)} 条")
        cover = d2.choose_cover(order, acts)
    order = cover + [a for a in order if a not in cover]

    if len(order) > MAX_ITEMS:
        log(f"超过上限，截到 {MAX_ITEMS} 条")
        order = order[:MAX_ITEMS]
    log(f"当选 {len(order)} 条；去重剔除 {len(deduper.dropped)} 条（{deduper.report()}）")
    for aid, why, other in deduper.dropped[:6]:
        log(f"    剔除 {((acts.get(aid) or {}).get('name') or '')[:20]:<22}{why} "
            f"← {((acts.get(other) or {}).get('name') or '')[:20]}")
    if len(order) < MIN_ITEMS:
        log(f"⚠️ 只有 {len(order)} 条，不足 {MIN_ITEMS}（这个时段有单/有访问的就这么多）")

    payload = {
        "date": today.strftime("%Y-%m-%d"), "slot": a.slot, "account": a.account,
        "title": title, "actIdList": order, "cover": cover, "source": src,
        "dedup": {"dropped": len(deduper.dropped), "by_reason": deduper.stats},
        "acts": {x: {"name": acts[x]["name"], "pics": acts[x].get("pics") or [],
                     "goods": len((cache.get(x) or {}).get("goods") or []),
                     "today": sales.get(x, 0), "view": acts[x].get("view") or 0,
                     "create": acts[x].get("create") or 0}
                 for x in order if x in acts},
        "dedup_detail": [{"actId": x, "why": w, "dupWith": o,
                          "name": (acts.get(x) or {}).get("name") or ""}
                         for x, w, o in deduper.dropped],
        "collectionId": done.get("collectionId") if done.get("date") ==
        today.strftime("%Y-%m-%d") else None,
    }
    json.dump(payload, open(store, "w"), ensure_ascii=False, indent=1)
    dump_xlsx(payload, os.path.join(
        HERE, "周报", f"{acc['name']}{slot['label']}预览_{today.strftime('%Y%m%d')}.xlsx"),
        [f"统计窗口：{datetime.datetime.fromtimestamp(start_ms / 1000):%m-%d %H:%M}"
         f" ~ {datetime.datetime.fromtimestamp(end_ms / 1000):%m-%d %H:%M}",
         f"访问量口径：前 {int(ratio * 100)}%",
         "封面 6 款：" + ("食品类" if acc["cover"] == "food" else "主图有真人（半身/大头照）")])

    if not a.apply:
        log(f"dry-run：{len(order)} 条，加 --apply 才真的建合集")
        return

    r = create_collection(session, headers, acc["gh"], title, order)
    cid = r.get("data")
    if cid:
        payload["collectionId"] = cid
        json.dump(payload, open(store, "w"), ensure_ascii=False, indent=1)
        log("合集 ID", cid, "已记入", os.path.basename(store))


if __name__ == "__main__":
    main()
