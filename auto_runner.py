"""常驻自动开团调度器 —— 挂在 server.py 里跑，不依赖 AI 定时任务，也不需要 cron/launchd。

设计要点：
  · 一个后台线程，每 TICK 秒看一次计划文件，到点就调 auto_open_hourly.run() 开 1 条；
  · 开关状态存 data/auto_state.json（网页上点一下就能停）；
  · 事件写内存队列 + data/auto_events.jsonl，页面读内存那份，不碰磁盘；
  · 用 fcntl 抢 data/open_plan.lock：和命令行手动跑脚本互斥，不会双开同一个团。
"""
import contextlib
import fcntl
import io
import json
import os
import threading
import time
from collections import deque
from datetime import datetime

import core

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
PLAN = os.path.join(DATA, "open_plan_tang.json")
STATE = os.path.join(DATA, "auto_state.json")
EVENTS = os.path.join(DATA, "auto_events.jsonl")
LOCK = os.path.join(DATA, "open_plan.lock")
STOP = os.path.join(DATA, "open_plan_STOP")

TICK = 20                 # 每 20 秒巡一次（计划是分钟级的，20 秒足够准）
HOUR_START, HOUR_END = 7, 22

_events = deque(maxlen=200)      # 最新的在前
_thread = None
_running = False
_last_tick = None
_last_idle = None


# ---------------------------------------------------------------- 状态
def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(d):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def enabled():
    s = load_state()
    return bool(s.get("enabled", False))       # 默认关着，等人在页面上点开


def set_enabled(flag, by="网页"):
    s = load_state()
    s["enabled"] = bool(flag)
    s["toggledAt"] = datetime.now().isoformat(timespec="seconds")
    s["toggledBy"] = by
    save_state(s)
    if flag:
        ensure_thread()
    return s


