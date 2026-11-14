#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查一条（或多条）活动「现在是不是在线」。

「在线」= 用户点开链接能看到、能下单。平台没有单一的 isOnline 字段，
只能三样一起看：

  1. **主页 feed 里有没有它** —— 这是「对用户可见」最硬的判据。
     接口不给你的团，别人也刷不到（未上架 / 审核中 / 已删除）。
  2. activityStatus == 10 —— 进行中；其它值是已结束/已删除一类。
  3. pauseSw == 5 —— 未暂停售卖；别急着当成布尔，平台这里是 5/10 这种枚举。
  外加 startTime < 现在 < endTime 才算真的能卖。

用法：
  python3 check_act.py 2509183505819756
  python3 check_act.py 2509183505819756 2601282967879353
  python3 check_act.py --recent 5          # 查 open_plan 里最近开的 5 条
  python3 check_act.py --pages 20 2509183505819756   # feed 多翻几页再判

退出码：有任一条不在线 → 1；全部在线 → 0（方便 cron / 脚本判断）。
"""
import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
TANG_GH = "PspU4o9_bsxkBH0_6c6cef00"
GH_NAMES = {"PspU4o9_bsxkBH0_6c6cef00": "汤团",
            "PsHHJN8_bsMtSbS_b09f0984": "汤团²"}


def say(m):
    print(f"[{datetime.now():%m-%d %H:%M}] {m}", flush=True)


def _ts(ms):
    return datetime.fromtimestamp(ms / 1000) if ms else None


def feed_scan(headers, session, gh_id, pages):
    """翻主页 feed，返回 {actId: actItemDTO}。近期的团都在最新几页。"""
    found = {}
    for p in range(1, pages + 1):
        try:
            r = session.post(
                f"{core.BASE_URL}/ghome-feed/ghome_feed/query_ghome_feed_back_v2",
                json={"page": p, "pageSize": 20, "ghId": gh_id,
                      "keyword": "", "ghType": 20, "sortRule": 10},
                headers=headers, timeout=20)
            d = (r.json() or {}).get("data") or {}
        except Exception as e:
            say(f"⚠️ feed 第 {p} 页失败（{e}）")
            break
        lst = d.get("feedItemDTOList") or []
        for x in lst:
            a = x.get("actItemDTO") or {}
            if a.get("actId"):
                found.setdefault(a["actId"], a)
        if not lst or not d.get("hasNextPage"):
            break
    return found


def judge(a, now_ms):
    """返回 (是否在线, 说明)。"""
    if not a:
        return False, "主页 feed 里找不到 → 别人也刷不到（未上架/审核中/已删）"
    st, pause = a.get("activityStatus"), a.get("pauseSw")
    s, e = a.get("startTime") or 0, a.get("endTime") or 0
    bad = []
    if st != 10:
        bad.append(f"activityStatus={st}（不是 10 进行中）")
    if pause != 5:
        bad.append(f"pauseSw={pause}（不是 5 未暂停）")
    if s and now_ms < s:
        bad.append(f"还没开始（{_ts(s):%m-%d %H:%M} 才开）")
    if e and now_ms > e:
        bad.append(f"已过结束时间（{_ts(e):%m-%d %H:%M}）")
    if bad:
        return False, "；".join(bad)
    return True, "进行中、未暂停、在售时间窗内"


def check_one(aid, heads, session, feed, now_ms, cfg):
    a = feed.get(int(aid))
    online, why = judge(a, now_ms)
    name = (a or {}).get("activityName") or ""
    print(f"\n{'✅ 在线' if online else '❌ 不在线'}  {aid}   {why}")
    if a:
        fid = a.get("feedGroupSaleChildActDTO") or {}
        root = (fid.get("groupSalerRootActInfoDTO") or {})
        print(f"   标题：{name[:60]}")
        print(f"   团长：{a.get('nickName')}   母团：{root.get('actId')} {root.get('ghName') or ''}")
        print(f"   类型：activityType={a.get('activityType')}   价格：{fid.get('priceTips')}   订单：{fid.get('orderCount')}")
        print(f"   起止：{_ts(a.get('startTime')):%Y-%m-%d %H:%M} → {_ts(a.get('endTime')):%Y-%m-%d %H:%M}")
        print(f"   可见：visibleModel={a.get('visibleModel')}  ghViewType={a.get('ghViewType')}")
    else:
        # feed 没有不等于不存在：批量详情再探一次，区分「存在但不可见」和「压根没有」
        try:
            got = core._fetch_details_batch(session, heads, TANG_GH, [int(aid)])
        except Exception:
            got = []
        if got:
            g = got[0]
            print(f"   但详情接口能查到：{(g.get('activityName') or '')[:50]}  activityStatus={g.get('activityStatus')}")
            print("   → 活动存在，只是没出现在你的主页：多为未上架/审核中/被隐藏")
        else:
            print("   详情接口也查不到 → 这个 actId 大概率不存在或无权访问")
    print(f"   链接：https://www.qunjielong.com/#/dashboard/{aid}")
    return online


def recent_ids(n):
    """open_plan 里最近开的 n 条（按 doneAt 倒序）。"""
    p = os.path.join(DATA, "open_plan_tang.json")
    try:
        plan = json.load(open(p))
    except Exception:
        return []
    done = [x for x in plan.get("items", []) if x.get("newActId")]
    done.sort(key=lambda x: x.get("doneAt") or "", reverse=True)
    return [int(x["newActId"]) for x in done[:n]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("actIds", nargs="*", type=int, help="要查的活动 id")
    ap.add_argument("--recent", type=int, default=0, help="查 open_plan 里最近开的 N 条")
    ap.add_argument("--pages", type=int, default=8, help="主页 feed 翻几页（每页 20）")
    ap.add_argument("--gh", default=TANG_GH, help="用哪个号看（默认汤团）")
    args = ap.parse_args()

    ids = list(args.actIds)
    if args.recent:
        ids = recent_ids(args.recent) + ids
    if not ids:
        ap.error("给个 actId，或者用 --recent 3")

    cfg = json.load(open(os.path.join(DATA, "config.json")))
    cfg["gh_id"] = args.gh
    ti = core.token_info(cfg)
    if not ti.get("valid"):
        say(f"❌ token 已过期（{ti.get('exp_str')}），先重新抓包写回 data/config.json")
        return 2

    heads = core.make_headers(cfg)
    session = core.make_session()
    say(f"用「{GH_NAMES.get(args.gh, args.gh)}」的主页看，翻 {args.pages} 页…")
    feed = feed_scan(heads, session, args.gh, args.pages)
    say(f"主页拿到 {len(feed)} 条团")

    now_ms = int(datetime.now().timestamp() * 1000)
    oks = [check_one(a, heads, session, feed, now_ms, cfg) for a in ids]
    on = sum(1 for x in oks if x)
    print(f"\n=== {on}/{len(ids)} 条在线")
    return 0 if on == len(ids) else 1


if __name__ == "__main__":
    sys.exit(main())
