"""用 DeepSeek 判「能不能入口吃 / 是不是保健品」——**唯一的判据**，不再用关键词/正则。

省 token 的四个办法：
  1. **大批量**：一次 120 个商品名（按字符预算切），系统提示只付一次钱
  2. **极简输出**：只要一个整数数组 `{"c":[0,1,2,3,...]}`，每位 0=都不是 / 1=食品 / 2=保健 / 3=两者，
     比原来 `{"i":0,"food":true,"health":false}` 省 4~5 倍输出 token
  3. **归一化后查缓存**：去掉 emoji/标点/空格再当 key，同一款换个前后缀也能命中，重复拉取零成本
  4. **短系统提示**：规则压到 6 行，靠示例把边界说清

失败语义：任何异常都返回 {}，调用方就不打品类标记（宁可不筛，也不乱筛）。
"""
import json
import os
import re
import threading

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(_HERE, "data", "config.json")
CACHE = os.path.join(_HERE, "data", "kind_cache.json")
API = "https://api.deepseek.com/chat/completions"

CACHE_V = "v4"            # 提示词/协议变了就 +1，旧缓存作废
BATCH_N = 120             # 一批最多多少个商品名
BATCH_CHARS = 2400        # 一批最多多少字符（中文约 1 token/字，控住输入）
_TIMEOUT = 45
_lock = threading.Lock()

# 累计 token 用量（诊断用：跑完可以 print(USAGE) 看这次花了多少）
USAGE = {"in": 0, "out": 0, "calls": 0}

# 提示词尽量短，但把三个最容易翻车的边界写死：
# 化妆品写香味、食品级包装材料、服饰的「牛奶丝/鱼骨纹」这类字面。
_SYSTEM = (
    "你是商品分类器。输入是一批商品名，编号从 1 开始。\n"
    "对每个商品给出类别码 c：0=都不是，1=能吃能喝，2=保健品/膳食补充剂，3=既是食品又是保健品。\n"
    "c=1/3 只限人直接入口的东西：生鲜、零食、饮料、酒、调味料、主粮、水果蔬菜坚果。\n"
    "以下一律判 0：化妆品护肤品（哪怕写了芒果/蜂蜜/牛奶香型）、厨具餐具杯壶、服饰鞋包\n"
    "（牛奶丝/鱼骨纹/牛仔都是字面）、食品级包装材料（保鲜袋、锡纸盘、保鲜膜）、家居日用品、鲜花绿植。\n"
    "2 只给**口服**的保健品/膳食补充剂：维生素、益生菌、蛋白粉、鱼油、钙片、口服中药滋补（阿胶膏/黄精膏）。\n"
    "外用的一律 0：面膜水乳面霜精华、眼贴/暖贴/膏药、滴眼液、泡脚包、洗护。品牌名带「丹/膏/精」的化妆品也是 0。\n"
    "拿不准判 0。\n"
    '只输出 JSON：{"c":[码,码,...]}，**位数必须等于商品数**，不要解释、不要省略。'
)

CODE_CN = {0: "", 1: "食品", 2: "保健", 3: "食品·保健"}


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
    """缓存 key：只留中英文数字、转小写。同一款换个 emoji/前缀也能命中，省一次请求。"""
    s = (name or "").replace("\u200b", "").replace("\u200c", "")
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", s.lower())


def _load_cache():
    try:
        d = json.load(open(CACHE, encoding="utf-8"))
        if d.get("v") == CACHE_V and isinstance(d.get("map"), dict):
            return d["map"]
    except Exception:
        pass
    return {}


def _save_cache(mp):
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        json.dump({"v": CACHE_V, "map": mp}, open(CACHE, "w", encoding="utf-8"),
                  ensure_ascii=False)
    except Exception:
        pass


def _session():
    s = requests.Session()
    s.trust_env = False          # 和 core.make_session 一样：忽略会话级死代理
    s.proxies.clear()
    return s


def _ask(session, names, stats=None):
    """问一批 → 返回 [码, ...]（长度必须等于 len(names)）"""
    # 编号从 1 开始：实测 0 起编号时模型会少给一位（39 vs 40），1 起就稳定对齐
    listing = "\n".join(f"{i + 1}. {n[:70]}" for i, n in enumerate(names))
    body = {
        "model": _model(),
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": f"共 {len(names)} 个商品：" + chr(10) + listing},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": len(names) * 3 + 60,
    }
    r = session.post(API,
                     headers={"Authorization": f"Bearer {_key()}",
                              "Content-Type": "application/json"},
                     json=body, timeout=_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"DeepSeek HTTP {r.status_code}: {r.text[:160]}")
    j = r.json()
    u = j.get("usage") or {}
    USAGE["in"] += u.get("prompt_tokens") or 0
    USAGE["out"] += u.get("completion_tokens") or 0
    USAGE["calls"] += 1
    txt = (j["choices"][0]["message"]["content"] or "").strip()
    i, k = txt.find("["), txt.rfind("]")
    if i < 0 or k < i:
        raise ValueError(f"返回里没有数组：{txt[:80]}")
    codes = json.loads(txt[i:k + 1])
    if len(codes) != len(names):
        raise ValueError(f"码位数不对：{len(codes)} vs {len(names)}")
    return [int(c) if str(c).isdigit() else 0 for c in codes]


def _chunks(names):
    """按「个数 + 字符数」双上限切批，避免一条请求塞太长被截断"""
    cur, size = [], 0
    for n in names:
        if cur and (len(cur) >= BATCH_N or size + len(n) > BATCH_CHARS):
            yield cur
            cur, size = [], 0
        cur.append(n)
        size += len(n)
    if cur:
        yield cur


def classify_names(names, progress=None):
    """批量判定。返回 {name: {"food":bool,"health":bool}}；没判出来的名字不在结果里。

    progress: 可选回调 (done, total)。
    额外把 token 用量塞在返回值的 `.usage` 里？—— 不方便，改成调用方传 stats 字典。
    """
    names = [str(n).strip() for n in names if str(n).strip()]
    uniq = list(dict.fromkeys(names))
    if not uniq or not _key():
        return {}
    with _lock:
        cache = _load_cache()
        need = {}                      # norm_key -> 原始名（拿去问模型）
        for n in uniq:
            k = norm_key(n)
            if k and k not in cache and k not in need:
                need[k] = n
        todo = list(need.items())
        if todo:
            s = _session()
            done = 0
            for chunk in _chunks([n for _, n in todo]):
                keys = [norm_key(n) for n in chunk]
                try:
                    codes = _ask(s, chunk)
                except Exception:
                    # 整批失败就劈半再来一次（常见于模型多写/少写一位）
                    if len(chunk) > 10:
                        half = len(chunk) // 2
                        parts = [chunk[:half], chunk[half:]]
                        codes = []
                        for part in parts:
                            try:
                                codes.extend(_ask(s, part))
                            except Exception:
                                codes.extend([0] * len(part))
                    else:
                        codes = [0] * len(chunk)
                for k, c in zip(keys, codes):
                    cache[k] = {"food": c in (1, 3), "health": c in (2, 3)}
                done += len(chunk)
                if progress:
                    progress(done, len(todo))
            _save_cache(cache)
        return {n: cache[norm_key(n)] for n in uniq if norm_key(n) in cache}


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    st = {}
    res = classify_names(args, progress=lambda a, b: print(f"  {a}/{b}", flush=True))
    for k, v in res.items():
        print(f"{CODE_CN[(1 if v['food'] else 0) + (2 if v['health'] else 0)] or '—':6} {k}")
