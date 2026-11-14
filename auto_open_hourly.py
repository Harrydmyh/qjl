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
TANG_GH = "PsHHJN8_bsMtSbS_b09f0984"
ACCOUNT_FILE = os.path.join(DATA, "open_account.json")


def load_account():
    """开团账号配置：优先 data/open_account.json（网页上能改），缺啥回退 config.json。

    两个号分工：config.json 里那个常年是「拉数据用」的号；真正开团用哪个、
    token 是哪个号抓的，都放 open_account.json，换号不用动主配置。
    """
    cfg = json.load(open(os.path.join(DATA, "config.json"), encoding="utf-8"))
    try:
        with open(ACCOUNT_FILE, encoding="utf-8") as f:
            extra = json.load(f)
    except Exception:
        extra = {}
    if (extra.get("authToken") or "").strip():
        cfg["auth_token"] = extra["authToken"].strip()
    if (extra.get("uid") or "").strip():
        cfg["uid"] = str(extra["uid"]).strip()
    cfg["gh_id"] = (extra.get("ghId") or "").strip() or TANG_GH
    cfg["account_name"] = (extra.get("name") or "").strip()
    return cfg

HOUR_START, HOUR_END = 7, 22
MAX_PER_RUN = 1          # 一次只开 1 条：间隔交给排期里的分钟级时间点，不靠一口气连开


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


# ---------------------------------------------------------------- open_log 同步
# ⚠️ 曾经这里只写 open_plan_log.jsonl，**没写 data/open_log.json**，
# 结果 11 月自动开了十几条，前端「开团记录」页一条都不显示，
# 看起来像"根本没开成功"（实际团好好地在线）。
# 顺带还有个隐患：5 天同款冷却是靠 open_log 里的 goodsIds/goodsNames 认的，
# 不进 open_log 就等于自动开团不受冷却约束，同一款会被反复开。
OPEN_LOG = os.path.join(DATA, "open_log.json")


def _goods_meta(cfg, act_id):
    """拿母团的 goodsIds / goodsNames。本地快照优先，没有就现查详情。"""
    try:
        acts = json.load(open(os.path.join(DATA, "activities.json")))
    except Exception:
        acts = []
    if isinstance(acts, dict):
        acts = acts.get("items") or acts.get("activities") or []
    for a in acts:
        if a.get("actId") == int(act_id):
            names = a.get("goodsNames") or ([a["goodsName"]] if a.get("goodsName") else [])
            ids = a.get("goodsIds") or ([a["goodsId"]] if a.get("goodsId") else [])
            if names:
                return ids, names
    try:
        got = core._fetch_details_batch(core.make_session(), core.make_headers(cfg),
                                       TANG_GH, [int(act_id)])
        if got:
            return core._item_goods_ids(got[0]), core._item_names(got[0])
    except Exception:
        pass
    return [], []


def _append_open_log(entry):
    """往 open_log.json 追加一条。加文件锁 + 临时文件 rename，别把 JSON 写坏。"""
    lock = None
    try:
        import fcntl
        lock = open(OPEN_LOG + ".lock", "w")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            log = json.load(open(OPEN_LOG))
        except Exception:
            log = []
        # 同一 newActId 只记一次，重复跑不会灌水
        nid = entry.get("newActId")
        if nid and any(str(x.get("newActId")) == str(nid) for x in log):
            return False
        log.append(entry)
        tmp = OPEN_LOG + ".tmp"
        with open(tmp, "w") as f:
            json.dump(log, f, ensure_ascii=False, indent=2)
        os.replace(tmp, OPEN_LOG)
        return True
    except Exception as e:
        say(f"⚠️ 写 open_log 失败：{e}")
        return False
    finally:
        if lock:
            try:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_UN)
            except Exception:
                pass
            lock.close()


def _due_min(it):
    """条目该开的时刻（0 点起的分钟数）。新计划用 slotMin，老计划退回 slot 小时。"""
    if it.get("slotMin") is not None:
        return int(it["slotMin"])
    t = it.get("time") or ""
    if ":" in t:
        h, m = t.split(":")[:2]
        return int(h) * 60 + int(m)
    return int(it.get("slot") or 0) * 60


def due_items(plan, now):
    """到点该开的：日期更早的（补课）或今天且时间点已到。按时间先后排队。"""
    today = now.date().isoformat()
    cur = now.hour * 60 + now.minute
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
        elif d == today and _due_min(it) <= cur:
            out.append(it)
    out.sort(key=lambda x: (x.get("date") or "", _due_min(x), -int(x.get("orders") or 0)))
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


def own_acts(cfg=None, max_age=1800):
    """主页团（含创建时间），半小时内复用本地缓存。"""
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
        _OWN = D.refresh_own(cfg)
    except Exception as e:
        say(f"⚠️ 拉主页团失败（{e}），本轮只按子团记录判重")
        _OWN = []
    return _OWN


