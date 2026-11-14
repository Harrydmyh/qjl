#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把商品名归到 4 个大类：食品 / 日用 / 服饰美妆 / 杂货。

为什么要 LLM 而不是关键词：品类词表永远补不完，而且中文商品名的坑很多
（「牛奶丝」是面料、「芒果香型」是沐浴露、「食品级保鲜袋」是日用品）。
用分类模型一次判完，再落盘缓存，同一款只花一次钱。

输出码：1=食品 2=日用 3=服饰美妆 4=杂货（**必居其一**，没有"判不出"）。
四类是为了排期均匀——每天都要有吃的、有用的、穿的、杂的。

缓存 data/cat_cache.json：{v, map:{归一化名: 码}}
"""
import json
import os
import re
import threading

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(_HERE, "data", "config.json")
CACHE = os.path.join(_HERE, "data", "cat_cache.json")
API = "https://api.deepseek.com/chat/completions"

CACHE_V = "cat-v1"
BATCH_N = 120
BATCH_CHARS = 2400
_TIMEOUT = 60
_lock = threading.Lock()

CAT_NAME = {1: "食品", 2: "日用", 3: "服饰美妆", 4: "杂货"}
CAT_ORDER = [1, 2, 3, 4]

_SYSTEM = (
    "你是商品类目分类器。输入一批商品名，编号从 1 开始。\n"
    "每个商品必须归到下面四类之一，输出码 c：\n"
    "1=食品：人能吃能喝的东西，生鲜果蔬肉蛋水产、零食、饮料酒水、调味料、粮油主食、坚果干货、茶叶咖啡、冷冻速食。\n"
    "2=日用：清洁洗护（洗衣液/纸品/洗发沐浴/牙膏）、厨具餐具杯壶、收纳家居、家纺（床单被套毛巾）、小家电、母婴用品、宠物用品、五金工具、外用膏贴/暖贴、鲜花绿植。\n"
    "3=服饰美妆：衣服鞋袜帽箱包内衣、饰品珠宝、美妆护肤彩妆香水、美发美甲、防晒。\n"
    "4=杂货：除上面三类之外的其它——文具玩具、数码配件、摆件装饰、文具办公、汽车用品、运动户外小件、节庆用品。\n"
    "关键边界（重要）：\n"
    "· 「牛奶丝/鱼骨纹/牛仔/羊羔绒」这类是面料，判 3；带水果香型的沐浴露洗发水判 2；\n"
    "· 食品级包装材料（保鲜袋/锡纸/保鲜膜/一次性餐具）判 2；\n"
    "· 带食品字样的保健品（维生素/益生菌/蛋白粉/阿胶糕）判 1；\n"
    "· 宠物粮判 2（宠物用品），人吃的肉干判 1；\n"
    "· 拿不准就往 4（杂货）放，别空着。\n"
    '只输出 JSON：{"c":[码,码,...]}，位数必须等于商品数，不要解释。'
)


def _key():
    k = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if k:
        return k
    try:
        return (json.load(open(CONFIG, encoding="utf-8")).get("deepseek_api_key") or "").strip()
    except Exception:
        return ""


def _model():
    try:
        return json.load(open(CONFIG, encoding="utf-8")).get("deepseek_model") or "deepseek-chat"
    except Exception:
        return "deepseek-chat"


def norm_key(name):
    s = (name or "").replace("\u200b", "").replace("\u200c", "")
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", s.lower())


def _load():
    try:
        d = json.load(open(CACHE, encoding="utf-8"))
        if d.get("v") == CACHE_V and isinstance(d.get("map"), dict):
            return d["map"]
    except Exception:
        pass
    return {}


def _save(mp):
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        json.dump({"v": CACHE_V, "map": mp}, open(CACHE, "w", encoding="utf-8"),
                  ensure_ascii=False)
    except Exception:
        pass


def _session():
    s = requests.Session()
    s.trust_env = False
    s.proxies.clear()
    return s


def _ask(session, names):
    listing = "\n".join(f"{i + 1}. {n[:70]}" for i, n in enumerate(names))
    body = {
        "model": _model(),
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": f"共 {len(names)} 个商品：\n{listing}"},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": len(names) * 3 + 60,
    }
    r = session.post(API, headers={"Authorization": f"Bearer {_key()}",
                                   "Content-Type": "application/json"},
                     json=body, timeout=_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
    txt = (r.json()["choices"][0]["message"]["content"] or "").strip()
    i, k = txt.find("["), txt.rfind("]")
    if i < 0 or k < i:
        raise ValueError("无数组")
    codes = json.loads(txt[i:k + 1])
    if len(codes) != len(names):
        raise ValueError(f"位数不对 {len(codes)} vs {len(names)}")
    out = []
    for c in codes:
        try:
            c = int(c)
        except Exception:
            c = 4
        out.append(c if c in (1, 2, 3, 4) else 4)
    return out


def _chunks(names):
    cur, size = [], 0
    for n in names:
        if cur and (len(cur) >= BATCH_N or size + len(n) > BATCH_CHARS):
            yield cur
            cur, size = [], 0
        cur.append(n)
        size += len(n)
    if cur:
        yield cur


def classify(names, progress=None):
    """→ {原名: 码1..4}；判不出来的一律归 4（杂货），保证排期不缺类。"""
    names = [str(n).strip() for n in names if str(n).strip()]
    uniq = list(dict.fromkeys(names))
    if not uniq:
        return {}
    with _lock:
        cache = _load()
        need = {}
        for n in uniq:
            k = norm_key(n)
            if k and k not in cache and k not in need:
                need[k] = n
        todo = list(need.items())
        if todo:
            if not _key():
                return {n: cache.get(norm_key(n), 4) for n in uniq}
            s = _session()
            done = 0
            for chunk in _chunks([n for _, n in todo]):
                keys = [norm_key(n) for n in chunk]
                try:
                    codes = _ask(s, chunk)
                except Exception:
                    codes = None
                if codes is None and len(chunk) > 10:
                    half = len(chunk) // 2
                    codes = []
                    for part in (chunk[:half], chunk[half:]):
                        try:
                            codes.extend(_ask(s, part))
                        except Exception:
                            codes.extend([None] * len(part))
                for k, c in zip(keys, codes or []):
                    if c:
                        cache[k] = c
                done += len(chunk)
                if progress:
                    progress(min(done, len(todo)), len(todo))
            _save(cache)
        return {n: cache.get(norm_key(n), 4) for n in uniq}


if __name__ == "__main__":
    import sys
    res = classify(sys.argv[1:])
    for k, v in res.items():
        print(f"{CAT_NAME[v]:4} {k}")
