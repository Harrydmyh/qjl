"""售后自动路由：新售后一出现，就按「供应商名字」转发到对应的企业微信群。

设计要点
--------
1. **为什么路由表是手工铺垫的**：群机器人 webhook 拿不到群名、拿不到 chat_id，
   每个群的 key 只能在该群里手动建机器人获得；API 也无「最近通讯时间」字段。
   另一条路「创建企业群发 add_msg_template」虽然支持 chat_id_list，但官方明写
   *需要成员确认后才会执行发送* —— 达不到「第一时间自动转发」，故不采用。
   → 所以：**建一次机器人、贴一次 key，之后全自动**。重复劳动只有一次。

2. **供应商名字从哪来**：售后记录里 100% 带两个名字
   `ghName`（成交团）和 `orderChainGhDTO.parentGhName`（上家），
   群里那个关键字（抬头见喜/番薯哥/7妹…）通常就是其中之一。
   我们拿这两个名字去路由表里做**包含匹配**。
   ⚠️ `sourceOfGoodsGhIds`（货源团）虽然字面才是供应商，但**没有任何接口能取到它的名字**。

3. **重名怎么选**：一个关键字绑定了多个群时，
   用 `preferred` 字段记住你挑过的那个（`-–pick` 命令）；没挑过就先丢到默认群（汇总群），
   并把候选列出来等你挑一次 —— 绝不乱猜。

用法
----
    # 登记：把某个供应商对接群的 webhook key 存进来
    python3 after_sale_watch.py --bind "抬头见喜" --key <webhook-key-or-url> --label "抬头见喜售后对接群"

    # 一个关键字有多个群时，指定用哪个（之后就永久记住）
    python3 after_sale_watch.py --pick "抬头见喜" --key <webhook-key>

    # 看看现有路由表
    python3 after_sale_watch.py --show

    # 试跑一轮（不发旧的，只推新出现的）
    QJL_TOKEN=<jwt> python3 after_sale_watch.py --gh-id <ghId> --once --dry

    # 常驻监听，每 120 秒查一次新的
    QJL_TOKEN=<jwt> python3 after_sale_watch.py --gh-id <ghId> --interval 120

凭证：群接龙 JWT 走 `--token` 或环境变量 QJL_TOKEN 或 data/config.json；
路由表放 **data/wecom_route.json**（已在 .gitignore 内，别外传）。
"""

import argparse
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
ROUTE_FILE = os.path.join(ROOT, "data", "wecom_route.json")
SEEN_FILE = os.path.join(ROOT, "data", "as_seen.json")
UNROUTED_FILE = os.path.join(ROOT, "data", "as_unrouted.json")

BASE_URL = "https://apipro.qunjielong.com"
SEARCH_PATH = "/after-sales-biz/as_order_search/v2/search"
WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="

AS_STATUS_TEXT = {10: "待处理", 20: "待买家处理"}
AS_TYPE_TEXT = {1: "仅退款", 2: "退货退款", 3: "换货"}


def _norm(s):
    """名字归一化：去掉空白/emoji 影响，方便包含匹配。"""
    s = (s or "").strip()
    s = re.sub(r"[\s　]+", "", s)
    s = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]", "", s)
    return s.lower()


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def extract_key(raw):
    raw = (raw or "").strip()
    return raw.split("key=")[-1] if "key=" in raw else raw


def normalize_routes(routes):
    """把不同写法都整理成规范形式，让映射表怎么顺手怎么写。

    suppliers 支持三种写法，混着写也行：
        "抬头见喜": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"   # 单个群，最简
        "番薯哥":   ["key1", "https://...key=key2"]                              # 多个群
        "7妹":      [{"label": "7妹对接群", "key": "xxx", "preferred": true}]     # 完整写法
    """
    out = {}
    for kw, val in (routes.get("suppliers") or {}).items():
        groups = []
        if isinstance(val, str):
            val = [val]
        if isinstance(val, dict):
            val = [val]
        for i, v in enumerate(val or []):
            if isinstance(v, str):
                groups.append({"label": f"{kw}群{i+1}", "key": extract_key(v)})
            else:
                g = dict(v)
                g["key"] = extract_key(g.get("key", ""))
                g.setdefault("label", f"{kw}群{i+1}")
                groups.append(g)
        if len(groups) > 1 and sum(1 for g in groups if g.get("preferred")) > 1:
            for g in groups[1:]:          # 防止手写成多个 preferred
                g["preferred"] = False
        out[kw] = groups
    return out


def supplier_names(row):
    """这条售后里可能代表「供应商」的名字候选（按可信度排序）。"""
    chain = row.get("orderChainGhDTO") or {}
    out = []
    for n in (chain.get("parentGhName"), row.get("ghName")):
        n = (n or "").strip()
        if n and n not in out:
            out.append(n)
    return out