def precheck(cfg, headers, it):
    """这条能不能开。返回 (ok, 原因, 证据)。

    两道：
      1. 子团记录 —— 该号给这个母团开过的子团，5 天内创建过就不开（最硬）；
      2. 主页团名 —— 近 5 天创建的团里有没有同名的（跟团不产生子团记录，只能按名字认）。
    主页团只在第 1 道没命中时才拉，省时间。
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
    gh = cfg.get("gh_id") or TANG_GH
    try:
        r = s.post("https://apipro.qunjielong.com/activity-accessory/gh_activity/list_gh_child_activities",
                   json={"ghId": gh, "pactId": aid, "page": 1, "pageSize": 50},
                   headers=headers, timeout=15)
        d = (r.json() or {}).get("data")
        kids = d if isinstance(d, list) else ((d or {}).get("list") or (d or {}).get("records") or [])
    except Exception:
        kids = []
    young = [k for k in kids if (k.get("createTime") or 0) >= cutoff]
    if young:
        k = max(young, key=lambda x: x.get("createTime") or 0)
        return False, "5天内已开", f"子团 {k.get('actId')}（{datetime.fromtimestamp((k.get('createTime') or 0)/1000):%m-%d %H:%M}）"

    own = own_acts(cfg)
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


def run(now, dry=0, limit=MAX_PER_RUN, only=None):
    cfg = load_account()

    plan = load_plan()
    if not plan:
        say("没有计划文件 data/open_plan_tang.json，先生成周报")
        return 0

    todo = due_items(plan, now)
    pend_all = sum(1 for x in plan["items"] if x.get("status") == "pending")
    if not todo:
        say(f"本时段无到期条目（剩余 {pend_all} 条待开）")
        return 0

    # 调度器指定了「就开这一条」（用于避免落后时一次补一堆），就不自作主张换别的
    if only is not None:
        picked = [x for x in todo if x.get("actId") == only]
        if not picked:
            say(f"本条已不在待开里（可能刚被开过），跳过：{only}")
            return 0
        todo = picked

    batch = todo[: limit * 3]      # 多取一些：被查重挡掉的号不占本波名额
    if dry:
        say(f"[dry] 现在该开 {len(todo)} 条，本次先开 {limit} 条（候选 {len(batch)}）:")
        for it in batch[:limit]:
            say(f"  {it['date']} {it.get('time') or ''} [{it['cat']}] {it['supplier'][:8]} {it['name'][:26]}")
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
            # 模仿主界面（选品跟团）：通过 core.open_one_group 发布到「选品界面 -> 我发布的接龙」
            good, new_id, name, err = core.open_one_group(cfg, aid, dry_run=False)
            if not good and ("模板" in str(err) or "5830" in str(err)):
                say(f"ℹ️ 选品跟团未成({err})，尝试回退帮卖方式(Q.open_child)")
                good_c, new_id_c, name_c, err_c = Q.open_child(cfg, aid, phone, dry_run=False)
                if good_c:
                    good, new_id, name, err = good_c, new_id_c, name_c or name, ""
        except Exception as e:
            good, new_id, name, err = False, None, it.get("name"), f"异常: {e}"
        if good:
            it["status"], it["newActId"] = "opened", new_id
            it["note"], it["doneAt"] = "", datetime.now().isoformat(timespec="seconds")
            ok += 1
            # 同步进 open_log：前端记录页要靠它，5 天同款冷却也靠它
            gids, gnames = _goods_meta(cfg, aid)
            _append_open_log({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "actId": aid,
                "newActId": new_id,
                "actName": it.get("actName") or name or "",
                "goodsIds": gids,
                "goodsNames": gnames,
                "status": "ok",
            })
            say(f"✅ {it.get('time','')} {it['cat']} | {it['supplier'][:8]} | {it['name'][:24]} → {new_id}")
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


def backfill(cfg):
    """把计划表里已开、但 open_log.json 里没有的条目补记进去。

    用于修复历史遗漏（比如脚本还没写 open_log 那段时期开的团）。
    重复执行安全：已在 open_log 的会跳过。
    """
    plan = load_plan() or {}
    try:
        cur = json.load(open(OPEN_LOG))
    except Exception:
        cur = []
    have = {str(x.get("newActId")) for x in cur if x.get("newActId")}
    todo = [x for x in plan.get("items", [])
            if x.get("status") == "opened" and x.get("newActId")
            and str(x["newActId"]) not in have]
    if not todo:
        say(f"open_log 已是最新（{len(cur)} 条），无需补录")
        return 0
    say(f"要补录 {len(todo)} 条…")
    n = 0
    for it in todo:
        gids, gnames = _goods_meta(cfg, it["actId"])
        if not gnames:
            say(f"⏭ 跳过 {it.get('name','')[:20]}：拿不到商品名，写空记录会破坏同款冷却")
            continue
        okk = _append_open_log({
            "time": it.get("doneAt", "").replace("T", " ")[:19] or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "actId": int(it["actId"]),
            "newActId": it["newActId"],
            "actName": it.get("actName") or it.get("name") or "",
            "goodsIds": gids,
            "goodsNames": gnames,
            "status": "ok",
        })
        if okk:
            n += 1
            say(f"＋ {it.get('name','')[:24]} → {it['newActId']}（{len(gids)} 个商品）")
        time.sleep(0.5)
    say(f"补录完成：{n} 条")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", type=int, default=0, help="只看要开哪几条，不发请求")
    ap.add_argument("--now", type=int, default=None, help="假装现在几点（测试）")
    ap.add_argument("--limit", type=int, default=MAX_PER_RUN)
    ap.add_argument("--force", action="store_true", help="忽略时间窗口（手工补开用）")
    ap.add_argument("--backfill", action="store_true",
                    help="只补录 open_log（把已开但没记录条的补进去），不开新团")
    args = ap.parse_args()

    if args.backfill:
        cfg = json.load(open(os.path.join(DATA, "config.json")))
        cfg["gh_id"] = TANG_GH
        return backfill(cfg)

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
