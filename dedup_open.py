#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开团前的三道查重（两份周报放在一起查）。

用户要求：
  1. 汤团号上「5 天内开过的」同款，不再开；
  2. 两份表之间有重复的，只开一条；
  3. 跟团表 + 帮卖表 620 条**全部**参与查重。

三道判据（从硬到软）：
  A. 帮卖：`list_gh_child_activities(pactId=母团)` —— 我在汤团号下给这个母团
     开过几个子团、什么时候开的，这是平台自己的记录，最硬。
  B. 跟团：主页团名匹配 —— 跟团不产生子团记录，只能按名字认。
  C. 同款：620 条之间用 core._goods_groups（goodsId 交集 / 标题 / 价格指纹 /
     同母团 startActId）分组，组内只留一条（订单最高的）。

产出 data/open_check.json：
  {"actId": {"open": bool, "why": "...", "evidence": ...}, ...}
  why 取值：可开 / 5天内已开 / 已有在售 / 重复

用法：
  python3 dedup_open.py             # 全量（会打 ~620 次子团接口，约 1 分钟）
  python3 dedup_open.py --offline   # 用上次抓的子团结果，只重算分组
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
OUT_DIR = os.path.join(ROOT, "周报")
TANG_GH = "PspU4o9_bsxkBH0_6c6cef00"
CACHE_CHILD = os.path.join(DATA, "open_check_child.json")
CACHE_OWN = os.path.join(DATA, "tang_own_acts.json")
RESULT = os.path.join(DATA, "open_check.json")
WINDOW_DAYS = 5


def say(m):
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


# ---------------------------------------------------------------- 1. 读两份表
def load_rows():
    import openpyxl
    l2a = {}
    p_link = os.path.join(DATA, "short_link_cache.json")
    if os.path.exists(p_link):
        try:
            l2a = {l: a for a, l in json.load(open(p_link)).items()}
        except Exception:
            pass
    files = [("跟团", os.path.join(OUT_DIR, "汤团_跟团模式_800单以上_20261114.xlsx")),
             ("帮卖", os.path.join(OUT_DIR, "汤团_帮卖模式_800单以上_20261114.xlsx"))]
    out = []
    for label, path in files:
        if not os.path.exists(path):
            continue
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb[wb.sheetnames[0]]
        head = [c.value for c in ws[1]]
        I = {h: i for i, h in enumerate(head)}
        for r in ws.iter_rows(min_row=2, values_only=True):
            act = None
            if "活动ID" in I and r[I["活动ID"]]:
                act = r[I["活动ID"]]
            elif "可点击链接" in I and l2a:
                act = l2a.get(r[I["可点击链接"]])
            if not act:
                continue
            out.append({
                "table": label,
                "actId": int(act),
                "name": r[I["品名"]] or "",
                "supplier": r[I["供货商"]] or "",
                "orders": r[I["订单数量"]] or 0,
                "cat": r[I["品类"]] or "",
                "date": str(r[I["日期"]]),
                "slot": r[I["计划开团时间"]] or "",
            })
    return out


# ---------------------------------------------------------------- 2. 详情补 goodsId
def fill_goods(rows):
    blob = json.load(open(os.path.join(DATA, "weekly800_detail_cache.json")))
    cache = blob.get("data") or {}
    miss = []
    for r in rows:
        v = cache.get(str(r["actId"])) or {}
        r["goodsIds"] = v.get("goodsIds") or []
        r["goodsNames"] = v.get("goodsNames") or []
        r["actName"] = v.get("actName") or r["name"]
        r["price"] = v.get("price") or 0
        r["startActId"] = v.get("startActId") or 0
        r["ghName"] = v.get("ghName") or r["supplier"]
        if not r["goodsIds"]:
            miss.append(r)
    if miss:
        say(f"详情缓存缺 {len(miss)} 条 goodsId，现查")
        cfg = json.load(open(os.path.join(DATA, "config.json")))
        h = core.make_headers(cfg)
        s = core.make_session()

        def one(r):
            for fv in (8, 6):
                try:
                    out = core._post_detail_batch(s, h, cfg["gh_id"], [r["actId"]], fromv=fv)
                except Exception:
                    out = []
                if out:
                    d = out[0]
                    g = d.get("basicFeedGoodDTOList") or []
                    r["goodsIds"] = sorted({x.get("goodsId") for x in g if x.get("goodsId")})
                    r["goodsNames"] = [(x.get("goodsName") or "").strip() for x in g]
                    r["actName"] = (d.get("actName") or "").strip()
                    r["startActId"] = d.get("startActId") or 0
                    return r
            return r

        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(one, miss))
        say(f"补完，仍缺 goodsId {sum(1 for r in miss if not r['goodsIds'])} 条")
    return rows


