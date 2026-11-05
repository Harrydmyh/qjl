"""关键词搜出「双十一」类活动 → 筛选 + 同款合并 → /tmp/kw_winners.json

池子来源：query_ghome_feed_back_v2 带 keyword（服务端搜索，覆盖标题与正文）。
实测 2026-11-05：双十一 251 条、双11 287 条、1111 仅 2 条（几乎没有活动用纯数字）。

筛选：在售（endTime 未过）+ 群内订单 ≥20
合并：标题相似 ≥90% / 图片 ≥5 张相同 / 同一商品库（商品名占少方 ≥50%，防换购小件误并）
"""
import json, sys, datetime, difflib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import requests, core

GH = "PspU4o9_bsxkBH0_6c6cef00"
KEYWORDS = ["双十一", "双11", "1111"]
MIN_ORDERS = 20
OUT = '/tmp/kw_winners.json'

cfg = json.load(open('data/config.json'))
H = core.make_headers(cfg)
NOW = datetime.datetime.now().timestamp() * 1000


def fetch_page(kw, p):
    s = requests.Session(); s.trust_env = False; s.proxies.clear()
    body = {"page": p, "pageSize": 20, "ghId": GH, "ghType": 105, "keyword": kw,
            "activityStatus": 10, "requireGoodsDTOList": True, "uvOrderCountForC": True}
    d = s.post(core.BASE_URL + '/ghome-feed/ghome_feed/query_ghome_feed_back_v2',
               json=body, headers=H, timeout=25).json()
    dd = d.get('data') or {}
    return p, dd.get('feedItemDTOList') or [], bool(dd.get('hasNextPage'))


def fetch_kw(kw, max_pages=60):
    got, p = {}, 1
    while p < max_pages:
        rng = list(range(p, p + 6))
        with ThreadPoolExecutor(max_workers=6) as ex:
            for pg, lst, hn in ex.map(lambda x: fetch_page(kw, x), rng):
                got[pg] = (lst, hn)
        if not any(got.get(pg, ([], False))[1] for pg in rng):
            break
        p += 6
    rows = [x for pg in sorted(got) for x in got[pg][0]]
    return {x['actItemDTO']['actId']: x for x in rows}


def pk(p):
    return (p or '').strip().lstrip('/')


def shape(x):
    a = x['actItemDTO']; st = a.get('feedGroupSalerRootActDTO') or {}
    pics = {pk(p) for p in (a.get('picList') or []) if p}
    names = set()
    for g in (a.get('goodsDTOList') or []):
        for p in (g.get('goodsPicList') or []):
            if p:
                pics.add(pk(p))
        if g.get('goodsName'):
            names.add(core._norm_name(g['goodsName']))
    return dict(actId=a['actId'], title=a.get('activityName') or '',
                ntitle=core._norm_name(a.get('activityName') or ''), pics=pics,
                goodsNames=names, orderCount=st.get('orderCount') or 0,
                saleAmount=st.get('saleAmount') or 0,
                createTime=a.get('createTime') or 0, endTime=a.get('endTime') or 0,
                # pauseSw: 5=正常，10=已暂停（暂停的不能进合集，点进去是死的）
                pauseSw=a.get('pauseSw'), activityStatus=a.get('activityStatus'),
                activityType=a.get('activityType'))


def dedup(cand):
    n = len(cand); parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    by_pic = defaultdict(list)
    for i, it in enumerate(cand):
        for p in it['pics']:
            by_pic[p].append(i)
    for i, it in enumerate(cand):
        hit = defaultdict(int)
        for p in it['pics']:
            for j in by_pic[p]:
                if j > i:
                    hit[j] += 1
        for j, k in hit.items():
            if k >= 5:
                union(i, j)

    for i, j in core._containment_pairs([list(x['goodsNames']) for x in cand]):
        if find(i) != find(j) and core._names_same(list(cand[i]['goodsNames']), list(cand[j]['goodsNames'])):
            union(i, j)

    for i in range(n):
        a = cand[i]['ntitle']
        if not a:
            continue
        for j in range(i + 1, n):
            if find(i) == find(j):
                continue
            b = cand[j]['ntitle']
            if not b or abs(len(a) - len(b)) > 0.12 * max(len(a), len(b)):
                continue
            sm = difflib.SequenceMatcher(None, a, b)
            if sm.real_quick_ratio() >= 0.9 and sm.quick_ratio() >= 0.9 and sm.ratio() >= 0.9:
                union(i, j)

    gs = defaultdict(list)
    for i, it in enumerate(cand):
        gs[find(i)].append(it)
    return [max(g, key=lambda x: (x['orderCount'], x['saleAmount'])) for g in gs.values()]


def main():
    seen = {}
    for kw in KEYWORDS:
        got = fetch_kw(kw)
        print(f"{kw}: {len(got)} 条", flush=True)
        for k, v in got.items():
            seen.setdefault(k, v)
    items = [shape(x) for x in seen.values()]
    alive = [i for i in items if (i['endTime'] or 0) > NOW]
    # 状态闸门：activityStatus 必须 10（在售）；pauseSw 必须不是 10（10=已暂停）
    ok = [i for i in alive if i['activityStatus'] in (10, None) and i['pauseSw'] != 10]
    paused = len(alive) - len(ok)
    cand = [i for i in ok if i['orderCount'] >= MIN_ORDERS]
    win = dedup(cand)
    win.sort(key=lambda x: (-x['orderCount'], -x['saleAmount']))
    print(f"三词并集 {len(items)}｜在售 {len(alive)}｜剔暂停 {paused}｜≥{MIN_ORDERS}单 {len(cand)}｜同款合并后 {len(win)}")
    json.dump([{k: (sorted(v) if isinstance(v, set) else v) for k, v in x.items()} for x in win],
              open(OUT, 'w'), ensure_ascii=False)
    print("→", OUT)


if __name__ == '__main__':
    main()
