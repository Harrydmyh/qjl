#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""汤团「日间集市」每日选品合集。

每天 19:00 生成一条合集，标题 `汤团日间集市MM.DD`，80~200 条；
封面 6 款要主图有模特真人（半身/大头照），其中销量优先。

选品口径（用户 2026-11-21 定的五条，晚间那次以 12:30 之后开的链接起算）：
  1. **汤团自己供货的跟团链接（activityType=150 且没有上游供货方）必选**，
     只有这种团能邀请别的团长做二级跟团；
  2. 当天有出订单的商品 → 进；当天访问量排名前 10% 的也进（原 35%，为必选让位下调）；
  3. 当天开的链接、当天有销量的 → 进（补位池从 12:30 之后算）
  4. 排在最前 6 款的主图要有模特真人（半身/大头照），其中销量优先、食品次之
  5. 条数 80~200，标题带当天日期
  6. 同款只留一条（按优先级留下更高的那条），三条判据任一命中即算同款：
     · 底层商品库同源：goodsId 或 spuId 有交集
     · 标题 90% 以上文字一样（活动名或商品名）
     · 内部图片重合 ≥ 5 张

数据来源（都实测过）：
  · 商品数据   POST /ghdata-biz/api/ghData/queryPageGhGoodsRank
               dataType=10 是「今天」；orderField=uv 按访问量排、=buyQuantity 按销量排。
               **翻页参数是 page（写 pageNum 会一直返回第一页）**，每页 30 条、pageSize 无效。
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
import difflib
import json
import os
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

import core

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
TANG = "PspU4o9_bsxkBH0_6c6cef00"      # 汤团（开团号），合集也建在这个号上

GOODS_CACHE = os.path.join(DATA, "coll_goods_cache.json")
SELECTED = os.path.join(DATA, "coll_selected.json")       # 每天的选品结果留底
PREVIEW = os.path.join(DATA, "coll_preview.json")         # dry-run 的结果，不覆盖留底
TEMPLATE_ID = 2098411                                     # 只为抄 extJson（背景图/同步开关）

MIN_ITEMS, MAX_ITEMS = 80, 200
UV_TOP_RATIO = 0.10            # 当天访问量前 10%（2026-11-21 由 35% 下调，给必选让位）
COVER_FOOD = 6                 # 封面 6 款（主图有真人，销量优先）
COVER_SCAN = 24                # 从前 24 个候选里找主图有真人的
FACE_MIN_H = 0.075             # 人脸框高 / 图高，≥ 这个算半身照或大头照
IMG_BASE = "https://res0.shangshi360.com/"
FACE_SRC = os.path.join(HERE, "tools", "facedet.swift")
FACE_BIN = os.path.join(HERE, "tools", "facedet")
DAY_CUTOFF_HM = (12, 30)       # 「当天新开」只从 12:30 之后算（用户 2026-11-21 定）
REFRESH_HOURS = 12             # 同一个团的商品明细，12 小时内不重复拉
CACHE_VER = 2                  # 缓存结构版本；升级后自动重拉（v2 才带 spuId 和全部图片）

# 去重三条判据（用户 2026-11-21 定）
TITLE_SIM = 0.90               # 标题文字相似度 ≥ 90% 视为同款
PIC_OVERLAP = 5                # 同一个团之间重合图片 ≥ 5 张视为同款


def log(*a):
    print(f"[{datetime.datetime.now():%H:%M:%S}]", *a, flush=True)


# ---------------------------------------------------------------- 0. 同款判定
def _norm_title(s):
    """标题归一化：去 emoji / 标点 / 空白，转小写。"""
    s = str(s or "")
    s = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F]", "", s)
    return re.sub(r"[^\w\u4e00-\u9fff]", "", s).lower()


def _title_same(a, b):
    """两个标题是否 90% 以上文字一样。"""
    a, b = _norm_title(a), _norm_title(b)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) < 6:                      # 太短的标题不判，免得「赠品」互相误杀
        return False
    if len(short) / len(long_) < 0.55:      # 长度差太多，不可能 90% 重合
        return False
    if short in long_:                      # 完全包含，按 100% 算
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= TITLE_SIM


