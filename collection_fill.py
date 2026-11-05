"""把「不容错过的双十一活动」合集改成「关键词搜出来的双十一团」。

池子 = query_ghome_feed_back_v2 服务端搜索「双十一 / 双11 / 1111」三词并集
     → 在售、群内订单 ≥20 → 同款合并（标题 90% / 图片≥5张 / 同一商品库，50% 占比阈值）
保留合集原有手工挑的那几条在最前面，其余按群内销量降序，总数封顶 200。

默认 dry-run，加 --apply 才写。
"""
import csv, json, sys, datetime
import requests, core

GH = "PspU4o9_bsxkBH0_6c6cef00"
COLLECTION_ID = 2098411
TARGET = 200
POOL = '/tmp/kw_winners.json'      # 由关键词搜索 + 去重得到（见 fetch_kw.py）
KEEP_FILE = 'data/collection_2098411_before_20261105.json'   # 原始手工 11 条

cfg = json.load(open('data/config.json'))
H = core.make_headers(cfg)
S = requests.Session(); S.trust_env = False; S.proxies.clear()


def main(apply=False):
    coll = S.post(core.BASE_URL + '/ghome-feed/act_collection/act_collection_query_by_id',
                  params={"actCollectionId": COLLECTION_ID}, headers=H, timeout=20).json()['data']
    cur_ids = list(coll['actIdList'])

    pool = [x for x in json.load(open(POOL))
            if (x['endTime'] or 0) > datetime.datetime.now().timestamp() * 1000
            and x.get('pauseSw') != 10 and x.get('activityStatus') in (10, None)]
    pool.sort(key=lambda x: (-x['orderCount'], -x['saleAmount']))

    # 手工挑的那几条（改之前的原始状态）继续留在最前面；但已暂停 / 已结束的照样剔掉
    keep_all = json.load(open(KEEP_FILE))['actIdList']
    keep, dropped_keep = [], []
    for i in range(0, len(keep_all), 50):
        b = keep_all[i:i + 50]
        lst = S.post(core.BASE_URL + '/ghome-feed/act_collection/act_collection_list_detail',
                     json={"actIds": [str(x) for x in b]}, headers=H, timeout=25).json().get('data') or []
        ok = {x['actId'] for x in lst
              if x.get('pauseSw') != 10 and x.get('activityStatus') in (10, None)}
        for aid in b:
            (keep if aid in ok else dropped_keep).append(aid)
    keep = keep[:TARGET]
    rest = [x for x in pool if x['actId'] not in keep]
    room = max(0, TARGET - len(keep))
    pick = rest[:room]
    final = keep + [x['actId'] for x in pick]

    if dropped_keep:
        print(f"⚠️ 原有 {len(dropped_keep)} 条已暂停/已结束，已剔除：{dropped_keep}")
    print(f"合集原 {len(cur_ids)} 条｜保留 {len(keep)}｜关键词池 {len(pool)} 条｜新增 {len(pick)} → 合计 {len(final)}")
    if pick:
        print(f"新增单量 {pick[-1]['orderCount']}~{pick[0]['orderCount']}，未进榜 {max(0, len(rest)-room)} 条")

    with open('合集补充清单_20261105.csv', 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['序号', 'actId', '标题', '群内订单数', '开团时间', '距今天数'])
        for i, x in enumerate(pick, 1):
            w.writerow([i, x['actId'], x['title'][:80], x['orderCount'],
                        datetime.datetime.fromtimestamp(x['createTime'] / 1000).strftime('%m-%d %H:%M'),
                        int((datetime.datetime.now().timestamp() * 1000 - x['createTime']) / 86400000)])

    if not apply:
        for x in pick[:6]:
            print(f"  {x['orderCount']:>5}单 {x['title'][:42]}")
        print("（dry-run，未写回）")
        return

    json.dump(coll, open('data/collection_2098411_before_kw_20261105.json', 'w'),
              ensure_ascii=False, indent=1)
    body = {"title": coll['title'], "subTitle": coll.get('subTitle') or "", "ghId": GH,
            "ghViewType": coll.get('ghViewType', 0), "extJson": coll['extJson'],
            "actIdList": final, "actCollectionId": COLLECTION_ID}
    r = S.post(core.BASE_URL + '/ghome-feed/act_collection/act_collection_modify',
               json=body, headers=H, timeout=60).json()
    print("写入:", r.get('code'), r.get('data'), r.get('msg'))
    chk = S.post(core.BASE_URL + '/ghome-feed/act_collection/act_collection_query_by_id',
                 params={"actCollectionId": COLLECTION_ID}, headers=H, timeout=20).json()['data']
    print("回读:", len(chk['actIdList']), "条 ｜", chk['title'])


if __name__ == '__main__':
    main(apply='--apply' in sys.argv)
