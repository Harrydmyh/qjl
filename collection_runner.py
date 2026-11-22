"""常驻合集定时调度器 —— 挂在 server.py 里跑，替代本地 AI 定时任务。

覆盖合集自动任务.md 中的 6 条定时任务：
  · 05:45 汤团   M.D汤团主推榜         (slot_collection.py --account tang --slot morning --apply)
  · 05:55 汤团²  M.D汤团2主推榜        (slot_collection.py --account tang2 --slot morning --apply)
  · 12:15 汤团   M.D汤团午间好品不错过 (slot_collection.py --account tang --slot noon --apply)
  · 12:25 汤团²  M.D汤团2午间好品不错过(slot_collection.py --account tang2 --slot noon --apply)
  · 18:45 汤团   汤团日间集市M.D       (daily_collection.py --apply)
  · 18:45 汤团²  汤团日间集市M.D       (daily_collection2.py --apply)
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
STATE = os.path.join(DATA, "collection_state.json")
EVENTS = os.path.join(DATA, "collection_events.jsonl")
LOCK = os.path.join(DATA, "collection.lock")
LOG_DIR = os.path.join(DATA, "collection_logs")

TICK = 20
CATCHUP_MINUTES = 30
PROXY_VARS = (
    "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
    "all_proxy", "ALL_PROXY",
)

TASKS = [
    {
        "id": "tang_morning",
        "hour": 5, "minute": 45,
        "account": "汤团",
        "title": "M.D汤团主推榜",
        "cmd": ["slot_collection.py", "--account", "tang", "--slot", "morning"],
    },
    {
        "id": "tang2_morning",
        "hour": 5, "minute": 55,
        "account": "汤团²",
        "title": "M.D汤团2主推榜",
        "cmd": ["slot_collection.py", "--account", "tang2", "--slot", "morning"],
    },
    {
        "id": "tang_noon",
        "hour": 12, "minute": 15,
        "account": "汤团",
        "title": "M.D汤团午间好品不错过",
        "cmd": ["slot_collection.py", "--account", "tang", "--slot", "noon"],
    },
    {
        "id": "tang2_noon",
        "hour": 12, "minute": 25,
        "account": "汤团²",
        "title": "M.D汤团2午间好品不错过",
        "cmd": ["slot_collection.py", "--account", "tang2", "--slot", "noon"],
    },
    {
        "id": "tang_daily",
        "hour": 18, "minute": 45,
        "account": "汤团",
        "title": "汤团日间集市M.D",
        "cmd": ["daily_collection.py"],
    },
    {
        "id": "tang2_daily",
        "hour": 18, "minute": 45,
        "account": "汤团²",
        "title": "汤团日间集市M.D",
        "cmd": ["daily_collection2.py"],
    },
]
TASK_MAP = {t["id"]: t for t in TASKS}

_events = deque(maxlen=100)
_state_lock = threading.Lock()
_run_lock = threading.Lock()
_thread = None
_running = False
_last_tick = None
_current = None


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            st = json.load(f)
            if isinstance(st, dict):
                return st
    except Exception:
        pass
    return {"enabled": True, "tasks": {}}


def save_state(st):
    os.makedirs(DATA, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def enabled():
    return bool(load_state().get("enabled", True))


def set_enabled(flag, by="网页"):
    with _state_lock:
        st = load_state()
        st["enabled"] = bool(flag)
        st["toggledAt"] = datetime.now().isoformat(timespec="seconds")
        st["toggledBy"] = by
        save_state(st)
    if flag:
        ensure_thread()
    push("config", f"合集定时任务已{'启动' if flag else '暂停'}（{by}）")
    return st


def push(kind, text):
    rec = {"at": datetime.now().isoformat(timespec="seconds"), "kind": kind, "text": text}
    _events.appendleft(rec)
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(EVENTS, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return rec


def _load_recent_events():
    if _events:
        return
    try:
        with open(EVENTS, encoding="utf-8") as f:
            lines = f.readlines()[-100:]
        for line in reversed(lines):
            line = line.strip()
            if line:
                _events.append(json.loads(line))
    except Exception:
        pass


def _clean_env():
    env = os.environ.copy()
    for k in PROXY_VARS:
        env.pop(k, None)
    env["OPENCV_LOG_LEVEL"] = "ERROR"
    return env


def _parse_output(out):
    """从脚本输出里提取合集 ID、摘要与状态。"""
    cid = None
    m = re.search(r"合集(?:\s*ID|已建[：:])\s*(\d+)", out)
    if not m:
        m = re.search(r"已经建过了：合集\s*(\d+)", out)
    if not m:
        m = re.search(r"已经建过了.*?(\d{5,})", out)
    if m:
        cid = m.group(1)

    lines = [ln.strip() for ln in out.splitlines() if ln.strip() and not ln.strip().startswith("[ WARN:")]
    summary = lines[-1] if lines else ""
    if "登录凭证不合法或已过期" in out or '"code":403' in out:
        return "fail", cid, "token 已过期（403），请更新 config.json"
    return "ok", cid, summary[:120]


def execute_task(task_id, apply=True, trigger="定时"):
    """同步执行单条合集任务，带文件锁防重入。"""
    global _current
    task = TASK_MAP.get(task_id)
    if not task:
        return {"ok": False, "error": f"未知任务：{task_id}"}

    if not _run_lock.acquire(blocking=False):
        return {"ok": False, "error": "另一条合集任务正在执行中"}

    lockf = None
    try:
        os.makedirs(DATA, exist_ok=True)
        os.makedirs(LOG_DIR, exist_ok=True)
        lockf = open(LOCK, "w")
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {"ok": False, "error": "命令行正在执行合集脚本，跳过"}

        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        cmd = [sys.executable, "-u", os.path.join(ROOT, task["cmd"][0])] + task["cmd"][1:]
        if apply:
            cmd.append("--apply")

        _current = {
            "id": task["id"],
            "account": task["account"],
            "title": task["title"],
            "apply": apply,
            "trigger": trigger,
            "startedAt": now.isoformat(timespec="seconds"),
        }
        push("start", f"[{trigger}] 开始 {task['account']} · {task['title']}（{'正式' if apply else '预览'}）")

        proc = subprocess.run(
            cmd,
            cwd=ROOT,
            env=_clean_env(),
            capture_output=True,
            text=True,
            timeout=900,
        )
        out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        log_path = os.path.join(LOG_DIR, f"{task_id}_{now.strftime('%Y%m%d')}.log")
        try:
            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write(f"\n=== {now.isoformat(timespec='seconds')} [{trigger}] apply={apply} rc={proc.returncode} ===\n")
                lf.write(out)
        except Exception:
            pass

        status_str, cid, summary = _parse_output(out)
        if proc.returncode != 0:
            status_str = "fail"
            if not summary:
                summary = f"退出码 {proc.returncode}"

        if apply:
            with _state_lock:
                st = load_state()
                tasks_st = st.setdefault("tasks", {})
                tasks_st[task_id] = {
                    "lastDate": today_str,
                    "lastRunAt": datetime.now().isoformat(timespec="seconds"),
                    "lastStatus": status_str,
                    "lastCollectionId": cid,
                    "lastSummary": summary,
                }
                save_state(st)

        kind = "open" if status_str == "ok" else "fail"
        cid_txt = f"（ID {cid}）" if cid else ""
        push(kind, f"{task['account']} · {task['title']}{cid_txt}：{summary}")
        return {
            "ok": status_str == "ok",
            "status": status_str,
            "collectionId": cid,
            "summary": summary,
            "returncode": proc.returncode,
        }
    except subprocess.TimeoutExpired:
        push("fail", f"{task['account']} · {task['title']}：执行超时（>15分钟）")
        return {"ok": False, "status": "fail", "summary": "执行超时"}
    except Exception as e:
        push("error", f"{task['account']} · {task['title']} 异常：{type(e).__name__}: {e}")
        return {"ok": False, "status": "fail", "summary": str(e)}
    finally:
        _current = None
        if lockf is not None:
            try:
                fcntl.flock(lockf, fcntl.LOCK_UN)
                lockf.close()
            except Exception:
                pass
        _run_lock.release()


def run_task_async(task_id, apply=False):
    if task_id not in TASK_MAP:
        return {"ok": False, "error": f"未知任务：{task_id}"}
    if _current is not None:
        return {"ok": False, "error": f"当前正在执行 {_current['account']} · {_current['title']}"}
    t = threading.Thread(
        target=execute_task,
        args=(task_id, apply, "手动"),
        name=f"coll-manual-{task_id}",
        daemon=True,
    )
    t.start()
    return {"ok": True, "started": True}


def tick():
    global _last_tick
    now = datetime.now()
    _last_tick = now.isoformat(timespec="seconds")
    if not enabled():
        return
    today_str = now.strftime("%Y-%m-%d")
    now_min = now.hour * 60 + now.minute

    st = load_state()
    tasks_st = st.get("tasks") or {}
    for task in TASKS:
        sched_min = task["hour"] * 60 + task["minute"]
        diff = now_min - sched_min
        if not (0 <= diff < CATCHUP_MINUTES):
            continue
        rec = tasks_st.get(task["id"]) or {}
        if rec.get("lastDate") == today_str:
            continue
        execute_task(task["id"], apply=True, trigger="定时")


def _loop():
    global _running
    _load_recent_events()
    while _running:
        try:
            tick()
        except Exception as e:
            push("error", f"合集调度异常：{type(e).__name__}: {e}")
        time.sleep(TICK)


def ensure_thread():
    global _thread, _running
    if _thread and _thread.is_alive():
        return _thread
    _running = True
    _thread = threading.Thread(target=_loop, name="collection-scheduler", daemon=True)
    _thread.start()
    return _thread


def status():
    _load_recent_events()
    st = load_state()
    tasks_st = st.get("tasks") or {}
    today_str = datetime.now().strftime("%Y-%m-%d")
    items = []
    for task in TASKS:
        rec = tasks_st.get(task["id"]) or {}
        ran_today = rec.get("lastDate") == today_str
        items.append({
            "id": task["id"],
            "time": f"{task['hour']:02d}:{task['minute']:02d}",
            "account": task["account"],
            "title": task["title"],
            "ranToday": ran_today,
            "lastDate": rec.get("lastDate"),
            "lastRunAt": rec.get("lastRunAt"),
            "lastStatus": rec.get("lastStatus"),
            "lastCollectionId": rec.get("lastCollectionId"),
            "lastSummary": rec.get("lastSummary"),
        })
    return {
        "enabled": bool(st.get("enabled", True)),
        "threadAlive": bool(_thread and _thread.is_alive()),
        "lastTick": _last_tick,
        "current": _current,
        "tasks": items,
        "events": list(_events)[:30],
        "serverTime": datetime.now().isoformat(timespec="seconds"),
    }