def money(v):
    try:
        return f"{int(v) / 100:.2f}"
    except Exception:
        return "-"


def goods_lines(row):
    out = []
    for oi in row.get("orderItemList") or []:
        unit = oi.get("goodsUnit")
        spec = f"（{unit}）" if unit else ""
        out.append(f"{oi.get('goodsName','')}{spec} ×{oi.get('buyQuantity',0)} ¥{money(oi.get('groupPrice'))}")
    return out


def md_block(row):
    state = AS_STATUS_TEXT.get(row.get("asStatus"), row.get("asStatus"))
    color = "warning" if row.get("asStatus") == 10 else "info"
    kind = AS_TYPE_TEXT.get(row.get("asType"), row.get("asType"))
    sups = " / ".join(supplier_names(row)) or "-"
    lines = [
        f'> <font color="{color}">**{state}**</font> · {kind} · `{row.get("asOrderNo","")}`',
        f'> 供应商：<font color="comment">{sups}</font>',
        f'> **{row.get("linkMan","")}** {row.get("linkPhone","")}',
    ]
    lines += [f"> {g}" for g in goods_lines(row)]
    lines.append(f'> 地址：{row.get("addr","")}')
    if row.get("asContent"):
        lines.append(f'> 原因：{row.get("asContent")}')
    lines.append(f'> 订单号 `{row.get("orderNo","")}`')
    return "\n".join(lines)


def resolve_target(routes, row):
    """返回 (targets, keyword, need_pick)。targets 为空表示没匹配到关键字。"""
    sups = supplier_names(row)
    for kw, groups in routes.get("suppliers", {}).items():
        nkw = _norm(kw)
        if not nkw:
            continue
        for name in sups:
            n = _norm(name)
            if nkw and (nkw in n or n in nkw):
                if len(groups) == 1:
                    return [groups[0]], kw, False
                preferred = [g for g in groups if g.get("preferred")]
                if len(preferred) == 1:
                    return [preferred[0]], kw, False
                if len(preferred) > 1:
                    preferred.sort(key=lambda g: g.get("picked_at", 0), reverse=True)
                    return [preferred[0]], kw, False
                return groups, kw, True      # 多个候选，没人选过
    return [], None, False


def wecom_send(session, key, content):
    r = session.post(WEBHOOK + key,
                     json={"msgtype": "markdown", "markdown": {"content": content}},
                     timeout=20)
    try:
        return r.json()
    except Exception:
        return {"errcode": -1, "errmsg": r.text[:120]}


def push(session, key, rows, title="新到售后单"):
    """把若干条售后推到同一个群，自动按字节预算拆包。"""
    if not rows:
        return 0
    import datetime
    stamp = datetime.datetime.now().strftime("%H:%M")
    sent = 0
    for r in rows:
        head = f"# {title}\n> 共 {len(rows)} 单 · {stamp}\n"
        content = head + md_block(r)
        size = len(content.encode("utf-8"))
        if size > 4096:
            content = head + "> ⚠️ 本单内容超长已截断\n" + md_block(r)[:3500]
            size = len(content.encode("utf-8"))
        res = wecom_send(session, key, content)
        ok = res.get("errcode") == 0
        print(f"    {'✅' if ok else '❌'} {size}B -> errcode={res.get('errcode')} {res.get('errmsg')}")
        if not ok:
            break
        sent += 1
        if rows.index(r) < len(rows) - 1:
            time.sleep(3)     # 远离 20 条/分钟上限
    return sent


def fetch_active(session, headers, gh_id, pages=3):
    out = []
    for p in range(1, pages + 1):
        r = session.post(BASE_URL + SEARCH_PATH, headers=headers, json={
            "ghId": gh_id, "ghViewType": 10, "searchContent": "", "sort": 110,
            "asStatusList": [10, 20], "asReturnGoodsStatusList": [],
            "searchType": 10, "orderSelectType": 0, "page": p, "pageSize": 20,
        }, timeout=30)
        data = (r.json() or {}).get("data") or {}
        out += data.get("orderSearchInfoList") or []
        if not data.get("hasNextPage"):
            break
    return out


def build_session():
    sys.path.insert(0, ROOT)
    try:
        import core
        return core.make_session()
    except Exception:
        import requests
        s = requests.Session()
        s.trust_env = False
        s.proxies.clear()
        return s


