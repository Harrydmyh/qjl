#!/usr/bin/env python3
"""
catch_token.py —— 站在下游接票，不试图造票。

用法：
    brew install mitmproxy
    mitmdump -s catch_token.py --listen-port 8888

前置三步：
    1) 首次运行 mitmproxy 会生成 ~/.mitmproxy/mitmproxy-ca-cert.pem
       双击装进「系统」钥匙串 → 始终信任
    2) 系统代理指向 127.0.0.1:8888（本机用，直接 networksetup 就行）
    3) Mac 微信里打开群接龙小程序，让它活着

它干的事：小程序每次自己换票（POST /user/login/wx_login_silence）
成功返回的 token，原子写回 data/config.json / data/open_account.json。

不做的事：不改请求、不拦请求、不抢 code。
小程序那边一切照旧，我们只是把它换出来的票复印一份。
code 一次性、5 分钟就死，硬抢只会让它自己登录失败，毫无收益。
"""

import json
import os
import time

from mitmproxy import http

ROOT = os.path.dirname(os.path.abspath(__file__))
TARGETS = [
    os.path.join(ROOT, "data", "config.json"),
    os.path.join(ROOT, "data", "open_account.json"),
]
LOG = os.path.join(ROOT, "data", "token_catch.log")
TOKEN_KEYS = ("auth_token", "token", "access_token")


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def patch_one(path: str, token: str, uid) -> bool:
    """把 token 原子写回某个 json。返回是否真的改了。"""
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(d, dict):
        return False

    key = next((k for k in TOKEN_KEYS if k in d), None)
    if not key:
        return False
    if d.get(key) == token:
        return False

    d[key] = token
    if uid and "uid" in d:
        d["uid"] = uid

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)  # 原子替换，服务端不会读到半个文件
    return True


def response(flow: http.HTTPFlow) -> None:
    if "wx_login_silence" not in flow.request.path:
        return
    try:
        body = flow.response.json()
    except (ValueError, AttributeError):
        return

    data = body.get("data") or {}
    token = data.get("token")
    if not token:
        log(f"换票失败 code={body.get('code')} msg={body.get('msg')}")
        return

    uid = data.get("uid")
    hit = [os.path.basename(p) for p in TARGETS if patch_one(p, token, uid)]
    log(f"拿到新票 uid={uid} 已写回={hit or '无变化（与现有 token 相同）'}")


def load(_loader) -> None:
    log("接票器已就位，等 wx_login_silence …")
