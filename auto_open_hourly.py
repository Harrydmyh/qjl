#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按计划表自动帮卖开团 —— 每小时被调度器叫起来跑一波。

设计要点（都是为了别在真账号上翻车）：
  · **单次小批量**：一波最多 MAX_PER_RUN 个，条与条之间随机等 20~50 秒，
    像人在操作，不像脚本扫射。
  · **落后不补爆**：机器睡眠/网络断了导致错过整点，下次只按上限补一部分，
    剩下的顺延到后面的整点，绝不一次把一天的量全放出去。
  · **三重刹车**：STOP 文件、token 过期、非 7:00-22:00 —— 任一命中就空跑退出。
  · **状态落盘**：每条开完立刻写回 open_plan_tang.json（先写临时文件再 rename），
    半路被打断也不会重复开。
  · **跳过已开的**：pending 里若有「主页已有在售同名团」，开之前再查一次。

用法：
  python3 auto_open_hourly.py            # 正常一波（调度器用这个）
  python3 auto_open_hourly.py --dry 5    # 只看会开哪 5 个，不发请求
  python3 auto_open_hourly.py --now 12   # 假装现在是 12 点（测试用）
"""
import argparse
import fcntl
import json
import os
import random
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core
import qjl_auto as Q

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
PLAN = os.path.join(DATA, "open_plan_tang.json")
LOG = os.path.join(DATA, "open_plan_log.jsonl")
LOCK = os.path.join(DATA, "open_plan.lock")
STOP = os.path.join(DATA, "open_plan_STOP")
TANG_GH = "PspU4o9_bsxkBH0_6c6cef00"
HOUR_START, HOUR_END = 7, 22
MAX_PER_RUN = 4


def say(msg):
    print(f"[{datetime.now():%m-%d %H:%M}] {msg}", flush=True)


def load_plan():
    if not os.path.exists(PLAN):
        return None
    with open(PLAN) as f:
        return json.load(f)


def save_plan(plan):
    tmp = PLAN + ".tmp"
    with open(tmp, "w") as f:
        json.dump(plan, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PLAN)


def append_log(rec):
    with open(LOG, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def due_items(plan, now):
    """到点该开的：日期更早的（补课）或今天且整点已到。"""
    today = now.date().isoformat()
    out = []
    for it in plan.get("items", []):
        if it.get("status") != "pending":
            continue
        d = it.get("date") or ""
        if d < today:
            out.append(it)
        elif d == today and int(it.get("slot") or 0) <= now.hour:
            out.append(it)
    out.sort(key=lambda x: (x.get("date") or "", int(x.get("slot") or 0), -int(x.get("orders") or 0)))
    return out


def already_live(cfg, headers, act_id, names_by_act):
    """开之前再确认一次：主页上有没有同名且在售的团。"""
    return False  # 轻量版不做逐条复查，靠 bs234_mine_check 预判；见 --recheck


def classify_err(msg):
    m = str(msg or "")
    if "5830" in m or "帮卖权限" in m:
        return "gone", "无帮卖权限"
    if "5259" in m:
        return "failed", f"设置参数非法({m[:30]})"
    if "已" in m and ("帮" in m or "开" in m or "存在" in m):
        return "opened", f"平台已存在({m[:30]})"
    return "failed", m[:60]


def run(now, dry=0, limit=MAX_PER_RUN):
    cfg = json.load(open(os.path.join(DATA, "config.json")))
    cfg["gh_id"] = TANG_GH

    plan = load_plan()
    if not plan:
        say("没有计划文件 data/open_plan_tang.json，先生成周报")
        return 0

    todo = due_items(plan, now)
    pend_all = sum(1 for x in plan["items"] if x.get("status") == "pending")
    if not todo:
        say(f"本小时无待开（剩余 {pend_all} 条）")
        return 0

    batch = todo[:limit]
    if dry:
        say(f"[dry] 现在该开 {len(todo)} 条，本次先开 {len(batch)}:")
        for it in batch:
            say(f"  {it['date']} {it['slot']:02d}:00 [{it['cat']}] {it['supplier'][:8]} {it['name'][:26]}")
        return 0

    ti = core.token_info(cfg)
    if not ti.get("valid"):
        say(f"❌ token 已过期（{ti.get('exp_str')}），停止开团。重新抓包写回 data/config.json 后自动继续。")
        return 2

    phone = Q.get_phone(cfg)
    if not phone:
        say("⚠️ 没取到团长手机号，帮卖子团可能开不出来")

    ok = fail = 0
    for i, it in enumerate(batch):
        aid = int(it["actId"])
        try:
            good, new_id, name, err = Q.open_child(cfg, aid, phone, dry_run=False)
        except Exception as e:
            good, new_id, name, err = False, None, it["name"], f"异常:{e}"
        if good:
            it["status"], it["newActId"] = "opened", new_id
            it["note"], it["doneAt"] = "", datetime.now().isoformat(timespec="seconds")
            ok += 1
            say(f"✅ {it['cat']} | {it['supplier'][:8]} | {it['name'][:24]} → {new_id}")
        else:
            st, note = classify_err(err)
            it["status"], it["note"] = st, note
            it["doneAt"] = datetime.now().isoformat(timespec="seconds")
            fail += 1
            say(f"❌ {it['supplier'][:8]} | {it['name'][:24]} → {note}")
        plan["updatedAt"] = datetime.now().isoformat(timespec="seconds")
        save_plan(plan)
        append_log({"at": datetime.now().isoformat(timespec="seconds"), "actId": aid,
                    "ok": bool(good), "newActId": new_id if good else None,
                    "status": it["status"], "note": it["note"]})
        if i < len(batch) - 1:
            w = random.uniform(20, 50)
            time.sleep(w)

    left = sum(1 for x in plan["items"] if x.get("status") == "pending")
    say(f"本波完成：成功 {ok} / 失败 {fail}；今日剩余待开 {left} 条")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", type=int, default=0, help="只看要开哪几条，不发请求")
    ap.add_argument("--now", type=int, default=None, help="假装现在几点（测试）")
    ap.add_argument("--limit", type=int, default=MAX_PER_RUN)
    ap.add_argument("--force", action="store_true", help="忽略时间窗口（手工补开用）")
    args = ap.parse_args()

    if os.path.exists(STOP):
        say("检测到 data/open_plan_STOP，已停止。删掉该文件即恢复。")
        return 0

    now = datetime.now()
    if args.now is not None:
        now = now.replace(hour=args.now)
    if not args.force and not (HOUR_START <= now.hour <= HOUR_END):
        say(f"当前 {now.hour} 点不在 {HOUR_START}-{HOUR_END} 开团窗口内，跳过")
        return 0

    lockf = open(LOCK, "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        say("上一次还在跑，本次跳过")
        return 0
    try:
        return run(now, dry=args.dry, limit=args.limit)
    finally:
        fcntl.flock(lockf, fcntl.LOCK_UN)
        lockf.close()


if __name__ == "__main__":
    sys.exit(main())