def _pic_key(u):
    """图片归一化：去 query、去尺寸后缀，取「目录/文件名」两段。"""
    u = str(u or "").strip().split("?")[0]
    if not u:
        return ""
    u = re.sub(r"[-_]\d{2,4}(x\d{2,4})?\.(jpg|jpeg|png|webp)$", "", u, flags=re.I)
    parts = [p for p in u.split("/") if p]
    return "/".join(parts[-2:]).lower()


class Deduper:
    """三层去重：底层商品同源 / 标题 90% 雷同 / 图片重合 5 张以上。

    用法：按优先级从高到低依次 answer `dup_of(aid)`，不重复就 `take(aid)`。
    已入选的团建倒排索引，几千个候选也只有线性开销。
    """

    def __init__(self, acts, cache):
        self.acts, self.cache = acts, cache
        self.gid = {}          # goodsId  → 已入选 actId
        self.spu = {}          # spuId    → 已入选 actId
        self.pic = {}          # 图片键   → 已入选 actId 列表
        self.titles = []       # [(归一化标题, actId)]
        self.stats = {"商品同源": 0, "标题雷同": 0, "图片重合": 0}
        self.dropped = []      # [(actId, 原因, 撞上的actId)]
        self.kept_ids = []
        self._fp = {}

    def _fp_of(self, aid):
        """→ (goodsId 集合, spuId 集合, 图片集合, 标题列表)"""
        if aid in self._fp:
            return self._fp[aid]
        c = self.cache.get(aid) or {}
        gids, spus, pics, titles = set(), set(), set(), []
        act = self.acts.get(aid) or {}
        if act.get("name"):
            titles.append(act["name"])
        for p in act.get("pics") or []:
            k = _pic_key(p)
            if k:
                pics.add(k)
        for g in c.get("goods") or []:
            if g.get("goodsId"):
                gids.add(str(g["goodsId"]))
            if g.get("spuId"):
                spus.add(str(g["spuId"]))
            if g.get("name"):
                titles.append(g["name"])
            for p in (g.get("pics") or [g.get("pic")]):
                k = _pic_key(p)
                if k:
                    pics.add(k)
        self._fp[aid] = (gids, spus, pics, titles)
        return self._fp[aid]

    def dup_of(self, aid):
        """撞上已入选的团就返回 (原因, 对方 actId)，否则 None。"""
        gids, spus, pics, titles = self._fp_of(aid)
        for g in gids:                                  # ① 底层商品库同源
            if g in self.gid:
                return "商品同源", self.gid[g]
        for s in spus:
            if s in self.spu and self.spu[s] != aid:
                return "商品同源", self.spu[s]
        hit = {}                                        # ② 图片重合 ≥5 张
        for k in pics:
            for other in self.pic.get(k, ()):
                hit[other] = hit.get(other, 0) + 1
        if hit:
            other, n = max(hit.items(), key=lambda kv: kv[1])
            if n >= PIC_OVERLAP:
                return "图片重合", other
        for mine in titles:                             # ③ 标题 90% 雷同
            for their, other in self.titles:
                if _title_same(mine, their):
                    return "标题雷同", other
        return None

    def take(self, aid):
        gids, spus, pics, titles = self._fp_of(aid)
        for g in gids:
            self.gid.setdefault(g, aid)
        for s in spus:
            self.spu.setdefault(s, aid)
        for k in pics:
            self.pic.setdefault(k, []).append(aid)
        for t in titles:
            nt = _norm_title(t)
            if nt:
                self.titles.append((nt, aid))

    def filter(self, ordered):
        """按传入顺序（优先级从高到低）过滤，返回 (保留, 丢弃)。"""
        kept = []
        for aid in ordered:
            why = self.dup_of(aid)
            if why:
                self.stats[why[0]] += 1
                self.dropped.append((aid, why[0], why[1]))
                continue
            self.take(aid)
            kept.append(aid)
        self.kept_ids = kept
        return kept, self.dropped

    def report(self):
        return "、".join(f"{k} {v} 条" for k, v in self.stats.items())


