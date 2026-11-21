#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""企业微信客户群台账拉取 + 内部/外部分类

用法:
    /opt/homebrew/bin/python3 wecom_groups.py --test    # 只拉 20 个群，先验证凭证
    /opt/homebrew/bin/python3 wecom_groups.py           # 全量拉取

依赖 data/wecom_config.json:
    {"corpid": "ww开头的企业ID", "corpsecret": "客户联系的Secret"}

产出:
    data/wecom_groups.json                      全量台账（供后续发送脚本读）
    周报/企微群台账_YYYYMMDD_HHMM.xlsx           人看的表
"""
import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests

BASE = "https://qyapi.weixin.qq.com/cgi-bin"
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
CFG = os.path.join(DATA, "wecom_config.json")
TOKEN_CACHE = os.path.join(DATA, "wecom_token.json")
OUT_JSON = os.path.join(DATA, "wecom_groups.json")
OUT_DIR = os.path.join(ROOT, "周报")

# 客户联系接口限流约 600 次/分钟，这里压到 ~400 次/分钟，留余量
THROTTLE_GAP = 0.15

_lock = threading.Lock()
_last_call = [0.0]


def make_session():
    s = requests.Session()
    s.trust_env = False          # 不读系统代理，企微走直连
    s.headers.update({"Content-Type": "application/json"})
    return s


def throttle(gap=THROTTLE_GAP):
    with _lock:
        now = time.time()
        wait = _last_call[0] + gap - now
        if wait > 0:
            time.sleep(wait)
            now = time.time()
        _last_call[0] = max(now, _last_call[0] + gap)


def load_cfg():
    if not os.path.exists(CFG):
        sys.exit(
            "缺少 data/wecom_config.json\n"
            '内容应为: {"corpid": "ww...", "corpsecret": "..."}\n'
            "（corpid 在 我的企业→企业信息；corpsecret 在 客户与上下游→客户联系→API）"
        )
    cfg = json.load(open(CFG, encoding="utf-8"))
    if not cfg.get("corpid") or not cfg.get("corpsecret"):
        sys.exit("wecom_config.json 里 corpid / corpsecret 不能为空")
    return cfg


def get_token(s, corpid, secret):
    import hashlib
    fp = hashlib.md5((corpid + "|" + secret).encode()).hexdigest()[:12]
    if os.path.exists(TOKEN_CACHE):
        try:
            c = json.load(open(TOKEN_CACHE, encoding="utf-8"))
            if (c.get("fp") == fp and c.get("expire_at", 0) > time.time() + 120):
                print("  复用缓存的 access_token")
                return c["access_token"]
        except Exception:
            pass
    r = s.get(f"{BASE}/gettoken", params={"corpid": corpid, "corpsecret": secret}, timeout=20)
    d = r.json()
    if d.get("errcode") != 0:
        sys.exit(f"拿 access_token 失败: errcode={d.get('errcode')} {d.get('errmsg')}\n"
                 "常见原因: Secret 复制不全、或该应用没有客户联系权限")
    json.dump(
        {"fp": fp, "corpid": corpid, "access_token": d["access_token"],
         "expire_at": time.time() + d.get("expires_in", 7200)},
        open(TOKEN_CACHE, "w", encoding="utf-8"),
    )
    print("  access_token 已获取")
    return d["access_token"]


def list_chats(s, token):
    """分页拉客户群 chat_id 列表。这个接口只返回客户群（有外部联系人的群）。"""
    out, offset, page = [], 0, 1000
    while True:
        r = s.post(f"{BASE}/externalcontact/groupchat/list",
                   params={"access_token": token},
                   json={"status_filter": 0, "offset": offset, "limit": page},
                   timeout=30)
        d = r.json()
        if d.get("errcode") == 60020:
            sys.exit(
                f"60020 当前 IP 不在可信白名单，被拒绝。\n"
                f"  {d.get('errmsg')}\n"
                f"  修复：管理后台 → 应用管理 → 自建 → 点进本应用 → 页面底部「企业可信IP」→ 配置\n"
                f"  把 errmsg 里 from ip 后面那个地址填进去，等 1 分钟再跑。"
            )
        if d.get("errcode") == 60011:
            sys.exit(
                "60011 应用没有客户联系接口权限。\n"
                "  修复：客户与上下游 → 客户联系 → 客户 → API → 可调用接口的应用 → 设置 → 勾选本应用"
            )
        if d.get("errcode") != 0:
            sys.exit(f"拉群列表失败 offset={offset}: {d}")
        batch = d.get("group_chat_list") or []
        out.extend(batch)
        print(f"  列表 offset={offset} 返回 {len(batch)} 条，累计 {len(out)}")
        if len(batch) < page:
            break
        offset += page
        time.sleep(0.3)
    return out


def get_detail(s, token, chat_id):
    throttle()
    for attempt in range(3):
        try:
            r = s.post(f"{BASE}/externalcontact/groupchat/get",
                       params={"access_token": token},
                       json={"chat_id": chat_id, "need_name": 1}, timeout=25)
            d = r.json()
        except Exception as e:
            d = {"errcode": "network", "errmsg": str(e)}
        if d.get("errcode") == 0:
            return d.get("group_chat") or {}
        if d.get("errcode") in (45009, -1, "network"):
            time.sleep(2 + attempt * 3)
            continue
        return {"chat_id": chat_id, "_err": f"{d.get('errcode')}:{d.get('errmsg')}"}
    return {"chat_id": chat_id, "_err": "retry_exhausted"}


def build_rows(chats, limit=0):
    s = make_session()
    cfg = load_cfg()
    token = get_token(s, cfg["corpid"], cfg["corpsecret"])

    ids = [c.get("chat_id") for c in chats if c.get("chat_id")]
    if limit:
        ids = ids[:limit]
    print(f"  开始拉 {len(ids)} 个群的详情，预计 {max(1, int(len(ids) * THROTTLE_GAP / 60))} 分钟")

    rows, done = [], 0
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(get_detail, s, token, cid): cid for cid in ids}
        for fut in as_completed(futures):
            g = fut.result()
            done += 1
            if done % 100 == 0 or done == len(ids):
                print(f"    进度 {done}/{len(ids)}", flush=True)
            members = g.get("member_list") or []
            inner = sum(1 for m in members if m.get("type") == 1)
            outer = sum(1 for m in members if m.get("type") == 2)
            ct = g.get("create_time") or 0
            rows.append({
                "chat_id": g.get("chat_id") or futures[fut],
                "name": (g.get("name") or "").strip(),
                "owner": g.get("owner") or "",
                "total": len(members),
                "inner": inner,
                "outer": outer,
                "create_time": ct,
                "create_date": datetime.fromtimestamp(ct).strftime("%Y-%m-%d") if ct else "",
                "notice": (g.get("notice") or "")[:80],
                "kind": "客户群" if outer > 0 else "疑似内部群",
                "err": g.get("_err", ""),
            })
    return rows


def summarize(rows):
    ok = [r for r in rows if not r["err"]]
    cust = [r for r in ok if r["outer"] > 0]
    inner_zero = [r for r in ok if r["outer"] == 0]
    err = [r for r in rows if r["err"]]
    owners = {}
    for r in cust:
        owners[r["owner"]] = owners.get(r["owner"], 0) + 1
    print("\n" + "=" * 56)
    print(f"成功拉到详情: {len(ok)} 个")
    print(f"  其中客户群(有外部客户): {len(cust)} 个")
    print(f"  其中无外部客户:         {len(inner_zero)} 个  → 疑似内部群/纯员工群")
    if err:
        print(f"  详情失败:               {len(err)} 个")
    print(f"不同群主数: {len(owners)} 个")
    if owners:
        top = sorted(owners.items(), key=lambda x: -x[1])[:10]
        print("  群最多的群主(前10): " + ", ".join(f"{k}:{v}" for k, v in top))
        need = -(-len(cust) // 200)
        print(f"按「每人200群」上限，覆盖 {len(cust)} 群至少需要 {need} 个负责人账号")
    print("=" * 56)
    return {"ok": len(ok), "cust": len(cust), "inner": len(inner_zero),
            "err": len(err), "owners": len(owners)}


def write_xlsx(rows):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"企微群台账_{datetime.now():%Y%m%d_%H%M}.xlsx")

    wb = Workbook()
    ws = wb.active
    ws.title = "群台账"
    head = ["群ID", "群名", "群主userid", "成员总数", "企业成员", "外部客户",
            "建群日期", "判定", "群公告摘要", "出错"]
    ws.append(head)
    fill = PatternFill("solid", fgColor="EEEDFE")
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = fill
        c.alignment = Alignment(horizontal="center", vertical="center")

    for r in sorted(rows, key=lambda x: (x["kind"] != "客户群", -x["total"])):
        ws.append([r["chat_id"], r["name"], r["owner"], r["total"], r["inner"],
                   r["outer"], r["create_date"], r["kind"], r["notice"], r["err"]])

    widths = [34, 30, 14, 10, 10, 10, 12, 12, 40, 20]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    # 第二页：按群主汇总
    ws2 = wb.create_sheet("按群主汇总")
    ws2.append(["群主userid", "群数", "成员总数", "外部客户总数"])
    agg = {}
    for r in rows:
        if r["err"]:
            continue
        a = agg.setdefault(r["owner"] or "(空)", [0, 0, 0])
        a[0] += 1
        a[1] += r["total"]
        a[2] += r["outer"]
    for k, v in sorted(agg.items(), key=lambda x: -x[1][0]):
        ws2.append([k, v[0], v[1], v[2]])
    for c in ws2[1]:
        c.font = Font(bold=True)
    ws2.column_dimensions["A"].width = 20
    for col in "BCD":
        ws2.column_dimensions[col].width = 12
    ws2.freeze_panes = "A2"

    wb.save(path)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="只拉前 20 个群验证凭证")
    args = ap.parse_args()

    print("① 读取配置、拿 token")
    s = make_session()
    cfg = load_cfg()
    token = get_token(s, cfg["corpid"], cfg["corpsecret"])

    print("② 拉客户群列表")
    chats = list_chats(s, token)
    print(f"  共 {len(chats)} 个客户群 chat_id")
    if not chats:
        sys.exit("一个客户群都没拉到：检查 secret 是否为「客户联系」的应用、以及是否有成员配置了客户联系功能")

    print("③ 拉群详情")
    rows = build_rows(chats, limit=20 if args.test else 0)

    print("④ 统计")
    stat = summarize(rows)

    print("⑤ 落盘")
    if not args.test:
        json.dump({"fetched_at": datetime.now().isoformat(timespec="seconds"),
                   "stat": stat, "groups": rows},
                  open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"  JSON: {OUT_JSON}")
    path = write_xlsx(rows)
    print(f"  表格: {path}")
    print("\n完成。用 Excel 打开看那两个页签：群台账 / 按群主汇总")


if __name__ == "__main__":
    main()
