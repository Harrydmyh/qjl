"""群接龙开团助手 - Web 服务"""

import asyncio
import json
import os
import queue
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

import core

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

CONFIG_FILE = DATA_DIR / "config.json"
ACTIVITIES_FILE = DATA_DIR / "activities.json"
LOG_FILE = DATA_DIR / "open_log.json"

app = FastAPI()

_fetch_lock = threading.Lock()
_open_lock = threading.Lock()


def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return {"auth_token": "", "gh_id": "", "uid": ""}


def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def load_activities() -> list:
    if ACTIVITIES_FILE.exists():
        return json.loads(ACTIVITIES_FILE.read_text(encoding="utf-8"))
    return []


def save_activities(items: list):
    ACTIVITIES_FILE.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")


def load_log() -> list:
    if LOG_FILE.exists():
        return json.loads(LOG_FILE.read_text(encoding="utf-8"))
    return []


def append_log(entry: dict):
    log = load_log()
    log.append(entry)
    LOG_FILE.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")


def sse_stream(work_fn, *args):
    """在线程里运行 work_fn，通过队列把事件转成 SSE 流"""
    q = queue.Queue()

    def run():
        try:
            work_fn(*args, q)
        except Exception as e:
            q.put({"type": "error", "msg": str(e)})
        finally:
            q.put(None)

    threading.Thread(target=run, daemon=True).start()

    async def generator():
        while True:
            try:
                item = q.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.05)
                continue
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/api/config")
async def get_config():
    cfg = load_config()
    info = core.token_info(cfg)
    return {"config": {k: v for k, v in cfg.items() if k != "auth_token"},
            "token_preview": cfg.get("auth_token", "")[:20] + "..." if cfg.get("auth_token") else "",
            "token_info": info,
            "categories": core.CATEGORY_MAP}


class ConfigUpdate(BaseModel):
    auth_token: str = ""
    gh_id: str = ""
    uid: str = ""


@app.post("/api/config")
async def update_config(body: ConfigUpdate):
    cfg = load_config()
    if body.auth_token:
        cfg["auth_token"] = body.auth_token.strip()
    if body.gh_id:
        cfg["gh_id"] = body.gh_id.strip()
    if body.uid:
        cfg["uid"] = body.uid.strip()
    save_config(cfg)
    info = core.token_info(cfg)
    return {"ok": True, "token_info": info}


class FetchParams(BaseModel):
    category_ids: list[int] = []
    upstream_types: list[int] = [2, 3]
    days_range: int = 7


@app.post("/api/fetch")
async def fetch_activities(params: FetchParams):
    if not _fetch_lock.acquire(blocking=False):
        raise HTTPException(409, "正在抓取中，请稍候")

    cfg = load_config()
    if not cfg.get("auth_token"):
        _fetch_lock.release()
        raise HTTPException(400, "未配置 Token")

    cat_ids = params.category_ids or core.ALL_CATEGORY_IDS

    real_q = queue.Queue()

    def run_fetch():
        try:
            inner_q = queue.Queue()
            log_entries = load_log()
            threading.Thread(
                target=lambda: core.fetch_activities(
                    cfg, inner_q, cat_ids, params.upstream_types, params.days_range,
                    log_entries=log_entries),
                daemon=True,
            ).start()
            while True:
                item = inner_q.get()
                if item is None:
                    real_q.put(None)
                    break
                if item.get("type") == "done":
                    if "items" in item:
                        save_activities(item["items"])
                    real_q.put(item)
                    real_q.put(None)
                    break
                real_q.put(item)
        except Exception as e:
            real_q.put({"type": "error", "msg": str(e)})
            real_q.put(None)
        finally:
            _fetch_lock.release()

    threading.Thread(target=run_fetch, daemon=True).start()

    async def generator():
        while True:
            try:
                item = real_q.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.05)
                continue
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/activities")
async def get_activities():
    return {"items": load_activities()}


class OpenParams(BaseModel):
    act_ids: list[int] = []
    dry_run: bool = False
    delay: float = 1.0


@app.post("/api/open")
async def open_groups(params: OpenParams):
    if not _open_lock.acquire(blocking=False):
        raise HTTPException(409, "正在开团中，请稍候")

    cfg = load_config()
    if not cfg.get("auth_token"):
        _open_lock.release()
        raise HTTPException(400, "未配置 Token")

    act_ids = params.act_ids
    if not act_ids:
        acts = load_activities()
        act_ids = [a["actId"] for a in acts]

    real_q = queue.Queue()

    def run():
        try:
            inner_q = queue.Queue()
            threading.Thread(
                target=lambda: core.batch_open_groups(cfg, act_ids, inner_q, params.dry_run, params.delay),
                daemon=True,
            ).start()
            while True:
                item = inner_q.get()
                if item is None:
                    real_q.put(None)
                    break
                if item.get("type") == "item":
                    if item.get("ok") and not params.dry_run:
                        from datetime import datetime
                        append_log({
                            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "actId": item["actId"],
                            "newActId": item.get("newActId"),
                            "actName": item.get("actName", ""),
                            "status": "ok",
                        })
                    elif not item.get("ok"):
                        from datetime import datetime
                        append_log({
                            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "actId": item["actId"],
                            "actName": item.get("actName", ""),
                            "status": "fail",
                            "error": item.get("error", ""),
                        })
                if item.get("type") == "done":
                    real_q.put(item)
                    real_q.put(None)
                    break
                real_q.put(item)
        except Exception as e:
            real_q.put({"type": "error", "msg": str(e)})
            real_q.put(None)
        finally:
            _open_lock.release()

    threading.Thread(target=run, daemon=True).start()

    async def generator():
        while True:
            try:
                item = real_q.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.05)
                continue
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/log")
async def get_log():
    log = load_log()
    return {"items": list(reversed(log))}


@app.delete("/api/log")
async def clear_log():
    LOG_FILE.write_text("[]", encoding="utf-8")
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8765, reload=False)
