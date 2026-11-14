#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查「汤团号」自己主页上是否已经有这些团（同名 / 仍在售）。

开团前必须先查这个：202/234 条 isFollow=True，说明平台记录里这号已经跟过，
再开一遍就是重复团。只有「没有在售同名团」的才该开。
"""
import json
import os
import re

import core

ROOT = os.path.dirname(os.path.abspath(__file__))
TANG = "PspU4o9_bsxkBH0_6c6cef00"


def fetch_mine(cfg, headers, max_pages=80):
    s = core.make_session()
    out = []
    for p in range(1, max_pages + 1):
        r = s.post(f"{core.BASE_URL}/ghome-feed/ghome_feed/query_ghome_feed_back_v2",
                   json={"page": p, "pageSize": 20, "ghId": cfg["gh_id"],
                         "keyword": "", "ghType": 20, "sortRule": 10},
                   headers=headers, timeout=20)
        d = (r.json() or {}).get("data") or {}
        lst = d.get("feedItemDTOList") or []
        if not lst:
            break
        for x in lst:
            a = x.get("actItemDTO") or {}
            if a.get("actId"):
                out.append({"actId": a["actId"], "name": a.get("activityName") or "",
                            "status": a.get("activityStatus"), "pause": a.get("pauseSw"),
                            "create": a.get("createTime"), "type": a.get("activityType")})
        print(f"  主页第 {p} 页，累计 {len(out)}", flush=True)
        if not d.get("hasNextPage"):
            break
    return out


def norm(t):
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", (t or "").lower())[:40]


def main():
    cfg = json.load(open(os.path.join(ROOT, "data", "config.json")))
    cfg["gh_id"] = TANG
    headers = core.make_headers(cfg)
    mine = fetch_mine(cfg, headers)
    json.dump(mine, open(os.path.join(ROOT, "data", "tang_own_acts.json"), "w"),
              ensure_ascii=False)
    bys = {}
    for m in mine:
        bys.setdefault(norm(m["name"]), []).append(m)

    det = json.load(open(os.path.join(ROOT, "data", "bs234_detail_tang.json")))
    res, live = {}, 0
    for k, v in det.items():
        if not v:
            continue
        ms = bys.get(norm(v["name"])) or []
        is_live = any(m["status"] == 10 and m["pause"] == 5 for m in ms)
        live += 1 if is_live else 0
        res[k] = {"hasSameName": bool(ms), "live": is_live,
                  "mine": [m["actId"] for m in ms]}
    json.dump(res, open(os.path.join(ROOT, "data", "bs234_mine_check.json"), "w"),
              ensure_ascii=False)
    print(f"[check] 主页团 {len(mine)} 个")
    print(f"[check] 已有同名团 {sum(1 for v in res.values() if v['hasSameName'])} / {len(res)}")
    print(f"[check] 其中仍在售 {live}，需要新开 {len(res) - live}")


if __name__ == "__main__":
    main()
