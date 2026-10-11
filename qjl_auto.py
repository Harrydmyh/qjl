#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qjl_auto.py —— 一键帮卖：用「新号」的凭证，把「汤团」能帮卖的团全开一遍

背景（2026-10-11 逆向出来的，别丢）：
  · 群接龙有两套帮卖模式，本脚本走的是**帮卖子团**：
      for_leader/{actId}/{ghId}  +  POST /activity-biz/help_sale_child/act/release
  · activityType=10 的团没有帮卖权限（平台报 5830「您没有帮卖权限」），会被自动跳过
  · 候选不在帮卖广场（新号搜不到），在推荐池：
      POST /ghome-feed/act_recommend/get_recommend_acts  种子必须是「自己主页里的团」
  · 自己已开过的团会出现在推荐池里 → 必须先拉自己主页 feed 排除掉，否则重复开

用法
  python3 qjl_auto.py --dry-run                  # 只看会开哪些，不开（先跑这个）
  python3 qjl_auto.py --go                       # 真跑（开团前打印清单并等 5 秒，可 Ctrl+C）
  python3 qjl_auto.py --go --yes                 # 不等确认
  python3 qjl_auto.py --chlz ~/Downloads/New.chlz   # 从抓包提取凭证写进 data/config.json
  python3 qjl_auto.py --go --gh tang             # 只开汤团的（默认）
  python3 qjl_auto.py --go --gh all              # 不筛来源，能开的全开
  python3 qjl_auto.py --go --delay 5 --limit 3   # 间隔 5 秒、最多开 3 个