# ---------------------------------------------------------------- 3. 子团记录
def fetch_children(rows, workers=6):
    """每条母团 → 我在汤团号下开过的子团（actId / createTime / status）"""
    if os.path.exists(CACHE_CHILD):
        try:
            cache = json.load(open(CACHE_CHILD))
            if time.time() - cache.get("at", 0) < 6 * 3600:
                say(f"子团记录用缓存（{len(cache['data'])} 条，{int((time.time()-cache['at'])/60)} 分钟前）")
                return cache["data"]
        except Exception:
            pass

    cfg = json.load(open(os.path.join(DATA, "config.json")))
    h = core.make_headers(cfg)
    url = "https://apipro.qunjielong.com/activity-accessory/gh_activity/list_gh_child_activities"

    def one(act):
        s = core.make_session()
        got = []
        for page in (1, 2):
            try:
                r = s.post(url, json={"ghId": TANG_GH, "pactId": int(act),
                                      "page": page, "pageSize": 50}, headers=h, timeout=15)
                j = r.json() or {}
            except Exception:
                break
            d = j.get("data")
            lst = d if isinstance(d, list) else ((d or {}).get("list") or (d or {}).get("records") or [])
            if not lst:
                break
            for x in lst:
                got.append({"actId": x.get("actId"), "name": (x.get("activityName") or "")[:80],
                            "create": x.get("createTime") or 0,
                            "status": x.get("activityStatus")})
            if len(lst) < 50:
                break
        return str(act), got

    uniq = sorted({r["actId"] for r in rows})
    say(f"查 {len(uniq)} 个母团的子团记录（汤团号）…")
    res, done = {}, 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(one, a) for a in uniq]
        for f in as_completed(futs):
            k, v = f.result()
            res[k] = v
            done += 1
            if done % 100 == 0:
                say(f"  {done}/{len(uniq)}")
    hit = sum(1 for v in res.values() if v)
    say(f"有子团记录的母团 {hit} / {len(uniq)}")
    json.dump({"at": time.time(), "data": res}, open(CACHE_CHILD, "w"), ensure_ascii=False)
    return res


# ---------------------------------------------------------------- 4. 主页团
def refresh_own(cfg=None):
    """主页团（含 createTime / endTime），只保留最近 10 天够用（最多翻 5 页）。"""
    if not cfg:
        cfg = json.load(open(os.path.join(DATA, "config.json")))
    gh = cfg.get("gh_id") or TANG_GH
    cfg["gh_id"] = gh
    h = core.make_headers(cfg)
    s = core.make_session()
    acts, page = [], 0
    cutoff = int(time.time() * 1000) - 10 * 86400000
    for p in range(1, 6):
        try:
            r = s.post(f"{core.BASE_URL}/ghome-feed/ghome_feed/query_ghome_feed_back_v2",
                       json={"page": p, "pageSize": 20, "ghId": gh,
                             "keyword": "", "ghType": 20, "sortRule": 10}, headers=h, timeout=10)
            d = (r.json() or {}).get("data") or {}
            lst = d.get("feedItemDTOList") or []
            if not lst:
                break
            page = p
            old_count = 0
            for x in lst:
                a = x.get("actItemDTO") or {}
                if not a.get("actId"):
                    continue
                ct = a.get("createTime") or 0
                acts.append({"actId": a["actId"], "name": a.get("activityName") or "",
                             "status": a.get("activityStatus"), "pause": a.get("pauseSw"),
                             "create": ct, "end": a.get("endTime") or 0,
                             "type": a.get("activityType")})
                if ct < cutoff and not x.get("stickyStatus"):
                    old_count += 1
            if not d.get("hasNextPage") or old_count >= 15:
                break
        except Exception:
            break
    # 去重（主页会因置顶/翻页重复）
    seen, uniq = set(), []
    for a in acts:
        if a["actId"] in seen:
            continue
        seen.add(a["actId"])
        uniq.append(a)
    json.dump(uniq, open(CACHE_OWN, "w"), ensure_ascii=False)
    say(f"主页团 {len(uniq)} 个（翻了 {page} 页）")
    return uniq


# ---------------------------------------------------------------- 5. 名字归一
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200b\u200c\u200d]")


def norm(s):
    s = _EMOJI.sub("", (s or "").lower())
    s = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", s)
    return s