# ---------------------------------------------------------------- 1.5 自营跟团链接
def _root_of(session, headers, aid):
    """查一个活动有没有「上游供货方」。

    `POST /activity-biz/activity/detail/frame/help_sale_root {activityId}` 返回的
    `activityRootInfo`：
      · 帮卖的团 → 指向上游那家供货团长（ghName 就是供货商名字）；
      · **自己供货的团 → null**。
    ⚠️ 这条接口只吃 150（跟团那套 actId）；160 会报「接龙类型与接口类型不一致」，
    160 要换成 help_sale_child，所以这里先试 root、失败了再试 child。
    """
    for path in ("/activity-biz/activity/detail/frame/help_sale_root",
                 "/activity-biz/activity/detail/frame/help_sale_child"):
        try:
            j = session.post(core.BASE_URL + path, json={"activityId": int(aid)},
                             headers=headers, timeout=15).json()
        except Exception:
            continue
        if j.get("code") == 200:
            ri = (j.get("data") or {}).get("activityRootInfo") or {}
            # 有这个字段 = 帮卖（指向上游）；为 null = 自己供货
            return bool(ri), ri.get("ghName") or ""
    return None, ""


def own_follow_acts(session, headers, cutoff_ms, max_pages=20):
    """汤团自己供货的跟团链接（activityType=150 且没有上游供货方）。

    平台没有「我发过的跟团链接」列表接口（那个「邀请团长帮卖 / 邀请团长跟团」
    只是单个活动页上的分享按钮），所以只能从自己主页的「跟团视角」feed 里取 150，
    再用 help_sale_root 判有没有上游。

    2026-11-21 实测：全天 150 共 175 条，其中 17 条没有上游 = 自营，
    跟用户说的「每天更新 5-20 条」对得上；用户举例的黑金鹅绒服就在这 17 条里。
    ⚠️ 别再按「名字+创建时间」去跟 160 硬配（老做法扫 200 页、配半天还配不上）。
    """
    got = {}

    def one(p):
        try:
            r = session.post(core.BASE_URL + "/ghome-feed/ghome_feed/query_ghome_feed_back_v2",
                             json={"page": p, "pageSize": 50, "ghId": TANG, "keyword": "",
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
                "pic": pics[0] if pics else "", "pics": pics, "own": True,
            }
        # ⚠️ 不能按单条 createTime 判停止：每页开头混着置顶的老团（stickyStatus=210），
        # 按单条判会在第 1 页就收工。整页最新一条都早于 cutoff 才算翻完了。
        if newest < cutoff_ms or not has_next:
            break
        page += 1

    def judge(aid):
        has_up, sup = _root_of(session, headers, aid)
        return aid, has_up, sup

    own, help_sale = {}, 0
    with ThreadPoolExecutor(max_workers=10) as ex:
        for aid, has_up, sup in ex.map(judge, list(got)):
            if has_up is False:
                own[aid] = got[aid]
            elif has_up:
                help_sale += 1
    log(f"今天 {len(got)} 条跟团链接里，自己没有上游的 {len(own)} 条（帮卖 {help_sale} 条）")
    return own


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
                        "pic": pics[0] if pics else "", "pics": [p for p in pics if p],
                        "own": a.get("ghId") == TANG}
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
                pics = [p for p in (g.get("goodsPicList") or []) if p]
                goods.append({"goodsId": g.get("goodsId"),
                              "spuId": g.get("spuId"),
                              "name": g.get("goodsName") or "",
                              "pic": pics[0] if pics else "",
                              "pics": pics,
                              "price": g.get("groupBuyPrice"),
                              "bought": g.get("boughtCount") or 0})
        return aid, goods
    except Exception:
        return aid, None


