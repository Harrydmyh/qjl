"""群接龙「售后」清单探测脚本。

从本地工具里独立出来，直接打群接龙接口，不依赖 server.py / fastapi。

用法：
    # 用 data/config.json 里的凭证（默认查自家 gh_id）
    python3 after_sale_probe.py

    # 查指定团长（另一个账号），并打印可直接复制的信息块
    python3 after_sale_probe.py --gh-id PsXXXXXX_bsXXXXXX_xxxxxxxx --copy

    # 只看处理中的（asStatus=10/20），限制 10 条
    python3 after_sale_probe.py --status 10 20 --limit 10

    # 临时换个 token（避免改 data/config.json）
    QJL_TOKEN=<jwt> python3 after_sale_probe.py --gh-id ... --copy

    # 推送到企业微信群（先 --dry 预览，再真发）
    WECOM_KEY=xxxx python3 after_sale_probe.py --gh-id ... --wecom --per-msg 5 --dry
    WECOM_KEY=xxxx python3 after_sale_probe.py --gh-id ... --wecom --per-msg 5

    # 导出 CSV
    python3 after_sale_probe.py --out 售后清单.csv

凭证优先级：--token / 环境变量 QJL_TOKEN > data/config.json 的 auth_token。
"""

import argparse
import csv
import json
import os
import sys
import time

BASE_URL = "https://apipro.qunjielong.com"
SEARCH_PATH = "/after-sales-biz/as_order_search/v2/search"
STATUS_NUM_PATH = "/after-sales-biz/as_order_search/v2/as_status_num"
CONDITIONS_PATH = "/after-sales-biz/gh_admin_search/conditions"

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "config.json")

# 售后入口页自己的 mini-route：不强制，但带上更像真实小程序
MINI_ROUTE = "pro/pages/order-manage/order-after-sales-manage/order-after-sales-manage"

AS_STATUS_TEXT = {
    10: "待处理",
    20: "待买家处理",
    30: "已取消",
    40: "已拒绝",
    50: "待平台介入",
    60: "已关闭",
    61: "待举证",
    63: "平台处理中",
    120: "已完成",
}
AS_TYPE_TEXT = {1: "仅退款", 2: "退货退款", 3: "换货"}