def name_same(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    if a[:20] == b[:20] and len(a[:20]) >= 8:
        return True
    n = min(len(a), len(b), 24)
    if n >= 10 and a[:n] == b[:n]:
        return True
    if len(a) >= 12 and a[:16] in b:
        return True
    if len(b) >= 12 and b[:16] in a:
        return True
    return False


# ---------------------------------------------------------------- 6. 判定
def judge(rows, children, own):
    now = int(time.time() * 1000)
    cutoff = now - WINDOW_DAYS * 86400000
    live_own = [a for a in own if a["status"] == 10 and a.get("pause") == 5]

    # ── A. 帮卖：子团记录 ──
    # 只看「5 天内创建过」。不能拿「有在售子团」判 —— 这个号本来就是把同一个母团
    # 每 7~20 天重开一次，371/620 条都挂着在售子团，按在售判会一条都开不出来。
    for r in rows:
        kids = children.get(str(r["actId"])) or []
        young = [k for k in kids if (k.get("create") or 0) >= cutoff]
        if young:
            k = max(young, key=lambda x: x.get("create") or 0)
            r["_why"] = "5天内已开"
            r["_ev"] = f"子团 {k['actId']}（{datetime.fromtimestamp((k['create'] or 0)/1000):%m-%d %H:%M}）"
            r["_mine"] = k["actId"]

    # ── B. 跟团（以及帮卖兜底）：主页团名 ──
    recent_own = [a for a in own if (a.get("create") or 0) >= cutoff]
    for r in rows:
        if r.get("_why"):
            continue
        hit = next((a for a in recent_own if name_same(r.get("actName") or r["name"], a["name"])), None)
        if hit:
            r["_why"] = "5天内已开"
            r["_ev"] = f"主页团 {hit['actId']}（{datetime.fromtimestamp((hit['create'] or 0)/1000):%m-%d %H:%M}）"
            r["_mine"] = hit["actId"]

    # ── C. 同款：620 条之间（含跨表） ──
    items = []
    for i, r in enumerate(rows):
        goods = [{"goodsId": g} for g in (r.get("goodsIds") or [])]
        goods += [{"goodsName": n} for n in (r.get("goodsNames") or []) if n]
        if not goods:
            goods = [{"goodsName": r.get("actName") or r["name"]}]
        items.append({
            "actId": r["actId"], "actName": r.get("actName") or r["name"],
            "startActId": r.get("startActId") or 0,
            "actStat": {"totalOrderNum": r["orders"]},
            "basicFeedGoodDTOList": goods,
            "_i": i,
        })
    groups = core._goods_groups(items)
    dup = {}
    for g in groups:
        if len(g) < 2:
            continue
        keep = max(g, key=lambda x: x.get("actStat", {}).get("totalOrderNum") or 0)
        for x in g:
            if x["_i"] == keep["_i"]:
                continue
            other = rows[keep["_i"]]
            dup[x["_i"]] = f"与「{other['table']}·{other['supplier'][:8]}·{other['name'][:16]}」同款，留订单高的那条"
    for i, r in enumerate(rows):
        if r.get("_why") or i not in dup:
            continue
        r["_why"] = "重复"
        r["_ev"] = dup[i]

    out = {}
    for r in rows:
        out[str(r["actId"])] = {
            "table": r["table"], "name": r["name"][:60], "supplier": r["supplier"],
            "orders": r["orders"],
            "open": not r.get("_why"),
            "why": r.get("_why") or "可开",
            "evidence": r.get("_ev") or "",
            "mineActId": r.get("_mine"),
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="不联网，用已抓的子团记录")
    ap.add_argument("--no-own", action="store_true", help="不刷新主页（用本地缓存）")
    args = ap.parse_args()

    rows = load_rows()
    say(f"两份表共 {len(rows)} 条（跟团 {sum(1 for r in rows if r['table']=='跟团')} / "
        f"帮卖 {sum(1 for r in rows if r['table']=='帮卖')}）")
    rows = fill_goods(rows)

    children = {} if args.offline else fetch_children(rows)
    own = json.load(open(CACHE_OWN)) if (args.offline or args.no_own) and os.path.exists(CACHE_OWN) else refresh_own()
    say(f"主页团 {len(own)} 个，其中近 {WINDOW_DAYS} 天创建 "
        f"{sum(1 for a in own if (a.get('create') or 0) >= int(time.time()*1000) - WINDOW_DAYS*86400000)} 个")

    res = judge(rows, children, own)
    json.dump(res, open(RESULT, "w"), ensure_ascii=False, indent=1)

    from collections import Counter
    for t in ("跟团", "帮卖"):
        sub = [v for v in res.values() if v["table"] == t]
        c = Counter(v["why"] for v in sub)
        say(f"{t} {len(sub)} 条 → " + " / ".join(f"{k} {v}" for k, v in c.most_common()))
    say(f"结果写入 {RESULT}")


if __name__ == "__main__":
    main()
