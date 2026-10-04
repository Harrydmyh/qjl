"""群接龙开团助手 - Web 服务"""

import asyncio
import json
import os
import queue
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
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

ACCESS_COOKIE = "qjl_key"


def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return {"auth_token": "", "gh_id": "", "uid": ""}


def access_key() -> str:
    return (load_config().get("access_key") or "").strip()


def strict_access_key() -> bool:
    """严格模式：设了 access_key 就**一律**要求密钥（连本机也不放行）。

    为什么需要：把后端挂上隧道后，请求是从本机 127.0.0.1 转进来的，
    而反代/隧道**可能把 Host 改写回 127.0.0.1**（Go 的 httputil 反代默认就会），
    那样「本机免密钥」的白名单就可能被穿透。挂隧道时务必打开本开关。
    """
    return bool(load_config().get("strict_access_key"))


# 反向代理/隧道会带上的来源头。走隧道时「对端」也是 127.0.0.1，
# 所以光看 IP 分不出本机还是外部，必须靠这些头来区分。
_PROXY_HDRS = ("x-forwarded-for", "x-forwarded-host", "x-real-ip", "cf-connecting-ip")
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _hostname(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    if v.startswith("["):                      # [::1]:8765
        return v[1:v.index("]")].lower() if "]" in v else v.lower()
    return v.split(":")[0].lower()


def is_local_request(request: Request) -> bool:
    """是不是「本机直连」—— 只有本机才享受免密钥。

    四个条件全满足才算（**从严**，宁可让本机多带一次密钥）：
      ① 对端是回环地址；
      ② 没有任何代理头（有就说明是隧道/反代转发来的）；
      ③ Host 是回环主机名（本地页面访问必然是 127.0.0.1/localhost）；
      ④ 没有跨站 Origin —— 浏览器发起的跨域请求一定带 Origin，
         而本机自己的页面要么不带、要么 Origin 就是 127.0.0.1。

    注意 ③ 不能单独依赖：部分反代会把 Host 改写成上游地址，
    所以挂隧道时还要开 `strict_access_key`（见上）。
    """
    host = (request.client.host if request.client else "") or ""
    if host not in _LOOPBACK_HOSTS:
        return False
    if any(request.headers.get(h) for h in _PROXY_HDRS):
        return False
    if _hostname(request.headers.get("host") or "") not in _LOOPBACK_HOSTS:
        return False
    origin = request.headers.get("origin")
    if origin and _hostname(origin.split("//")[-1]) not in _LOOPBACK_HOSTS:
        return False
    return True


@app.middleware("http")
async def access_gate(request: Request, call_next):
    """访问密钥闸门。

    为什么需要：这个服务**能真的开团**，还能改写账号凭证（`POST /api/config`）。
    一旦挂到内网或公网，谁打开页面谁就能用你的账号做事。
    所以只要 config 里设了 `access_key`，**非本机**的请求都必须带密钥。

    · 本机直连（回环对端 + 无代理头 + 回环 Host + 非跨站 Origin）→ 免密钥，
      本地预览面板和书签照旧能用；
    · 其它来源（局域网 IP、隧道转发、挂在 Vercel 上的前端）→ 必须带密钥；
    · `strict_access_key=true` 时连本机也要密钥（挂隧道时建议打开，见函数注释）。

    三种带法任选：cookie / `?k=密钥` / 请求头 `X-Access-Key`。
    跨域（前端挂 Vercel）时**必须用请求头** —— 第三方 Cookie 会被浏览器拦掉。
    带 `?k=` 访问一次后端会自动种 cookie，同源场景下之后就不用再带了。

    ⚠️ 刻意**不用 HTTP Basic**：用户是在嵌入式预览面板里操作，
    浏览器原生的用户名/密码弹窗会被静默吞掉（项目里踩过这个坑，见 MEMORY）。

    没设 access_key 时**完全不生效**（默认状态），保持本地单机使用的原样。
    """
    key = access_key()
    # CORS 预检不带自定义头，必须放行（正常由最外层的 CORS 中间件直接应答，这里是兜底）
    if request.method == "OPTIONS":
        return await call_next(request)
    if not key:
        return await call_next(request)
    if not strict_access_key() and is_local_request(request):
        return await call_next(request)
    given = (request.cookies.get(ACCESS_COOKIE)
             or request.query_params.get("k")
             or request.headers.get("X-Access-Key")
             or "")
    if given != key:
        return JSONResponse(
            {"detail": "需要访问密钥。请在网址后面加 ?k=你的密钥 再打开。"},
            status_code=401)
    resp = await call_next(request)
    if request.query_params.get("k") == key:
        resp.set_cookie(ACCESS_COOKIE, key, max_age=180 * 24 * 3600,
                        httponly=True, samesite="lax")
    return resp


@app.get("/api/access")
async def access_status():
    """当前访问密钥状态（不返回密钥本身）"""
    k = access_key()
    return {"enabled": bool(k), "length": len(k), "strict": strict_access_key()}


class StrictReq(BaseModel):
    strict: bool = True


@app.post("/api/access/strict")
async def access_strict(req: StrictReq):
    """开关「严格模式」：开了之后连本机访问也要密钥。

    挂隧道 / 前端挂公网托管时**建议打开** —— 反代可能把 Host 改写回 127.0.0.1，
    那样「本机免密钥」的白名单就有被穿透的风险。
    """
    cfg = load_config()
    cfg["strict_access_key"] = bool(req.strict)
    save_config(cfg)
    return {"ok": True, "strict": bool(req.strict)}


@app.post("/api/access/rotate")
async def access_rotate():
    """生成 / 轮换访问密钥（旧密钥立即失效）。

    密钥没设时可直接调用（此时服务本来就谁都能访问）；
    已设密钥时会被上面的闸门拦截，必须带着旧密钥才能换。
    """
    cfg = load_config()
    cfg["access_key"] = secrets.token_urlsafe(18)
    save_config(cfg)
    return {"ok": True, "access_key": cfg["access_key"]}


@app.post("/api/access/disable")
async def access_disable():
    """关掉访问密钥（回到本地单机、无鉴权的状态）"""
    cfg = load_config()
    cfg.pop("access_key", None)
    save_config(cfg)
    return {"ok": True}


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


def _goods_meta(act_ids) -> dict:
    """查这些 actId 的 goodsIds / goodsNames（先查本地 activities.json，查不到再打接口）。

    为什么要单独抽出来：写 open_log 时**必须带上 goodsIds + goodsNames**，
    5 天同款冷却是靠它们认出「同一款货被别的供应商重新上架 / 标题被改过」的。
    本地的 activities.json 可能是旧快照（没有 goodsNames 字段），所以查不到就现打一次详情。
    """
    wanted = [int(a) for a in dict.fromkeys(act_ids or [])]
    meta = {}
    by_id = {a.get("actId"): a for a in load_activities() if a.get("actId")}
    missing = []
    for aid in wanted:
        a = by_id.get(aid)
        if not a:
            missing.append(aid)
            continue
        names = a.get("goodsNames") or ([a["goodsName"]] if a.get("goodsName") else [])
        meta[aid] = {"goodsIds": a.get("goodsIds") or [], "goodsNames": names}
    if missing:
        try:
            cfg = load_config()
            details = core._fetch_details_batch(
                core.make_session(), core.make_headers(cfg), cfg["gh_id"], missing)
            for it in details:
                meta[it.get("actId")] = {
                    "goodsIds": core._item_goods_ids(it),
                    "goodsNames": core._item_names(it),
                }
        except Exception:
            pass
    return meta


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
    # 禁止缓存：页面里是内联 JS，浏览器拿到旧的 index.html 会跟新接口对不上，
    # 表现为「按钮点了没反应」。宁可每次都重新读盘。
    return HTMLResponse(
        html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@app.get("/api/config")
async def get_config():
    cfg = load_config()
    info = core.token_info(cfg)
    return {"config": {k: v for k, v in cfg.items() if k not in ("auth_token", "access_key")},
            "token_preview": cfg.get("auth_token", "")[:20] + "..." if cfg.get("auth_token") else "",
            "token_info": info,
            "access_enabled": bool(cfg.get("access_key")),
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
    dates: list[str] = []   # ["YYYY-MM-DD", ...] 按具体某几天拉取；给了就忽略 days_range
    include_followed: bool = True   # 保留「我已跟过」的活动（默认保留；设 False 可主动排除）
    keywords: list[str] = []        # 供应商名/关键词，服务端搜索，只拉匹配的活动


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

            def work():
                # 关键：不管 fetch_activities 正常结束还是抛异常，都必须给 inner_q
                # 放一个结束哨兵。否则 run_fetch 会永远阻塞在 inner_q.get()，
                # 下面的 finally 不执行 → _fetch_lock 永久泄漏，
                # 之后所有 /api/fetch 都返回 409 直到重启服务（已踩过一次）。
                try:
                    core.fetch_activities(
                        cfg, inner_q, cat_ids, params.upstream_types, params.days_range,
                        log_entries=log_entries, dates=params.dates or None,
                        include_followed=params.include_followed,
                        keywords=params.keywords or None)
                except Exception as e:
                    inner_q.put({"type": "error", "msg": f"{type(e).__name__}: {e}"})
                finally:
                    inner_q.put(None)

            threading.Thread(target=work, daemon=True).start()
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


class OpenJobParams(BaseModel):
    act_ids: list[int] = []
    dry_run: bool = False
    delay: float = 3.0            # 两条之间的基准间隔（秒）
    jitter: float = 0.0           # 在 delay 上 ±jitter 秒随机浮动
    start_at: Optional[str] = None  # 本地时间 ISO 串（如 2026-09-17T00:00），空 = 立即开始


# ── 开团任务：服务端执行，支持定时开始、随时取消、页面重开可接回 ──

JOB_EVENT_LIMIT = 3000

_job = {
    "id": 0,
    "state": "idle",        # idle | scheduled | running | done | cancelled | failed
    "start_ts": None,       # 计划开始时间（epoch 秒）
    "remaining": None,      # 距开始还有几秒（仅 scheduled）
    "total": 0,
    "index": 0,
    "ok": 0,
    "fail": 0,
    "current": None,
    "next_wait": None,
    "dry_run": False,
    "delay": 0.0,
    "jitter": 0.0,
    "error": None,
    "created_at": None,
    "finished_at": None,
    "events": [],
}
_job_lock = threading.Lock()
_job_cancel = threading.Event()


def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _job_update(**kw):
    with _job_lock:
        _job.update(kw)


def _job_push(kind, msg):
    with _job_lock:
        _job["events"].append({"t": datetime.now().strftime("%H:%M:%S"),
                               "kind": kind, "msg": msg})
        if len(_job["events"]) > JOB_EVENT_LIMIT:
            del _job["events"][:len(_job["events"]) - JOB_EVENT_LIMIT]


def _job_snapshot(with_events_from=None):
    with _job_lock:
        snap = {k: v for k, v in _job.items() if k != "events"}
        snap["event_count"] = len(_job["events"])
        if with_events_from is not None:
            snap["events"] = _job["events"][with_events_from:]
    return snap


def _job_thread(job_id, act_ids, dry_run, delay, jitter, start_ts):
    """等到达开始时间（可取消）→ 执行开团 → 累计事件与计数"""
    try:
        # 1) 等待到点
        while True:
            if _job_cancel.is_set() or _job["id"] != job_id:
                _job_update(state="cancelled", finished_at=_now_str(),
                            remaining=None, next_wait=None)
                _job_push("info", "已取消，未开始开团")
                return
            remaining = start_ts - time.time()
            if remaining <= 0:
                break
            _job_update(remaining=int(remaining) + 1)
            time.sleep(min(1.0, remaining))

        _job_update(state="running", remaining=None)
        _job_push("info", f"开始开团：共 {len(act_ids)} 个，"
                          f"间隔 {delay:g}s" + (f" ± {jitter:g}s" if jitter else ""))

        # 开团成功时要记进 open_log 的两项：活动里所有商品的 goodsId 与商品名。
        # goodsId 会因为「同款被别的供应商重新上架」而变，所以商品名也必须记 ——
        # 冷却判定靠它认出「不同供应商的同款」（见 core._finish_fetch）。
        # ⚠️ 曾经这里只算了 goods_by_act / names_by_act，names_by_act 从没被写进日志，
        # 结果**新开的团在日志里没有 goodsNames**，5 天冷却只剩 actId/goodsId 两种命中方式，
        # 「同款换了个供应商重新上架」就认不出来了 —— 表现为同一款反复被开。
        goods_meta = _goods_meta(act_ids)
        inner_q = queue.Queue()

        def should_stop():
            return _job_cancel.is_set() or _job["id"] != job_id

        def work():
            # 同 /api/fetch：必须保证结束哨兵一定被放入，
            # 否则 batch_open_groups 抛异常时 _job_thread 会永远阻塞在 inner_q.get()，
            # _open_lock 永久泄漏（开团按钮一直禁用 / 后续都 409）。
            try:
                core.batch_open_groups(
                    load_config(), act_ids, inner_q, dry_run, delay, jitter, should_stop)
            except Exception as e:
                _job_push("err", f"任务异常：{type(e).__name__}: {e}")
                _job_update(state="failed", error=f"{type(e).__name__}: {e}",
                            finished_at=_now_str())
            finally:
                inner_q.put(None)

        threading.Thread(target=work, daemon=True).start()

        ok = fail = idx = 0
        aborted = False
        while True:
            item = inner_q.get()
            if item is None:
                break
            t = item.get("type")

            if t == "item":
                if item.get("ok") and not dry_run:
                    _meta = goods_meta.get(item["actId"]) or {}
                    append_log({
                        "time": _now_str(),
                        "actId": item["actId"],
                        "newActId": item.get("newActId"),
                        "actName": item.get("actName", ""),
                        "goodsIds": _meta.get("goodsIds") or [],
                        # 这一项是 5 天同款冷却的关键，别漏写（曾经就漏了）
                        "goodsNames": _meta.get("goodsNames") or [],
                        "status": "ok",
                    })
                elif not item.get("ok"):
                    append_log({
                        "time": _now_str(),
                        "actId": item["actId"],
                        "actName": item.get("actName", ""),
                        "status": "fail",
                        "error": item.get("error", ""),
                    })
                idx = item["index"]
                if item.get("ok"):
                    ok += 1
                else:
                    fail += 1
                _job_update(index=idx, ok=ok, fail=fail, next_wait=None,
                            current=item.get("actName") or str(item["actId"]))
                _job_push("ok" if item.get("ok") else "err",
                          f"[{idx}/{len(act_ids)}] "
                          + (f"✓ {item.get('actName')}"
                             + (" [dry]" if dry_run else f" → {item.get('newActId')}")
                             if item.get("ok")
                             else f"✗ {item.get('actName')}: {item.get('error')}"))

            elif t == "wait":
                _job_update(next_wait=item["seconds"])
                _job_push("info", f"等待 {item['seconds']:g} 秒后开下一条 …")

            elif t == "aborted":
                aborted = True

            elif t == "error":
                _job_push("err", item.get("msg", "未知错误"))

            elif t == "done":
                _job_push("ok", f"完成：✓{item.get('success', 0)}  ✗{item.get('fail', 0)}")
                break

        _job_update(state="cancelled" if aborted else "done",
                    finished_at=_now_str(), next_wait=None, current=None)
    except Exception as e:
        _job_update(state="failed", error=str(e), finished_at=_now_str())
        _job_push("err", f"任务异常: {e}")
    finally:
        _open_lock.release()


@app.post("/api/open/start")
async def open_start(params: OpenJobParams):
    if not _open_lock.acquire(blocking=False):
        raise HTTPException(409, "已有开团任务在排队或运行中，请先取消")

    cfg = load_config()
    if not cfg.get("auth_token"):
        _open_lock.release()
        raise HTTPException(400, "未配置 Token")

    act_ids = params.act_ids or [a["actId"] for a in load_activities()]
    if not act_ids:
        _open_lock.release()
        raise HTTPException(400, "没有可开团的活动")

    start_ts = time.time()
    if params.start_at:
        try:
            start_ts = datetime.fromisoformat(params.start_at.strip()).timestamp()
        except ValueError:
            _open_lock.release()
            raise HTTPException(400, "开始时间格式不正确")
        if start_ts < time.time() - 5:
            _open_lock.release()
            raise HTTPException(400, "开始时间已过，请重新选择")

    with _job_lock:
        _job["id"] += 1
        job_id = _job["id"]
        _job["events"] = []
        _job.update({
            "state": "scheduled" if params.start_at else "running",
            "start_ts": start_ts,
            "remaining": max(0, int(start_ts - time.time())) if params.start_at else None,
            "total": len(act_ids), "index": 0, "ok": 0, "fail": 0,
            "current": None, "next_wait": None, "error": None,
            "dry_run": params.dry_run,
            "delay": params.delay, "jitter": params.jitter,
            "created_at": _now_str(), "finished_at": None,
        })
    _job_cancel.clear()

    threading.Thread(
        target=_job_thread,
        args=(job_id, act_ids, params.dry_run, params.delay, params.jitter, start_ts),
        daemon=True,
    ).start()

    return _job_snapshot()


@app.post("/api/open/cancel")
async def open_cancel():
    _job_cancel.set()
    _job_push("info", "收到取消指令 …")
    return _job_snapshot()


@app.get("/api/open/status")
async def open_status(since: int = 0):
    return _job_snapshot(with_events_from=max(0, since))


@app.get("/api/log")
async def get_log():
    log = load_log()
    return {"items": list(reversed(log))}


@app.delete("/api/log")
async def clear_log():
    LOG_FILE.write_text("[]", encoding="utf-8")
    return {"ok": True}


class MarkReq(BaseModel):
    actIds: list


@app.post("/api/log/mark")
async def mark_logged(req: MarkReq):
    """手动把活动记成「我已经开过」→ 纳入 5 天同款冷却。

    为什么需要这个：本工具只能看到**帮卖广场**，看不到「你自己在群接龙里开的团」。
    实测全库里 ghId 等于自己账号的活动数是 0，广场接口也不提供自己的团列表。
    所以在工具之外开的团不会进 open_log，冷却认不出来，同一款货就会被反复开
    （用户 2026-09-23 报的「印花卷边毛圈卫衣」4 天开了 4 次就是这种情况）。

    标记一次后，同款（按 actId / goodsId / 商品名，含改名与跨供应商）5 天内都不会再进入开团范围，
    满 5 天自动恢复。
    """
    meta = _goods_meta(req.actIds)
    now = _now_str()
    marked, empty = [], []
    for aid in dict.fromkeys(int(a) for a in (req.actIds or [])):
        m = meta.get(aid) or {}
        if not (m.get("goodsIds") or m.get("goodsNames")):
            # 拿不到商品信息也记，但只能靠 actId 命中冷却，要告诉用户
            empty.append(aid)
        append_log({
            "time": now,
            "actId": aid,
            "newActId": None,
            "actName": "",
            "goodsIds": m.get("goodsIds") or [],
            "goodsNames": m.get("goodsNames") or [],
            "status": "ok",
            "manual": True,        # 不是本工具开的团，是用户手动标记的
        })
        marked.append(aid)
    return {"ok": True, "marked": marked, "weak": empty}


# ── CORS ────────────────────────────────────────────────────────────────
# 前端可以挂到 Vercel / CloudStudio 这类静态托管上，页面里的请求会跨域打到这台机器，
# 所以必须开 CORS —— 否则浏览器直接拦掉，连 401 都看不到。
#
# 两个要点：
# ① **必须最后 add_middleware**：Starlette 的 user_middleware 是 insert(0)，
#    最后加的会变成最外层。CORS 放最外层才能给「闸门返回的 401」也补上 CORS 头，
#    否则前端只会看到一个不可读的跨域错误、拿不到 detail。
# ② 预检（OPTIONS）不带自定义头，会被密钥闸门挡掉 → 由最外层的 CORS 直接应答，
#    不会走到闸门（`access_gate` 里也额外放行了 OPTIONS 兜底）。
#
# 允许的源默认 `*`：跨域时前端用 `X-Access-Key` 请求头带密钥（不依赖 Cookie，
# 避免被浏览器拦第三方 Cookie），所以密钥仍然是硬门槛。
# 想收紧就在 config.json 里加 `cors_origins`（数组或逗号分隔字符串）。
_cors = load_config().get("cors_origins")
if isinstance(_cors, str):
    _cors = [x.strip() for x in _cors.split(",") if x.strip()]
if not _cors:
    _cors = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8765, reload=False)
