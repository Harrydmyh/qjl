"""把群主页近 N 天开的团抓全（并发分页），缓存到 /tmp/feed_7d.json。只读。"""
import json, datetime, sys
from concurrent.futures import ThreadPoolExecutor
import requests, core

GH = "PspU4o9_bsxkBH0_6c6cef00"
DAYS = 7
PAGE_SIZE = 20
c = json.load(open('data/config.json'))
H = core.make_headers(c)
NOW = datetime.datetime.now().timestamp() * 1000
CUTOFF = NOW - DAYS * 86400_000


def page(p):
    s = requests.Session(); s.trust_env = False; s.proxies.clear()
    body = {"page": p, "pageSize": PAGE_SIZE, "ghId": GH, "ghType": 105,
            "activityStatus": 10, "requireGoodsDTOList": True, "uvOrderCountForC": True}
    d = s.post(core.BASE_URL + '/ghome-feed/ghome_feed/query_ghome_feed_back_v2',
               json=body, headers=H, timeout=25).json()
    dd = d.get('data') or {}
    return p, dd.get('feedItemDTOList') or [], bool(dd.get('hasNextPage'))


def main():
    got, p = {}, 1
    stop = False
    while not stop and p < 400:
        batch = list(range(p, p + 8))
        with ThreadPoolExecutor(max_workers=8) as ex:
            for pg, lst, hn in ex.map(page, batch):
                got[pg] = lst
                if not hn or not lst:
                    stop = True
        # 本批最末尾的页如果已经早于边界，停
        last = got.get(batch[-1]) or []
        if last:
            tail = [x['actItemDTO'].get('createTime') or 0 for x in last[-5:]]
            if max(tail) < CUTOFF:
                stop = True
        print(f"已抓 {p}-{batch[-1]} 页，累计 {sum(len(v) for v in got.values())} 条", flush=True)
        p += 8

    rows = [x for pg in sorted(got) for x in got[pg]]
    # 去重 actId（并发下可能有重叠/乱序）
    seen, out = set(), []
    for x in rows:
        aid = x['actItemDTO']['actId']
        if aid in seen:
            continue
        seen.add(aid); out.append(x)
    win = [x for x in out if (x['actItemDTO'].get('createTime') or 0) >= CUTOFF]
    print(f"总抓取 {len(out)}，近{DAYS}天 {len(win)}", flush=True)
    json.dump(win, open('/tmp/feed_7d.json', 'w'), ensure_ascii=False)


if __name__ == '__main__':
    main()
