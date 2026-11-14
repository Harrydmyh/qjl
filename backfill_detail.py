"""把详情缓存里"空"的活动用 from=8 重查一遍（服务端入口级可见性过滤导致漏查）。

用法：python3 backfill_detail.py [并发数]
只改 data/weekly800_detail_cache.json，不联网抓新活动、不出表。
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import core
import weekly800 as W

CACHE = os.path.join(W.DATA, "weekly800_detail_cache.json")


def main():
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    cfg = json.load(open(os.path.join(W.DATA, "config.json")))
    blob = json.load(open(CACHE))
    data = blob["data"]
    todo = [a for a, v in data.items() if not v]
    print(f"[backfill] 缓存 {len(data)} 条，其中空 {len(todo)} 条，并发 {workers}", flush=True)

    headers = core.make_headers(cfg)
    t0 = time.time()
    done = got = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(W.fetch_one, cfg, core.make_session(), headers, a): a
                for a in todo}
        for f in as_completed(futs):
            a = futs[f]
            try:
                det = f.result()
            except Exception:
                det = None
            if det:
                data[a] = W.flatten(det)
                got += 1
            done += 1
            if done % 200 == 0 or done == len(todo):
                print(f"  {done}/{len(todo)} 新捞回 {got} 条 "
                      f"{time.time()-t0:.0f}s", flush=True)
                blob["data"] = data
                json.dump(blob, open(CACHE, "w"), ensure_ascii=False)

    blob["data"] = data
    json.dump(blob, open(CACHE, "w"), ensure_ascii=False)
    print(f"[backfill] 完成：重查 {done} 条，捞回 {got} 条，耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