结果写到 data/auto_open_log.json（不动工具原本的 open_log.json）
"""

import argparse
import base64
import datetime
import glob
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "data", "config.json")
AUTO_LOG = os.path.join(ROOT, "data", "auto_open_log.json")

# 汤团的两个主页（Claw 抓包里拿到的 ghId）
TANG_GH = {
    "PspU4o9_bsxkBH0_6c6cef00": "汤团",
    "PsHHJN8_bsMtSbS_b09f0984": "汤团²",
}

# 帮卖子团：成功样本的 settings，用来补齐模板缺失的键
# （缺 showAllGroupOrder 会报 5259「非法的展示所有同源父级的接龙设置」）
SETTINGS_FILL = {
    "showAllGroupOrder": 10, "richTextShowMode": 10, "productShowVisibleFlag": 10,
    "syncRestartSw": 5, "syncRootTypes": {"10": 10, "20": 10, "30": 10, "40": 5},
    "syncRootContextTypes": [10, 20, 30], "actRecommendTypes": [],
    "generalSettlingModel": 10, "promoterSw": 5, "gactSw": 5, "marketGoodsSw": 5,
    "marketShopPhoneSw": 5, "childExchangeSwitch": 5, "returnAddrType": 0,
    "recommendActIds": [], "showGoodsStockToCustomer": 5, "allowSignIn": 5,
    "exchangeSwitch": None,
}


# ────────────────────────── 凭证 ──────────────────────────

def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            return json.loads(open(CONFIG_PATH, encoding="utf-8").read())
        except Exception:
            pass
    return {"auth_token": "", "gh_id": "", "uid": ""}


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def token_status(token):
    """→ (uid, 到期str, 剩余秒数)"""
    try:
        p = token.split(".")[1]
        p += "=" * (-len(p) % 4)
        pl = json.loads(base64.urlsafe_b64decode(p))
        if "exp" not in pl:
            return pl.get("uid"), "未知", None
        exp = datetime.datetime.fromtimestamp(pl["exp"])
        return pl.get("uid"), exp.strftime("%Y-%m-%d %H:%M:%S"), \
            (exp - datetime.datetime.now()).total_seconds()
    except Exception:
        return None, "解析失败", None


def extract_from_chlz(path):
    """从 Charles 导出(.chlz 是 zip)里抠 apipro 的 authorization / uid / ghId"""
    tmp = tempfile.mkdtemp(prefix="chlz_")
    try:
        with zipfile.ZipFile(path) as z:
            z.extractall(tmp)
        metas = sorted(glob.glob(os.path.join(tmp, "*-meta.json")),
                       key=lambda x: int(os.path.basename(x).split("-")[0]))
        token = uid = None
        ghs = {}
        for m in metas:
            try:
                d = json.loads(open(m, encoding="utf-8").read())
            except Exception:
                continue
            if "apipro.qunjielong.com" not in (d.get("host") or ""):
                continue
            for h in ((d.get("request", {}) or {}).get("header", {}).get("headers", []) or []):
                n = (h.get("name") or "").lower()
                v = str(h.get("value") or "").strip()
                if n == "authorization" and v and not token:
                    token = v
                elif n == "uid" and v and not uid:
                    uid = v
            for g in re.findall(r"Ps[A-Za-z0-9_]{10,}", json.dumps(d, ensure_ascii=False)):
                ghs[g] = ghs.get(g, 0) + 1
        if not token:
            raise SystemExit("  抓包里没有 apipro 的 authorization 头（是不是抓错服务了？）")
        return token, uid, (max(ghs, key=ghs.get) if ghs else None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ────────────────────────── 候选 ──────────────────────────

def my_home_acts(cfg, pages=6):
    """自己主页的团（已开过的）——用来排除，避免重复帮卖"""
    s = core.make_session()
    h = core.make_headers(cfg)
    gh = cfg["gh_id"]
    ids, names = set(), {}
    for p in range(1, pages + 1):
        try:
            r = s.post(f"{core.BASE_URL}/ghome-feed/ghome_feed/query_ghome_feed_back_v2",
                       json={"page": p, "pageSize": 20, "ghId": gh, "keyword": "",
                             "ghType": 20, "sortRule": 10}, headers=h, timeout=20)
            d = (r.json() or {}).get("data") or {}
        except Exception:
            break
        lst = d.get("feedItemDTOList") or []
        if not lst:
            break
        for x in lst:
            a = x.get("actItemDTO") or {}
            if a.get("actId"):
                ids.add(a["actId"])
                names[a["actId"]] = str(a.get("activityName") or "")[:40]
        if not d.get("hasNextPage"):
            break
    return ids, names


def recommend_pool(cfg, seeds, rounds=4):
    """推荐池：种子必须是自己主页的团，迭代到收敛"""
    s = core.make_session()
    h = core.make_headers(cfg)
    url = f"{core.BASE_URL}/ghome-feed/act_recommend/get_recommend_acts"
    seen, frontier = {}, list(seeds)[:3]
    for _ in range(rounds):
        nxt = []
        for sd in frontier:
            try:
                d = s.post(url, json={"actId": sd, "orderSuccessPage": False},
                           headers=h, timeout=20).json()
            except Exception:
                continue
            for x in d.get("data") or []:
                aid = x.get("actId")
                if aid and aid not in seen:
                    seen[aid] = str(x.get("actName") or "")[:44]
                    nxt.append(aid)
        if not nxt:
            break
        frontier = nxt[:8]
    return seen


def probe(cfg, act_id):
    """取模板，返回 (ok, ghId, activityType, name, err)"""
    s = core.make_session()
    h = core.make_headers(cfg)
    try:
        r = s.get(f"{core.BASE_URL}/activity-biz/help_sale_root/act/for_leader/{act_id}/{cfg['gh_id']}",
                  headers=h, timeout=20)
        d = r.json() or {}
        D = d.get("data") or {}
        if not D.get("actId"):
            return False, None, None, "", d.get("msg") or "无模板"
        ad = D.get("activityDTO") or {}
        return True, ad.get("ghId"), ad.get("activityType"), \
            str(ad.get("activityName") or "")[:44], ""
    except Exception as e:
        return False, None, None, "", type(e).__name__


def get_phone(cfg):
    s = core.make_session()
    h = core.make_headers(cfg)
    try:
        r = s.get(f"{core.BASE_URL}/activity-accessory/activity_input/get_user_input",
                  params={"ghId": cfg["gh_id"]}, headers=h, timeout=15)
        for it in ((r.json() or {}).get("data") or {}).get("inputMap", {}).get("210") or []:
            try:
                v = json.loads(it)
                if v.get("phone"):
                    return v["phone"]
            except Exception:
                pass
    except Exception:
        pass
    return ""


# ────────────────────────── 开团 ──────────────────────────

def build_payload(D, gh, phone):
    ad = D.get("activityDTO") or {}
    ai = D.get("activityInfoDTO") or {}
    asd = dict(D.get("activitySettingsDTO") or {})
    for k, v in SETTINGS_FILL.items():
        if k not in asd:
            asd[k] = v
    goods = []
    for g in D.get("activityGoodsList") or []:
        skus = (g.get("mutiActGoodsInfoDTO") or {}).get("actGoodsSkuPropertyDTOS") or []
        goods.append({
            "actGoodsId": g.get("actGoodsId"),
            "dimensionModel": g.get("dimensionModel"),
            "groupBuyPrice": g.get("actGroupBuyPrice"),
            "mutiGoodsParamList": [{"actGoodsId": x.get("actGoodsId"),
                                    "groupBuyPrice": x.get("groupBuyPrice"),
                                    "totalStock": x.get("totalStock"),
                                    "evaluationPrice": 0} for x in skus],
            "notDiscount": g.get("notDiscount"), "actGoodsExtStatus": 10,
        })
    logi = [{"logisticsModel": x.get("logisticsModel"),
             "actLogisticsAttrs": [{"attrType": a.get("attrType"), "attrName": a.get("attrName")}
                                   for a in (x.get("actLogisticsAttrs") or [])]}
            for x in (D.get("actLogisticsInfos") or [])]
    return {
        "pactId": D.get("actId"), "ghGroupId": gh,
        "addActivityParam": {"isDoubtSubmit": False, "activityName": ad.get("activityName"),
                             "startTime": ad.get("startTime"), "endTime": ad.get("endTime"),
                             "displayType": ad.get("displayType"),
                             "activityStatus": 10, "ghViewType": 20},
        "activityInfoParam": {"activityDetail": ai.get("activityDetail"),
                              "bgImgId": ai.get("bgImgId"), "phone": phone},
        "activityAttrList": D.get("activityAttrList") or [],
        "activitySettingsParam": asd, "activityGoodsList": goods,
        "logisticsExtension": 10, "activityGroupDeliverIdList": [],
        "isSelectAllGroupDeliver": 1, "selectActivityGoodsIds": [],
        "actDiscountParam": {"discountItemParams": []},
        "actLogisticsInfos": logi, "servicesGuarantee": D.get("servicesGuarantee") or {},
        "isDoubtSubmit": False, "isCertificationSubmit": False, "labelIdList": [],
    }


def open_child(cfg, act_id, phone, dry_run=False):
    s = core.make_session()
    h = core.make_headers(cfg)
    gh = cfg["gh_id"]
    r = s.get(f"{core.BASE_URL}/activity-biz/help_sale_root/act/for_leader/{act_id}/{gh}",
              headers=h, timeout=20)
    d = r.json() or {}
    D = d.get("data") or {}
    if not D.get("actId"):
        return False, None, "", d.get("msg") or "无模板"
    name = str((D.get("activityDTO") or {}).get("activityName") or "")[:44]
    if dry_run:
        return True, None, name, ""
    res = s.post(f"{core.BASE_URL}/activity-biz/help_sale_child/act/release",
                 json=build_payload(D, gh, phone), headers=h, timeout=30).json()
    if res.get("code") == 200 and (res.get("data") or {}).get("actId"):
        return True, res["data"]["actId"], name, ""
    return False, None, name, res.get("msg") or str(res)[:80]


# ────────────────────────── 主流程 ──────────────────────────

def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--chlz")
    ap.add_argument("--token")
    ap.add_argument("--gh-id", dest="gh_id")
    ap.add_argument("--uid")
    ap.add_argument("--gh", default="tang", help="tang=只开汤团的(默认) / all=不筛 / 指定 ghId")
    ap.add_argument("--delay", type=float, default=3.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--go", action="store_true")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    print("=" * 64)
    print("  一键帮卖 · 帮卖子团模式")
    print("=" * 64)

    cfg = load_config()
    if args.chlz:
        print(f"\n[1/5] 从抓包取凭证 …")
        t, u, g = extract_from_chlz(args.chlz)
        cfg["auth_token"] = t
        if u:
            cfg["uid"] = str(u)
        if g:
            cfg["gh_id"] = g
        save_config(cfg)
        print(f"      uid={cfg.get('uid')}  gh_id={cfg.get('gh_id')}  → 已写入 data/config.json")
    if args.token:
        cfg["auth_token"] = args.token
    if args.gh_id:
        cfg["gh_id"] = args.gh_id
    if args.uid:
        cfg["uid"] = args.uid

    print("\n[2/5] 检查凭证 …")
    uid, exp, left = token_status(cfg.get("auth_token", ""))
    if left is None:
        print(f"      ⚠️ token 解析不出来（{exp}），继续尝试")
    elif left <= 0:
        print(f"      ❌ token 已于 {exp} 过期")
        print("      → 重新抓一次包，然后：python3 qjl_auto.py --chlz <抓包文件>")
        return 1
    else:
        print(f"      ✅ uid={uid}  到期 {exp}（还剩 {left/3600:.1f} 小时）")
    print(f"      gh_id = {cfg.get('gh_id')}")

    print("\n[3/5] 拉自己主页（排除已开过的）…")
    mine, mine_names = my_home_acts(cfg)
    print(f"      已开过 {len(mine)} 个 → 会自动跳过")

    print("\n[4/5] 从推荐池取候选 …")
    pool = recommend_pool(cfg, list(mine)[:3] or [], rounds=args.rounds)
    print(f"      候选 {len(pool)} 个")

    phone = get_phone(cfg)
    print(f"      团长手机号 {'已取到' if phone else '⚠️未取到（可能开团失败）'}")

    print("\n      逐个查归属与权限 …")
    targets, skipped = [], []
    for aid, nm in pool.items():
        if aid in mine:
            skipped.append((aid, "自己已开过", nm))
            continue
        ok, ogh, at, name, err = probe(cfg, aid)
        if not ok:
            skipped.append((aid, f"取不到模板:{err}", nm))
            continue
        if at == 10:
            skipped.append((aid, "activityType=10 无帮卖权限", nm))
            continue
        if args.gh == "tang" and ogh not in TANG_GH:
            skipped.append((aid, f"非汤团({TANG_GH.get(ogh, ogh)})", name))
            continue
        if args.gh not in ("tang", "all") and ogh != args.gh:
            skipped.append((aid, "来源不匹配", name))
            continue
        targets.append({"actId": aid, "name": name, "ghId": ogh,
                        "owner": TANG_GH.get(ogh, ogh), "actType": at})

    if args.limit:
        targets = targets[:args.limit]

    print(f"\n      可开 {len(targets)} 个（跳过 {len(skipped)} 个）")
    if skipped:
        from collections import Counter
        def why(s):
            for k in ("自己已开过", "无帮卖权限", "非汤团", "取不到模板", "来源不匹配"):
                if k in s:
                    return k
            return "其它"
        print("      跳过原因:", dict(Counter(why(s[1]) for s in skipped)))
        for aid, reason, nm in skipped[:5]:
            print(f"        · {aid} {reason} | {nm[:34]}")
    if targets:
        print("\n" + "-" * 64)
        for i, t in enumerate(targets, 1):
            print(f"  {i:2d}. [{t['owner']}] {t['name']}")
            print(f"      actId={t['actId']} type={t['actType']}")
        print("-" * 64)

    if args.dry_run:
        print("\n  --dry-run：不开团。确认清单无误后加 --go 真跑。")
        return 0
    if not args.go:
        print("\n  加 --go 才会真正开团。")
        return 0
    if not targets:
        print("\n  没有可开的团。")
        return 0
    if not args.yes:
        print(f"\n  5 秒后开始开 {len(targets)} 个团…（Ctrl+C 取消）")
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            print("  已取消")
            return 0

    print(f"\n[5/5] 开团（间隔 {args.delay}s）…")
    ok_list, fail_list = [], []
    for i, t in enumerate(targets, 1):
        ok, new_id, name, err = open_child(cfg, t["actId"], phone)
        if ok:
            ok_list.append({"actId": t["actId"], "newActId": new_id, "name": name,
                            "owner": t["owner"], "time": time.strftime("%Y-%m-%d %H:%M:%S")})
            print(f"  {i:2d}. ✅ {name[:38]} → {new_id}")
        else:
            fail_list.append({"actId": t["actId"], "name": name, "err": err})
            print(f"  {i:2d}. ❌ {name[:38]} → {err}")
        if i < len(targets):
            time.sleep(args.delay)

    print(f"\n{'='*64}")
    print(f"  完成：成功 {len(ok_list)} / 失败 {len(fail_list)}")
    print(f"{'='*64}")
    try:
        old = json.loads(open(AUTO_LOG, encoding="utf-8").read()) if os.path.exists(AUTO_LOG) else []
    except Exception:
        old = []
    old.append({"runAt": time.strftime("%Y-%m-%d %H:%M:%S"), "gh": args.gh,
                "ok": ok_list, "fail": fail_list})
    os.makedirs(os.path.dirname(AUTO_LOG), exist_ok=True)
    with open(AUTO_LOG, "w", encoding="utf-8") as f:
        json.dump(old, f, ensure_ascii=False, indent=2)
    print(f"  已记录 → data/auto_open_log.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
