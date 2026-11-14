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
from datetime import datetime, timedelta

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
MIN_CATCHUP = 15          # 落后补课时，两条之间至少隔这么多分钟（别扎堆）

_events = deque(maxlen=200)      # 最新的在前
_thread = None
_running = False
_last_tick = None
_last_idle = None
_current = None                  # 正在开的那一条（页面用来显示「执行中」）


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
WEEK = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _min(it):
    return int(it.get("slotMin") or 0)


def hhmm(m):
    return f"{int(m) // 60:02d}:{int(m) % 60:02d}"


def _gap_of(ts):
    """一批时间点里最常见的间隔（分钟）。"""
    ts = sorted(set(int(t) for t in ts))
    gaps = [b - a for a, b in zip(ts, ts[1:]) if b > a]
    if not gaps:
        return None
    from collections import Counter
    return Counter(gaps).most_common(1)[0][0]


def _brief(x):
    return {
        "time": x.get("time") or "",
        "slotMin": _min(x),
        "cat": x.get("cat") or "",
        "supplier": x.get("supplier") or "",
        "name": x.get("name") or "",
        "status": x.get("status") or "",
        "note": x.get("note") or "",
        "actId": x.get("actId"),
        "orders": x.get("orders") or 0,
    }


def _load_plan_blob():
    with open(PLAN, encoding="utf-8") as f:
        return json.load(f)


def day_items(date):
    """某一天的清单（含已开/已跳过），按时间排序。"""
    try:
        blob = _load_plan_blob()
    except Exception:
        return {"ok": False, "msg": "还没有清单"}
    items = [x for x in (blob.get("items") or []) if (x.get("date") or "") == date]
    items.sort(key=_min)
    return {"ok": True, "date": date, "items": [_brief(x) for x in items]}


def plan_stats():
    try:
        blob = _load_plan_blob()
    except Exception:
        return {"ok": False, "msg": "还没有开团清单 —— 先出一次周报（weekly800.py），清单会写到 data/open_plan_tang.json"}

    items = blob.get("items") or []
    now = datetime.now()
    today_d = now.date()
    today = today_d.isoformat()
    cur = now.hour * 60 + now.minute
    pend = [x for x in items if x.get("status") == "pending"]

    today_all = sorted([x for x in items if x.get("date") == today], key=_min)
    today_pend = [x for x in today_all if x.get("status") == "pending"]
    overdue = [x for x in today_pend if _min(x) <= cur]          # 到点了还没开的
    later_today = [x for x in today_pend if _min(x) > cur]       # 今天还没到点的
    nxt = later_today[0] if later_today else None
    if nxt is None:
        future = sorted([x for x in pend if (x.get("date") or "") > today],
                        key=lambda x: (x.get("date") or "", _min(x)))
        nxt = future[0] if future else None

    days = []
    for i in range(7):
        d = today_d + timedelta(days=i)
        ds = d.isoformat()
        di = [x for x in items if x.get("date") == ds]
        dp = [x for x in di if x.get("status") == "pending"]
        ts = [_min(x) for x in dp]
        days.append({
            "date": ds,
            "weekday": WEEK[d.weekday()],
            "isToday": ds == today,
            "total": len(di),
            "pending": len(dp),
            "opened": sum(1 for x in di if x.get("status") == "opened"),
            "skipped": sum(1 for x in di if x.get("status") == "skip_recent"),
            "failed": sum(1 for x in di if x.get("status") == "failed"),
            "gap": _gap_of(ts),
            "first": hhmm(min(ts)) if ts else "",
            "last": hhmm(max(ts)) if ts else "",
            "cats": _cat_count(dp),
        })

    gap_today = _gap_of([_min(x) for x in (later_today or today_pend)])
    gap_next = next((d["gap"] for d in days if not d["isToday"] and d["pending"]), None)
    pace_bits = []
    if gap_today:
        pace_bits.append(f"今天每 {gap_today} 分钟一条")
    if gap_next:
        pace_bits.append(f"之后每 {gap_next} 分钟一条")

    nxt_out = None
    if nxt:
        nxt_out = _brief(nxt)
        nxt_out["date"] = nxt.get("date")
        if nxt.get("date") == today:
            nxt_out["inMinutes"] = _min(nxt) - cur
        else:
            delta_days = (datetime.strptime(nxt["date"], "%Y-%m-%d").date() - today_d).days
            nxt_out["inMinutes"] = delta_days * 1440 + _min(nxt) - cur

    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(PLAN)).isoformat(timespec="seconds")
    except Exception:
        mtime = None

    return {
        "ok": True,
        "updatedAt": blob.get("updatedAt"),
        "planMtime": mtime,
        "total": len(items),
        "pending": len(pend),
        "opened": sum(1 for x in items if x.get("status") == "opened"),
        "failed": sum(1 for x in items if x.get("status") == "failed"),
        "skipped": sum(1 for x in items if x.get("status") == "skip_recent"),
        "today": today,
        "todayTotal": len(today_all),
        "todayDone": sum(1 for x in today_all if x.get("status") == "opened"),
        "todayPending": len(today_pend),
        "overdue": len(overdue),
        "pace": " · ".join(pace_bits),
        "gapToday": gap_today,
        "gapNext": gap_next,
        "next": nxt_out,
        "days": days,
        "items": [_brief(x) for x in today_all],
        "cats": _cat_count([x for x in today_all if x.get("status") == "pending"]),
    }