def push(kind, text):
    rec = {"at": datetime.now().isoformat(timespec="seconds"), "kind": kind, "text": text}
    _events.appendleft(rec)
    try:
        with open(EVENTS, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return rec


# ---------------------------------------------------------------- 计划统计
def plan_stats():
    try:
        with open(PLAN, encoding="utf-8") as f:
            blob = json.load(f)
    except Exception:
        return {"ok": False, "msg": "还没有开团计划（先跑一次 weekly800.py 出表）"}
    items = blob.get("items") or []
    today = datetime.now().date().isoformat()
    cur = datetime.now().hour * 60 + datetime.now().minute
    pend = [x for x in items if x.get("status") == "pending"]
    today_items = sorted([x for x in pend if x.get("date") == today],
                         key=lambda x: (x.get("slotMin") or 0))
    nxt = None
    if today_items:
        nxt = today_items[0]
    else:
        future = sorted([x for x in pend if (x.get("date") or "") > today],
                        key=lambda x: (x.get("date") or "", x.get("slotMin") or 0))
        nxt = future[0] if future else None
    return {
        "ok": True,
        "updatedAt": blob.get("updatedAt"),
        "total": len(items),
        "pending": len(pend),
        "opened": sum(1 for x in items if x.get("status") == "opened"),
        "failed": sum(1 for x in items if x.get("status") == "failed"),
        "skipped": sum(1 for x in items if x.get("status") == "skip_recent"),
        "todayTotal": len(today_items),
        "todayPending": sum(1 for x in today_items if (x.get("slotMin") or 0) <= cur),
        "next": None if not nxt else {
            "date": nxt.get("date"), "time": nxt.get("time"),
            "name": nxt.get("name"), "supplier": nxt.get("supplier"),
            "cat": nxt.get("cat"), "actId": nxt.get("actId"),
        },
    }


# ---------------------------------------------------------------- 账号
def account_info():
    """当前开团账号（不返回 token 明文）。"""
    import auto_open_hourly as AUT
    cfg = AUT.load_account()
    ti = core.token_info(cfg)
    raw = cfg.get("auth_token") or ""
    return {
        "name": cfg.get("account_name") or "",
        "ghId": cfg.get("gh_id") or "",
        "uid": cfg.get("uid") or "",
        "tokenPreview": (raw[:16] + "…" + raw[-6:]) if len(raw) > 24 else ("已设置" if raw else ""),
        "tokenLen": len(raw),
        "tokenInfo": ti,
        "source": "open_account.json" if os.path.exists(AUT.ACCOUNT_FILE) else "config.json",
    }


def save_account(payload):
    """写 data/open_account.json（开团专用账号），不动主 config.json。"""
    import auto_open_hourly as AUT
    old = {}
    try:
        with open(AUT.ACCOUNT_FILE, encoding="utf-8") as f:
            old = json.load(f)
    except Exception:
        old = {}
    for k_src, k_dst in (("ghId", "ghId"), ("uid", "uid"), ("name", "name")):
        v = (payload.get(k_src) or "").strip()
        if v:
            old[k_dst] = v
    if (payload.get("authToken") or "").strip():
        old["authToken"] = payload["authToken"].strip()
    old["savedAt"] = datetime.now().isoformat(timespec="seconds")
    tmp = AUT.ACCOUNT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(old, f, ensure_ascii=False, indent=1)
    os.replace(tmp, AUT.ACCOUNT_FILE)
    push("config", f"开团账号已更新：{old.get('name') or ''} {old.get('ghId') or ''}")
    return account_info()


# ---------------------------------------------------------------- 巡一次
def tick(force=False):
    """到点就开一条。force=True 忽略开关/时间窗（页面上的「立刻开一条」）。"""
    global _last_tick, _last_idle
    now = datetime.now()
    _last_tick = now.isoformat(timespec="seconds")

    if not force:
        if not enabled():
            return {"acted": False, "why": "自动开团是关着的"}
        if not (HOUR_START <= now.hour <= HOUR_END):
            _last_idle = _last_tick
            return {"acted": False, "why": f"不在 {HOUR_START}-{HOUR_END} 点窗口"}
    if os.path.exists(STOP) and not force:
        return {"acted": False, "why": "检测到 data/open_plan_STOP，已停"}

    # 和命令行手动跑脚本互斥：抢不到锁说明外面正在开
    try:
        lockf = open(LOCK, "w")
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return {"acted": False, "why": "另一个进程正在开团，本次跳过"}

    try:
        import auto_open_hourly as AUT
        # 先自己判一次有没有到期的，避免每次都把 run() 的"无到期"也当事件记
        try:
            with open(PLAN, encoding="utf-8") as f:
                plan = json.load(f)
            due = AUT.due_items(plan, now)
        except Exception:
            due = []
        if not due and not force:
            _last_idle = now.isoformat(timespec="seconds")
            return {"acted": False, "why": "本时段没有到期的条目"}

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = AUT.run(now, dry=0, limit=1)
        out = buf.getvalue().strip()
        kind = "idle"
        if "✅" in out:
            kind = "open"
        elif "❌" in out:
            kind = "fail"
        elif "⏭" in out:
            kind = "skip"
        if kind != "idle":
            for line in out.splitlines():
                line = line.strip()
                if line:
                    push(kind, line)
        s = load_state()
        s["lastRunAt"] = now.isoformat(timespec="seconds")
        s["lastResult"] = kind
        save_state(s)
        return {"acted": kind != "idle", "kind": kind, "code": code, "out": out}
    finally:
        try:
            fcntl.flock(lockf, fcntl.LOCK_UN)
            lockf.close()
        except Exception:
            pass


def _loop():
    global _running
    while _running:
        try:
            tick()
        except Exception as e:
            push("error", f"调度异常：{type(e).__name__}: {e}")
        time.sleep(TICK)


def ensure_thread():
    """服务启动时调一次；enabled 才会真正干活，线程本身一直在等。"""
    global _thread, _running
    if _thread and _thread.is_alive():
        return _thread
    _running = True
    _thread = threading.Thread(target=_loop, name="auto-opener", daemon=True)
    _thread.start()
    return _thread


def status():
    st = load_state()
    try:
        token = account_info()
    except Exception as e:
        token = {"error": f"{type(e).__name__}: {e}"}
    return {
        "enabled": enabled(),
        "threadAlive": bool(_thread and _thread.is_alive()),
        "tickSeconds": TICK,
        "window": [HOUR_START, HOUR_END],
        "lastTick": _last_tick,
        "lastIdle": _last_idle,
        "lastRunAt": st.get("lastRunAt"),
        "lastResult": st.get("lastResult"),
        "toggledAt": st.get("toggledAt"),
        "stopFile": os.path.exists(STOP),
        "plan": plan_stats(),
        "account": token,
        "events": list(_events)[:40],
        "serverTime": datetime.now().isoformat(timespec="seconds"),
    }