def fill_goods(session, headers, acts, cache):
    """给在售团补齐商品明细；12 小时内拉过的不重复拉。"""
    now = time.time()
    todo = [a for a in acts
            if a not in cache or cache[a].get("v") != CACHE_VER
            or now - (cache[a].get("ts") or 0) > REFRESH_HOURS * 3600]
    stale = sum(1 for a in todo if a in cache and cache[a].get("v") != CACHE_VER)
    log(f"拉商品明细 {len(todo)} 个（缓存命中 {len(acts) - len(todo)}"
        + (f"；{stale} 个是旧版缓存，需要补 spuId/图片" if stale else "") + "）")
    done = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        for aid, goods in ex.map(lambda a: _one_act_goods(session, headers, a), todo):
            done += 1
            if goods is None:
                continue
            cache[aid] = {"goods": goods,
                          "orders": sum(g["bought"] for g in goods),
                          "v": CACHE_VER,
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
def pick(acts, cache, uv_rank, buy_rank, prev_orders, today_ms, deduper=None,
         must=None, cutoff_ms=0):
    """→ (有序 actId 列表, 来源标记, 今日单)

    权重刻意拉开档次：**自营跟团必选 110** > 有单 100 > 访问前列 90 > 今日开 50 > 补位 10。
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

    for aid in (must or []):                                     # ⓿ 自营跟团必选
        add(aid, "自营必选", 110)

    for g in buy_rank:                                           # ① 今日有出单的商品
        n = g.get("buyQuantity") or 0
        if n > 0 and g.get("goodsId") in g2a:
            aid = g2a[g["goodsId"]][1]
            add(aid, "今日有单", 100)
            today_order[aid] = today_order.get(aid, 0) + n

    for g in uv_top:                                             # ② 今日访问前 10%
        hit = g2a.get(g.get("goodsId"))
        if hit:
            add(hit[1], "访问前列", 90)
            best_uv[hit[1]] = max(best_uv.get(hit[1], 0), g.get("uv") or 0)

    def key_of(aid):
        return (picked.get(aid, 0), today_order.get(aid, 0), best_uv.get(aid, 0),
                delta.get(aid, 0), acts[aid]["view"], acts[aid]["create"])

    order = sorted(picked, key=key_of, reverse=True)

    # 去重：按优先级从高到低过一遍，同款只留一条（留下的是优先级更高的）
    if deduper is not None:
        order, _ = deduper.filter(order)

    # 条数口径（用户 2026-11-21 定）：达标多少就放多少，超过 200 截 200；
    # 不到 80 先用「今天开的团」按累计浏览补，再不够用其余在售补。补位同样要去重。
    def fill_from(pool, tag, w):
        pool.sort(key=lambda a: (acts[a]["view"], acts[a]["create"]), reverse=True)
        for a in pool:
            if len(order) >= MIN_ITEMS:
                break
            if a in picked:
                continue
            if deduper is not None:
                why = deduper.dup_of(a)
                if why:
                    deduper.stats[why[0]] += 1
                    deduper.dropped.append((a, why[0], why[1]))
                    continue
                deduper.take(a)
            add(a, tag, w)
            order.append(a)

    if len(order) < MIN_ITEMS:
        fill_from([a for a in acts if a not in picked and a in cache
                   and acts[a]["create"] >= (cutoff_ms or today_ms)], "今日新开", 50)
    if len(order) < MIN_ITEMS:
        fill_from([a for a in acts if a not in picked and a in cache], "补位", 10)

    # 只对去重后的这一份排序；早先这里用 sorted(picked) 会让被剔除的条目复活
    order = sorted(order, key=key_of, reverse=True)
    return order[:MAX_ITEMS], src, {**delta, **today_order}


def _ensure_facedet():
    """编译 tools/facedet（调 macOS 自带的 Vision 做人脸检测），编好就一直复用。"""
    if os.path.exists(FACE_BIN):
        return FACE_BIN
    try:
        r = subprocess.run(["swiftc", "-O", FACE_SRC, "-o", FACE_BIN],
                           capture_output=True, timeout=300)
        if r.returncode != 0:
            log("人脸检测工具编译失败：", (r.stderr or b"")[:200])
            return None
        log("人脸检测工具已编译")
        return FACE_BIN
    except Exception as e:
        log("人脸检测工具编译失败：", e)
        return None


def face_scores(pics):
    """并发下载主图 → [(人脸数, 最大脸高占比)]，顺序和 pics 一一对应。"""
    n = len(pics)
    blank = [(0, 0.0)] * n
    bin_ = _ensure_facedet()
    if not bin_ or not n:
        return blank
    tmp = tempfile.mkdtemp(prefix="cover_")
    try:
        import requests
        dl = requests.Session()
        dl.trust_env = False

        def grab(item):
            i, p = item
            rel = (p or "").lstrip("/")
            if not rel:
                return
            small = os.path.join(tmp, f"{i}s.jpg")
            # 走 OSS 缩略图（原图动辄 3MB，缩到 300 宽只有 40KB）
            for q in ("?x-oss-process=image/resize,w_300", ""):
                try:
                    r = dl.get(IMG_BASE + rel + q, timeout=25,
                               headers={"User-Agent": "Mozilla/5.0"})
                    if r.status_code == 200 and r.content:
                        open(small, "wb").write(r.content)
                        return
                except Exception:
                    continue

        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(grab, enumerate(pics)))

        files = [os.path.join(tmp, f"{i}s.jpg") for i in range(n)]
        todo = [f for f in files if os.path.exists(f)]
        if not todo:
            return blank
        out = subprocess.run([bin_] + todo, capture_output=True, timeout=180)
        res = {}
        for line in (out.stdout or b"").decode("utf-8", "ignore").splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            try:
                idx = int(os.path.basename(parts[0]).split("s.")[0])
                res[idx] = (int(parts[1]), float(parts[3]))
            except Exception:
                continue
        return [res.get(i, (0, 0.0)) for i in range(n)]
    except Exception as e:
        log("人脸检测失败，封面退回按原顺序取：", e)
        return blank
    finally:
        try:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass


def cover_first(order, acts, sales=None):
    """把封面 6 款提到最前面：主图要有模特真人（半身/大头照），其中销量优先。

    封面在合集里就是前 6 张图，只能靠调顺序实现。
    候选取优先级最高的 COVER_SCAN 条，在里面挑「检测到人脸、且脸够大」的，
    再按今天的单量排（单量一样时食品类优先），取前 6 条。
    """
    head = order[:COVER_SCAN]
    if not head:
        return order, set()
    sales = sales or {}
    pics = [((acts.get(a) or {}).get("pics") or [""])[0] for a in head]
    scores = face_scores(pics)
    face_ok = [a for a, (cnt, h) in zip(head, scores) if cnt > 0 and h >= FACE_MIN_H]
    log(f"封面候选 {len(head)} 个，主图有半身/大头人像 {len(face_ok)} 个")
    if len(face_ok) < COVER_FOOD:                     # 不够就把脸小一点的补上
        face_ok += [a for a, (cnt, h) in zip(head, scores)
                    if a not in face_ok and cnt > 0][:COVER_FOOD - len(face_ok)]
    if not face_ok:
        log("没有一张主图有真人，封面保持原顺序")
        return order, set()

    foods = set()
    try:
        import classify_cat
        cat = classify_cat.classify([acts[a]["name"] for a in face_ok])
        foods = {a for a in face_ok if cat.get(acts[a]["name"]) == 1}
    except Exception as e:
        log("品类判定跳过：", e)

    face_ok.sort(key=lambda a: (-(sales.get(a) or 0),
                                0 if a in foods else 1,
                                -(acts[a].get("view") or 0)))
    head6 = face_ok[:COVER_FOOD]
    if len(head6) < COVER_FOOD:                       # 实在不够就按原顺序凑
        head6 += [a for a in head if a not in head6][:COVER_FOOD - len(head6)]
    head6 = head6[:COVER_FOOD]
    log(f"封面 6 款：{len([a for a in head6 if a in foods])} 个食品 / "
        f"{len([a for a in head6 if a not in foods])} 个非食品")
    return head6 + [a for a in order if a not in head6], set(head6)


def dump_xlsx(payload, path):
    """把选品结果导成一份 Excel 方便人工核对（周报/日间集市预览_日期.xlsx）。"""
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
    heads = ["序号", "封面", "标题", "来源", "今日单", "商品数", "累计浏览", "活动ID"]
    ws.append(heads)
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
    must = sum(1 for a in order if "自营必选" in (src.get(a) or []))
    for line in [
        f"标题：{payload.get('title')}",
        f"总条数：{len(order)} 条（要求 80~200）",
        f"封面 6 款：主图有真人（半身/大头照），按今天的单量优先挑",
        f"自营跟团必选：{must} 条（自己供货、可邀请团长二级跟团）",
        f"访问量口径：前 {int(UV_TOP_RATIO * 100)}%",
        f"「当天新开」起算：{DAY_CUTOFF_HM[0]}:{DAY_CUTOFF_HM[1]:02d}",
        f"去重剔除：{len(payload.get('dedup_detail') or [])} 条",
        "生成脚本：daily_collection.py",
    ]:
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
    if a.apply and done.get("date") == datetime.date.today().strftime("%Y-%m-%d") \
            and done.get("collectionId") and not a.force:
        log(f"今天已经建过了：合集 {done['collectionId']}（{done.get('title')}）。"
            f"确实要再建一条加 --force；只想看结果就去掉 --apply")
        return

    cfg = json.load(open(os.path.join(DATA, "config.json")))
    cfg["gh_id"] = TANG
    headers = core.make_headers(cfg)
    session = core.make_session()

    today = datetime.date.today()
    today_ms = int(datetime.datetime.combine(today, datetime.time()).timestamp() * 1000)
    cutoff_ms = int(datetime.datetime.combine(
        today, datetime.time(*DAY_CUTOFF_HM)).timestamp() * 1000)

    uv_rank = goods_rank(session, headers, "uv", max_pages=30)
    buy_rank = goods_rank(session, headers, "buyQuantity", max_pages=10)
    log(f"今日商品数据：访问榜 {len(uv_rank)} 条（前 {round(len(uv_rank) * UV_TOP_RATIO)} 名进选品）"
        f" / 销量榜 {len(buy_rank)} 条（当天有单的商品全量）")

    acts_all = feed_acts(session, headers)

    # 自营跟团链接（用户 2026-11-21 要求作为必选）：自己供货的团没有上游，
    # 别的团长可以二级跟团，所以必须进合集。只取今天 12:30 之后开的。
    must_acts = own_follow_acts(session, headers, cutoff_ms)
    merged = 0
    for aid, v in must_acts.items():
        if aid not in acts_all:
            acts_all[aid] = v
            merged += 1
    log(f"自营跟团链接 {len(must_acts)} 条（{merged} 条主页 feed 里没有，单独补进来）")

    now_ms = int(time.time() * 1000)
    acts = {k: v for k, v in acts_all.items()
            if v["status"] == 10 and v["pause"] != 10 and (v["end"] or 0) > now_ms}
    new_today = sum(1 for v in acts.values() if v["create"] >= today_ms)
    log(f"在售 {len(acts)}（今日新开 {new_today}，其中 12:30 后 "
        f"{sum(1 for v in acts.values() if v['create'] >= cutoff_ms)}）")

    try:
        cache = json.load(open(GOODS_CACHE))
    except Exception:
        cache = {}
    if not a.no_fetch:
        cache = fill_goods(session, headers, list(acts), cache)

    prev_orders, back = prev_snapshot(today)
    log(f"商品明细 {len([x for x in acts if x in cache])} 个可用；上一份快照 "
        f"{len(prev_orders)} 条" + (f"（{back} 天前）" if back else "（无，今天只能靠商品榜）"))

    must = [aid for aid in must_acts if aid in acts and aid in cache]
    if len(must) < len(must_acts):
        log(f"⚠️ 有 {len(must_acts) - len(must)} 条自营链接拿不到商品明细/已下架，进不了合集")

    deduper = Deduper(acts, cache)
    order, src, delta = pick(acts, cache, uv_rank, buy_rank, prev_orders, today_ms,
                             deduper, must=must, cutoff_ms=cutoff_ms)
    order, cover = cover_first(order, acts, delta)

    log(f"当选 {len(deduper.kept_ids)} 条；去重剔除 {len(deduper.dropped)} 条（{deduper.report()}）")
    for aid, why, other in deduper.dropped[:8]:
        nm = (acts.get(aid) or {}).get("name") or str(aid)
        on = (acts.get(other) or {}).get("name") or str(other)
        log(f"    剔除 {nm[:20]:<22}{why} ← {on[:20]}")
    if len(deduper.dropped) > 8:
        log(f"    …另有 {len(deduper.dropped) - 8} 条同款剔除")
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
               "dedup": {"dropped": len(deduper.dropped), "by_reason": deduper.stats},
               "acts": {a: {"name": acts[a]["name"], "pics": acts[a].get("pics") or [],
                            "goods": len((cache.get(a) or {}).get("goods") or []),
                            "today": delta.get(a, 0), "view": acts[a].get("view") or 0,
                            "create": acts[a].get("create") or 0}
                        for a in order if a in acts},
               "dedup_detail": [{"actId": x, "why": w, "dupWith": o,
                                 "name": (acts.get(x) or {}).get("name") or ""}
                                for x, w, o in deduper.dropped],
               "collectionId": done.get("collectionId") if done.get("date") == today.strftime("%Y-%m-%d") else None}
    # dry-run 只写预览文件，不动当天留底（留底要跟真正建出来的合集一致）
    out = SELECTED if a.apply else PREVIEW
    json.dump(payload, open(out, "w"), ensure_ascii=False, indent=1)
    log(f"留底 {os.path.basename(out)}；标题「{title}」")
    dump_xlsx(payload, os.path.join(HERE, "周报",
                                    f"日间集市预览_{today.strftime('%Y%m%d')}.xlsx"))

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