def load_config(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[warn] 读配置失败 {path}: {e}", file=sys.stderr)
        return {}


def build_session():
    """沿用 core.make_session()（已屏蔽会话级代理），拿不到就自己建一个等价的。"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import core
        return core.make_session()
    except Exception:
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        s = requests.Session()
        s.trust_env = False
        s.proxies.clear()
        forced = os.environ.get("QJL_PROXY", "").strip()
        if forced:
            s.proxies.update({"http": forced, "https": forced})
        retry = Retry(total=3, backoff_factor=1.5, status_forcelist=[500, 502, 503, 504],
                      allowed_methods=["GET", "POST"])
        a = HTTPAdapter(max_retries=retry)
        s.mount("https://", a)
        s.mount("http://", a)
        return s


def build_headers(token, uid):
    return {
        "Host": "apipro.qunjielong.com",
        "Authorization": token,
        "appid": "wx059cd327295ab444",
        "client-version": "6.2.84",
        "companyid": "190",
        "content-type": "application/json",
        "device-type": "10",
        "feature-tag": "f0000",
        "mini-route": MINI_ROUTE,
        "scenecode": "1005",
        "uid": str(uid),
        "version": "3.0.0",
        "charset": "utf-8",
        "Referer": "https://servicewechat.com/wx059cd327295ab444/2326/page-frame.html",
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 12; SM-F926U Build/V417IR; wv) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0.0 Chrome/150.0.7871.189 "
            "Safari/537.36 XWEB/1500117 MMWEBSDK/20260604 MMWEBID/1213 "
            "MicroMessenger/8.0.77.3160(0x28004D30) WeChat/arm64 Weixin NetType/WIFI "
            "Language/zh_CN ABI/arm64 MiniProgramEnv/android"
        ),
    }


def post(session, headers, path, body):
    r = session.post(BASE_URL + path, headers=headers, json=body, timeout=30)
    j = r.json()
    if j.get("code") != 200:
        raise RuntimeError(f"{path} -> code={j.get('code')} msg={j.get('msg')}")
    return j.get("data") or {}


def search(session, headers, gh_id, statuses, page=1, page_size=20, view_type=10, keyword=""):
    body = {
        "ghId": gh_id,
        "ghViewType": view_type,
        "searchContent": keyword,
        "sort": 110,                     # 110=售后创建时间倒序，100=正序
        "asStatusList": list(statuses),
        "asReturnGoodsStatusList": [],
        "searchType": 10,
        "orderSelectType": 0,
        "page": page,
        "pageSize": page_size,
    }
    return post(session, headers, SEARCH_PATH, body)


def fetch_all(session, headers, gh_id, statuses, view_type, limit=None, page_size=50):
    rows, page = [], 1
    while True:
        d = search(session, headers, gh_id, statuses, page=page, page_size=page_size,
                   view_type=view_type)
        batch = d.get("orderSearchInfoList") or []
        rows += batch
        if limit and len(rows) >= limit:
            return rows[:limit]
        if not d.get("hasNextPage") or not batch:
            break
        page += 1
    return rows


def money(v):
    try:
        return f"{int(v) / 100:.2f}"
    except Exception:
        return "-"


def goods_lines(row):
    out = []
    for oi in row.get("orderItemList") or []:
        name = oi.get("goodsName") or ""
        unit = oi.get("goodsUnit")
        spec = f"（{unit}）" if unit else ""
        out.append(f"{name}{spec} ×{oi.get('buyQuantity', 0)} ¥{money(oi.get('groupPrice'))}")
    return out


def md_block(row):
    state = AS_STATUS_TEXT.get(row.get("asStatus"), row.get("asStatus"))
    color = "warning" if row.get("asStatus") == 10 else "info"
    kind = AS_TYPE_TEXT.get(row.get("asType"), row.get("asType"))
    lines = [f'> <font color="{color}">**{state}**</font> · {kind} · {row.get("ghName") or ""}']
    lines.append(f'> **{row.get("linkMan","")}** {row.get("linkPhone","")}')
    for g in goods_lines(row):
        lines.append(f"> {g}")
    lines.append(f'> 地址：{row.get("addr","")}')
    if row.get("asContent"):
        lines.append(f'> 原因：{row.get("asContent")}')
    lines.append(f'> 订单号 `{row.get("orderNo","")}`')
    lines.append(f'> 售后单号 `{row.get("asOrderNo","")}`')
    return "\n".join(lines)


def md_chunks(rows, budget=3600, head="售后待处理清单"):
    """按字节预算把记录拆成多条消息（企微 markdown 正文上限 4096 字节）。"""
    import datetime
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    batches, cur = [], []
    for r in rows:
        trial = cur + [r]
        size = sum(len(md_block(x).encode("utf-8")) + 2 for x in trial)
        if cur and size > budget:
            batches.append(cur)
            cur = [r]
        else:
            cur = trial
    if cur:
        batches.append(cur)
    total = len(batches)
    out = []
    for i, b in enumerate(batches, 1):
        title = f"# {head}" + (f"（{i}/{total}批）" if total > 1 else "")
        sub = f"共 {len(rows)} 单，本次第 {i}/{total} 批 · {stamp}"
        out.append("\n".join([title, sub, ""] + [md_block(x) for x in b]))
    return out, total


def wecom_send(session, key, content):
    url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + key
    r = session.post(url, json={"msgtype": "markdown", "markdown": {"content": content}},
                     timeout=20)
    return r.json()


def copy_text(row):
    """「复制信息」的文本模板。

    群接龙小程序里这一步是纯前端动作（埋点里的 handleCopyOrderData，无任何网络请求），
    格式无从抓取 —— 所以这里按我们自己要的字段拼。
    """
    lines = [f"【售后】{AS_STATUS_TEXT.get(row.get('asStatus'), row.get('asStatus'))}"]
    lines += goods_lines(row)
    lines.append(f"订单号：{row.get('orderNo','')}")
    lines.append(f"售后单号：{row.get('asOrderNo','')}")
    lines.append(f"收货人：{row.get('linkMan','')} {row.get('linkPhone','')}")
    lines.append(f"地址：{row.get('addr','')}")
    if row.get("asContent"):
        lines.append(f"售后原因：{row.get('asContent')}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="群接龙售后清单探测")
    ap.add_argument("--config", default=CONFIG_PATH, help="data/config.json 路径")
    ap.add_argument("--token", default=None, help="覆盖 JWT（也可设环境变量 QJL_TOKEN）")
    ap.add_argument("--gh-id", default=None, help="要查的团长 ghId，默认取 config 的 gh_id")
    ap.add_argument("--status", nargs="*", type=int, default=[10, 20],
                    help="售后状态码，默认 10 20（处理中的）；空用 --all-status")
    ap.add_argument("--all-status", action="store_true", help="不过滤状态，拉全部（量可能上千）")
    ap.add_argument("--view", type=int, default=10,
                    help="10=别人向我/我的下级发起的售后（默认）；20=我向上游发起的售后")
    ap.add_argument("--limit", type=int, default=None, help="最多几条")
    ap.add_argument("--copy", action="store_true", help="额外打印每单的可复制信息块")
    ap.add_argument("--copy-only", action="store_true", help="只打印可复制信息块")
    ap.add_argument("--out", default=None, help="导出 CSV 路径")
    ap.add_argument("--wecom", action="store_true", help="推送到企业微信群（消息推送/群机器人）")
    ap.add_argument("--wecom-key", default=None,
                    help="webhook key 或完整 URL；不给则读环境变量 WECOM_KEY 或 config 的 wecom_key")
    ap.add_argument("--per-msg", type=int, default=5, help="单条群消息最多几单（默认 5）")
    ap.add_argument("--dry", action="store_true", help="只打印要发的 markdown，不真发")
    args = ap.parse_args()

    cfg = load_config(args.config)
    token = args.token or os.environ.get("QJL_TOKEN") or cfg.get("auth_token", "")
    uid = cfg.get("uid", "")
    gh_id = args.gh_id or cfg.get("gh_id", "")
    if not token:
        sys.exit("没有 token：--token 或环境变量 QJL_TOKEN，或填 data/config.json 的 auth_token")
    if not gh_id:
        sys.exit("没有 ghId：--gh-id 或 data/config.json 的 gh_id")

    session = build_session()
    headers = build_headers(token, uid)
    statuses = [] if args.all_status else args.status

    try:
        num = post(session, headers, STATUS_NUM_PATH, {
            "asStatusList": [], "ghId": gh_id, "ghViewType": args.view,
            "searchType": 10, "orderSelectType": 0})
    except Exception as e:
        sys.exit(f"拿状态数失败（多半是 token 过期了）：{e}")

    if not args.copy_only:
        print(f"ghId: {gh_id}   view={args.view}")
        print("未完结售后数：", "  ".join(
            f"{AS_STATUS_TEXT.get(int(k), k)}={v}" for k, v in sorted(num.items())))

    rows = fetch_all(session, headers, gh_id, statuses, args.view, limit=args.limit)
    if not rows:
        print("没有符合条件的售后单")
        return

    if args.out:
        with open(args.out, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["售后单号", "订单号", "状态", "售后类型", "创建时间", "收货人", "电话",
                        "地址", "商品", "规格", "数量", "金额", "售后原因"])
            for r in rows:
                for oi in (r.get("orderItemList") or [{}]):
                    w.writerow([r.get("asOrderNo"), r.get("orderNo"),
                                AS_STATUS_TEXT.get(r.get("asStatus"), r.get("asStatus")),
                                AS_TYPE_TEXT.get(r.get("asType"), r.get("asType")),
                                r.get("asCreateTime"), r.get("linkMan"), r.get("linkPhone"),
                                r.get("addr"), oi.get("goodsName"), oi.get("goodsUnit"),
                                oi.get("buyQuantity"), money(oi.get("groupPrice")),
                                r.get("asContent")])
        print(f"已导出 {args.out}（{len(rows)} 单）")

    if args.copy_only:
        for i, r in enumerate(rows):
            if i:
                print("\n" + "-" * 40)
            print(copy_text(r))
        return

    print(f"\n共 {len(rows)} 条：")
    for i, r in enumerate(rows, 1):
        oi = (r.get("orderItemList") or [{}])[0]
        print(f"{i:>3}. [{AS_STATUS_TEXT.get(r.get('asStatus'), r.get('asStatus'))}] "
              f"{AS_TYPE_TEXT.get(r.get('asType'), r.get('asType'))} | "
              f"{r.get('linkMan')} {r.get('linkPhone')} | "
              f"{oi.get('goodsName')} {oi.get('goodsUnit') or ''} ×{oi.get('buyQuantity')} | "
              f"{r.get('asOrderNo')}")
        if len(r.get("orderItemList") or []) > 1:
            print("      └ 本单多件：" + " / ".join(goods_lines(r)[1:]))
        if args.copy:
            print("      " + "\n      ".join(copy_text(r).split("\n")))

    if args.wecom:
        raw = args.wecom_key or os.environ.get("WECOM_KEY") or cfg.get("wecom_key", "")
        if not raw:
            sys.exit("没有 webhook key：--wecom-key / 环境变量 WECOM_KEY / config 的 wecom_key")
        key = raw.strip().split("key=")[-1] if "key=" in raw else raw.strip()
        if args.per_msg > 0:
            rows = rows[:args.per_msg]
        chunks, total = md_chunks(rows, head="售后待处理清单")
        print(f"\n准备推送 {len(chunks)} 条群消息（key 尾号 …{key[-6:]}）")
        for i, content in enumerate(chunks, 1):
            size = len(content.encode("utf-8"))
            print(f"  第 {i}/{len(chunks)} 条：{size} 字节 {'⚠️ 超 4096' if size > 4096 else '✅'}")
            if args.dry:
                print(content)
                continue
            res = wecom_send(session, key, content)
            ok = res.get("errcode") == 0
            print(f"    -> errcode={res.get('errcode')} {res.get('errmsg')} {'✅' if ok else '❌'}")
            if not ok:
                break
            if i < len(chunks):
                time.sleep(3)   # 远离 20 条/分钟上限


if __name__ == "__main__":
    main()