def _cat_count(rows):
    from collections import Counter
    c = Counter((x.get("cat") or "未分类") for x in rows)
    return dict(c)


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

        # ── 补课限速 ──────────────────────────────────────────────
        # 关了一段时间再打开时，一堆「已过点」的条目会同时到期。若不管，20 秒
        # 一次的 tick 会把它们连着一条条开出去，反而扎堆、违背均匀曝光。
        # 规则：刚过点 5 分钟内的算准点，照计划开；超过 5 分钟算过期，过期条目
        # 两条之间至少隔 MIN_CATCHUP 分钟。
        pick = due[0]
        if due and not force:
            today = now.date().isoformat()
            cur = now.hour * 60 + now.minute
            on_time = [x for x in due if x.get("date") == today and 0 <= cur - _min(x) <= 5]
            if on_time:
                pick = on_time[0]
            else:
                last = load_state().get("lastOpenAt")
                if last:
                    try:
                        mins = (now - datetime.fromisoformat(last)).total_seconds() / 60
                    except Exception:
                        mins = 999
                    if mins < MIN_CATCHUP:
                        _last_idle = now.isoformat(timespec="seconds")
                        return {"acted": False,
                                "why": f"落后 {len(due)} 条待补，补课间隔 {MIN_CATCHUP} 分钟（距上次 {mins:.0f} 分钟）"}

        buf = io.StringIO()
        global _current
        _current = {
            "actId": pick.get("actId"),
            "name": pick.get("name") or "",
            "supplier": pick.get("supplier") or "",
            "cat": pick.get("cat") or "",
            "time": pick.get("time") or "",
            "date": pick.get("date") or "",
            "startedAt": datetime.now().isoformat(timespec="seconds"),
        }
        with contextlib.redirect_stdout(buf):
            code = AUT.run(now, dry=0, limit=1, only=pick.get("actId"))
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
        if kind == "open":
            s["lastOpenAt"] = now.isoformat(timespec="seconds")
        save_state(s)
        return {"acted": kind != "idle", "kind": kind, "code": code, "out": out}
    finally:
        _current = None
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
        "current": _current,
        "lastRunAt": st.get("lastRunAt"),
        "lastResult": st.get("lastResult"),
        "toggledAt": st.get("toggledAt"),
        "stopFile": os.path.exists(STOP),
        "plan": plan_stats(),
        "account": token,
        "events": list(_events)[:40],
        "serverTime": datetime.now().isoformat(timespec="seconds"),
    }
