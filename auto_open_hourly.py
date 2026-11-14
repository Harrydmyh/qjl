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
import dedup_open as D

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
    bl = load_blacklist()
    out = []
    for it in plan.get("items", []):
        if it.get("status") != "pending":
            continue
        if any(k and k in (it.get("supplier") or "") for k in bl):
            continue            # 拉黑的供货商，计划里就算有也不开
        d = it.get("date") or ""
        if d < today:
            out.append(it)
        elif d == today and int(it.get("slot") or 0) <= now.hour:
            out.append(it)
    out.sort(key=lambda x: (x.get("date") or "", int(x.get("slot") or 0), -int(x.get("orders") or 0)))
    return out


def load_blacklist():
    """永久不开的供货商（与 weekly800 共用 data/blacklist_suppliers.json）。"""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "blacklist_suppliers.json")
    try:
        b = json.load(open(p))
        lst = b.get("suppliers") if isinstance(b, dict) else b
        if lst:
            return [str(x) for x in lst if str(x).strip()]
    except Exception:
        pass
    return ["简小姐", "悦宝"]


def already_live(cfg, headers, act_id, names_by_act):
    """开之前再确认一次：主页上有没有同名且在售的团。"""
    return False  # 轻量版不做逐条复查，靠 bs234_mine_check 预判；见 --recheck


# ---------------------------------------------------------------- 开前实时查重
_OWN = None
_STATIC = None


def _static_check():
    """dedup_open.py 的静态查重结论（每轮生成周报时刷新）。"""
    global _STATIC
    if _STATIC is None:
        try:
            _STATIC = json.load(open(os.path.join(DATA, "open_check.json")))
        except Exception:
            _STATIC = {}
    return _STATIC


def own_acts(max_age=1800):
    """汤团号主页团（含创建时间），半小时内复用本地缓存。"""
    global _OWN
    if _OWN is not None:
        return _OWN
    p = os.path.join(DATA, "tang_own_acts.json")
    try:
        fresh = time.time() - os.path.getmtime(p) < max_age
    except OSError:
        fresh = False
    if fresh:
        try:
            _OWN = json.load(open(p))
            return _OWN
        except Exception:
            pass
    try:
        _OWN = D.refresh_own()
    except Exception as e:
        say(f"⚠️ 拉主页团失败（{e}），本轮只按子团记录判重")
        _OWN = []
    return _OWN


def precheck(cfg, headers, it):
    """这条能不能开。返回 (ok, 原因, 证据)。

    两道：
      1. 子团记录 —— 汤团号给这个母团开过的子团，5 天内创建过就不开（最硬）；
      2. 主页团名 —— 近 5 天创建的团里有没有同名的（跟团不产生子团记录，只能按名字认）。
    主页那 2000 多个团只在第 1 道没命中时才拉，省时间。
    """
    aid = int(it["actId"])
    now = int(time.time() * 1000)
    cutoff = now - D.WINDOW_DAYS * 86400000

    # 「重复」是永久判据（同一款不该挂两个团），静态结论直接兜住；
    # 「5天内已开」是滚动的，交给下面实时查。
    st = _static_check()
    v = st.get(str(aid)) or {}
    if v and not v.get("open") and v.get("why") == "重复":
        return False, "重复", v.get("evidence") or ""

    s = core.make_session()
    try:
        r = s.post("https://apipro.qunjielong.com/activity-accessory/gh_activity/list_gh_child_activities",
                   json={"ghId": TANG_GH, "pactId": aid, "page": 1, "pageSize": 50},
                   headers=headers, timeout=15)
        d = (r.json() or {}).get("data")
        kids = d if isinstance(d, list) else ((d or {}).get("list") or (d or {}).get("records") or [])
    except Exception:
        kids = []
    young = [k for k in kids if (k.get("createTime") or 0) >= cutoff]
    if young:
        k = max(young, key=lambda x: x.get("createTime") or 0)
        return False, "5天内已开", f"子团 {k.get('actId')}（{datetime.fromtimestamp((k.get('createTime') or 0)/1000):%m-%d %H:%M}）"

    # 注意：**不能**把「有在售子团」当拦截。这个号本来就把同一个母团每 7~20 天
    # 重开一次，371/620 条都挂着在售子团，按在售判会一条都开不出来。
    own = own_acts()
    # 主页团名是「活动标题」，跟商品名差得远，优先用 actName 比；两个都比一遍
    cands = [x for x in (it.get("actName"), it.get("name")) if x]
    for nm in cands:
        hit = next((a for a in own
                    if (a.get("create") or 0) >= cutoff and D.name_same(nm, a.get("name") or "")), None)
        if hit:
            return False, "5天内已开", \
                f"主页团 {hit['actId']}（{datetime.fromtimestamp((hit.get('create') or 0)/1000):%m-%d %H:%M}）"
    return True, "", ""


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

    batch = todo[: limit * 3]      # 多取一些：被查重挡掉的号不占本波名额
    if dry:
        say(f"[dry] 现在该开 {len(todo)} 条，本次先开 {limit} 条（候选 {len(batch)}）:")
        for it in batch[:limit]:
            say(f"  {it['date']} {it['slot']:02d}:00 [{it['cat']}] {it['supplier'][:8]} {it['name'][:26]}")
        return 0

    ti = core.token_info(cfg)
    if not ti.get("valid"):
        say(f"❌ token 已过期（{ti.get('exp_str')}），停止开团。重新抓包写回 data/config.json 后自动继续。")
        return 2

    phone = Q.get_phone(cfg)
    if not phone:
        say("⚠️ 没取到团长手机号，帮卖子团可能开不出来")

    headers = core.make_headers(cfg)
    ok = fail = skipped = 0
    for i, it in enumerate(batch):
        if ok + fail >= limit:
            break
        aid = int(it["actId"])
        allowed, why, ev = precheck(cfg, headers, it)
        if not allowed:
            it["status"] = "skip_recent"
            it["note"] = f"{why}：{ev}"
            it["doneAt"] = datetime.now().isoformat(timespec="seconds")
            plan["updatedAt"] = it["doneAt"]
            save_plan(plan)
            append_log({"at": it["doneAt"], "actId": aid, "ok": False, "newActId": None,
                        "status": "skip_recent", "note": it["note"]})
            skipped += 1
            say(f"⏭ 不开 {it['supplier'][:8]} | {it['name'][:22]} → {why}（{ev}）")
            continue
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
        if ok + fail < limit:
            w = random.uniform(20, 50)
            time.sleep(w)

    left = sum(1 for x in plan["items"] if x.get("status") == "pending")
    say(f"本波完成：成功 {ok} / 失败 {fail} / 查重跳过 {skipped}；剩余待开 {left} 条")
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