def main():
    ap = argparse.ArgumentParser(description="按供应商自动把新售后推到企业微信群")
    ap.add_argument("--bind", metavar="KEYWORD", help="登记某供应商对应的群 webhook")
    ap.add_argument("--key", default=None, help="webhook key 或完整 URL")
    ap.add_argument("--label", default=None, help="群名备注，便于人看")
    ap.add_argument("--pick", metavar="KEYWORD", help="某供应商有多个群时，指定默认用哪个 key")
    ap.add_argument("--show", action="store_true", help="打印当前路由表")
    ap.add_argument("--import", dest="imp", metavar="FILE",
                    help="从 CSV/JSON 批量导入（CSV 列：供应商,群名,webhook）")
    ap.add_argument("--check", action="store_true", help="体检：重 silver/缺群课时/重名未指定")
    ap.add_argument("--send-test", action="store_true",
                    help="配合 --check：给每个群发一条测试消息（验证 key 是否还有效）")
    ap.add_argument("--gh-id", default=None)
    ap.add_argument("--token", default=None)
    ap.add_argument("--interval", type=int, default=120, help="轮询间隔秒，默认 120")
    ap.add_argument("--once", action="store_true", help="只跑一轮就退出")
    ap.add_argument("--dry", action="store_true", help="只打印不真发")
    args = ap.parse_args()

    raw = load_json(ROUTE_FILE, {"default": [], "suppliers": {}})
    routes = {"default": raw.get("default") or [], "suppliers": normalize_routes(raw)}
    cfg = load_json(os.path.join(ROOT, "data", "config.json"), {})
    session = build_session()

    if args.check:
        sups = routes["suppliers"]
        print(f"登记表：{len(sups)} 个供应商，共 {sum(len(v) for v in sups.values())} 个群")
        bykey = {}
        for kw, gs in sups.items():
            for g in gs:
                bykey.setdefault(g["key"], []).append(f"{kw}/{g.get('label')}")
        dup = {k: v for k, v in bykey.items() if len(v) > 1}
        if dup:
            print("同一群被多个供应商共用（不算错，但留意会不会重复收消息）：")
            for k, v in dup.items():
                print(f"  …{k[-6:]} <- {', '.join(v)}")
        need = [(kw, gs) for kw, gs in sups.items()
                if len(gs) > 1 and not any(g.get("preferred") for g in gs)]
        if need:
            print("⚠️ 以下供应商有多个群但还没指定默认群：")
            for kw, gs in need:
                opts = " / ".join(f"{g.get('label')}(…{g['key'][-6:]})" for g in gs)
                print(f'  {kw}: [{opts}]  → --pick "{kw}" --key <选中的key>')
        else:
            print("✅ 没有待指定的重名供应商")
        if not routes.get("default"):
            print("⚠️ 没登记汇总群 —— 没匹配到的售后会无处可去")
        if args.send_test:
            sess = build_session()
            for kw, gs in sups.items():
                for g in gs:
                    res = wecom_send(sess, g["key"],
                                     f"# 路由表自检\n> `{kw}` → {g.get('label')} 连通性测试")
                    print(f"  {kw}/{g.get('label')}: errcode={res.get('errcode')} {res.get('errmsg')}")
                    time.sleep(3)
        return

    if args.imp:
        path = args.imp
        rows = []
        if path.lower().endswith(".csv"):
            import csv as _csv
            with open(path, "r", encoding="utf-8-sig") as f:
                for row in _csv.reader(f):
                    if not row or not row[0].strip() or row[0].strip().startswith("#"):
                        continue
                    supplier = row[0].strip()
                    label = (row[1].strip() if len(row) > 1 and row[1].strip() else supplier)
                    key = row[2].strip() if len(row) > 2 else ""
                    if not key and len(row) > 1 and "key=" in row[1]:
                        key = row[1].strip()
                    rows.append((supplier, label, key))
        else:
            for kw, v in load_json(path, {}).items():
                for item in (v if isinstance(v, list) else [v]):
                    key = item if isinstance(item, str) else item.get("key", "")
                    label = kw if isinstance(item, str) else item.get("label", kw)
                    rows.append((kw, label, key))
        added = {}
        for supplier, label, key in rows:
            key = extract_key(key)
            if not key:
                print(f"  跳过 {supplier}：没有 webhook")
                continue
            gs = routes["suppliers"].setdefault(supplier, [])
            if any(g["key"] == key for g in gs):
                continue
            gs.append({"label": label, "key": key, "preferred": not gs,
                       "picked_at": int(time.time()) if not gs else 0})
            added[supplier] = added.get(supplier, 0) + 1
        save_json(ROUTE_FILE, routes)
        print(f"导入完成：新增 {sum(added.values())} 条，覆盖 {len(added)} 个供应商")
        for k, v in added.items():
            print(f"  {k}: +{v} 个群")
        return

    if args.show:
        print(json.dumps(routes, ensure_ascii=False, indent=2))
        return

    if args.bind:
        key = extract_key(args.key or os.environ.get("WECOM_KEY", ""))
        if not key:
            sys.exit("给个 key：--key <webhook key 或完整 URL>")
        groups = routes["suppliers"].setdefault(args.bind, [])
        if any(g["key"] == key for g in groups):
            print("该群已登记过")
            return
        if groups:
            # 关键字下已经有群了再加一个 → 变成“有歧义”，清掉旧的首选，
            # 逼你在下一次运行时用 --pick 挑一次（这正是「人工指定一次」的入口）。
            for g in groups:
                g["preferred"] = False
            added_msg = "该关键字现在有多个群，下次遇到会提示你用 --pick 指定"
        else:
            added_msg = ""
        groups.append({"label": args.label or args.bind, "key": key,
                       "preferred": not groups, "picked_at": int(time.time())})
        save_json(ROUTE_FILE, routes)
        print(f"已登记：{args.bind} -> {args.label or ''}（key …{key[-6:]}）" +
              (f"。{added_msg}" if added_msg else ""))
        return

    if args.pick:
        key = extract_key(args.key or "")
        if not key:
            sys.exit("--pick 需要配合 --key")
        groups = routes["suppliers"].get(args.pick, [])
        hit = [g for g in groups if g["key"] == key]
        if not hit:
            sys.exit(f"关键字 {args.pick!r} 下没有这个 key，先 --bind 登记")
        for g in groups:
            g["preferred"] = False
        hit[0]["preferred"] = True
        hit[0]["picked_at"] = int(time.time())
        save_json(ROUTE_FILE, routes)
        print(f"已记住：{args.pick} 以后优先发往 {hit[0].get('label')}")
        return

    token = args.token or os.environ.get("QJL_TOKEN") or cfg.get("auth_token", "")
    gh_id = args.gh_id or cfg.get("gh_id", "")
    if not token or not gh_id:
        sys.exit("缺凭证：--token / QJL_TOKEN 与 --gh-id")

    headers = {"Authorization": token, "appid": "wx059cd327295ab444",
               "content-type": "application/json", "device-type": "10",
               "uid": str(cfg.get("uid", "")), "client-version": "6.2.84",
               "companyid": "190",
               "mini-route": "pro/pages/order-manage/order-after-sales-manage/order-after-sales-manage"}

    seen = set(load_json(SEEN_FILE, []))
    first_round = not seen
    unrouted = load_json(UNROUTED_FILE, {})

    print("售后自动路由启动（Ctrl+C 停止）")
    while True:
        try:
            rows = fetch_active(session, headers, gh_id)
            fresh = [r for r in rows if r.get("asOrderNo") and r["asOrderNo"] not in seen]
            print(f"[{time.strftime('%H:%M:%S')}] 活跃 {len(rows)} 单，其中新出现 {len(fresh)} 单")

            if first_round and fresh:
                print("  首次运行：把已存在的单记为已见，不补推历史消息")
                fresh = []

            buckets = {}
            for r in fresh:
                targets, kw, need_pick = resolve_target(routes, r)
                if need_pick:
                    opts = " / ".join(f"{g.get('label')}(…{g['key'][-6:]})" for g in targets)
                    print(f"  ⚠️ {kw}: 有 {len(targets)} 个群候选 [{opts}]，还没指定 → 先发汇总群；"
                          f"  指定用：--pick \"{kw}\" --key <选中的key>")
                    targets = []
                if not targets:
                    targets = routes.get("default") or []
                    for n in supplier_names(r):
                        if n not in unrouted:
                            unrouted[n] = {"count": 0}
                        unrouted[n]["count"] += 1
                        unrouted[n]["last"] = r.get("asOrderNo")
                for t in targets:
                    buckets.setdefault(t["key"], {"label": t.get("label", ""), "rows": []})
                    buckets[t["key"]]["rows"].append(r)

            for key, bk in buckets.items():
                print(f"  → {bk['label'] or '汇总群'}(…{key[-6:]}) {len(bk['rows'])} 单")
                if args.dry:
                    for r in bk["rows"]:
                        print(md_block(r))
                else:
                    push(session, key, bk["rows"])

            if fresh:
                seen.update(r["asOrderNo"] for r in fresh)
                save_json(SEEN_FILE, sorted(seen))
                save_json(UNROUTED_FILE, unrouted)

            if args.once:
                break
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n已停止")
            break
        except Exception as e:
            print(f"[异常] {type(e).__name__}: {e}", file=sys.stderr)
            if args.once:
                break
            time.sleep(min(args.interval, 60))

    if unrouted:
        print("\n还没绑定供应商群的名字（可用 --bind 登记）：")
        for n, v in sorted(unrouted.items(), key=lambda x: -x[1]["count"]):
            print(f"  {v['count']:>3} 单  {n}")


if __name__ == "__main__":
    main()
