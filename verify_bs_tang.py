#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐条核对「帮卖模式」表里的链接在**汤团号**下的可见性/已开状态。

为什么要单独存一份：open 之前必须知道
  · isFollow=True  → 这个号已经帮卖过，再开就是重复团
  · activityType=10 → 没有帮卖权限，开了必报 5830
  · followEarning 为空 → 上游没开帮卖
批量接口会串号，所以这里**逐条单查**。
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import core

ROOT = os.path.dirname(os.path.abspath(__file__))
TANG = "PspU4o9_bsxkBH0_6c6cef00"


def one(cfg, headers, act_id):
    for fv in (8, 6):
        s = core.make_session()
        try:
            out = core._post_detail_batch(s, headers, cfg["gh_id"], [int(act_id)], fromv=fv)
        except Exception:
            out = []
        if out:
            return act_id, out[0]
    return act_id, None


def main():
    import openpyxl
    cfg = json.load(open(os.path.join(ROOT, "data", "config.json")))
    cfg["gh_id"] = TANG
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        ROOT, "周报", "汤团_帮卖模式_800单以上_20261114.xlsx")
    out_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        ROOT, "data", "bs234_detail_tang.json")

    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[wb.sheetnames[0]]
    head = [c.value for c in ws[1]]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    l2a = {l: a for a, l in json.load(
        open(os.path.join(ROOT, "data", "short_link_cache.json"))).items()}
    ci = head.index("可点击链接")
    aids = [int(l2a[r[ci]]) for r in rows if l2a.get(r[ci])]
    print(f"[verify] {len(aids)} 条，逐条查（汤团号）", flush=True)

    headers = core.make_headers(cfg)
    res = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(one, cfg, headers, a) for a in aids]
        for f in as_completed(futs):
            a, d = f.result()
            if not d:
                res[str(a)] = None
                continue
            g = d.get("basicFeedGoodDTOList") or []
            res[str(a)] = {
                "actId": a,
                "name": (d.get("actName") or "")[:60],
                "goodsName": (g[0].get("goodsName") if g else "") or "",
                "ghName": d.get("ghName"),
                "activityType": d.get("activityType"),
                "isFollow": d.get("isFollow"),
                "hadHelpSale": d.get("hadHelpSale"),
                "followEarning": bool(g and g[0].get("followEarning")),
                "orders": (d.get("actStat") or {}).get("totalOrderNum"),
                "pauseSw": d.get("pauseSw"),
                "activityStatus": d.get("activityStatus"),
                "startActId": d.get("startActId"),
            }
    json.dump(res, open(out_path, "w"), ensure_ascii=False)
    ok = [v for v in res.values() if v]
    print(f"[verify] 有详情 {len(ok)} / 共 {len(res)}")
    print(f"  已帮卖过(isFollow) {sum(1 for v in ok if v['isFollow'])}")
    print(f"  activityType=10   {sum(1 for v in ok if v['activityType'] == 10)}")
    print(f"  followEarning 空  {sum(1 for v in ok if not v['followEarning'])}")
    print(f"  已暂停/已结束     {sum(1 for v in ok if v['pauseSw'] != 5 or v['activityStatus'] != 10)}")


if __name__ == "__main__":
    main()
