"""价格与链接的归一化 —— 纯函数，便于自检直接喂真实样本。

为什么单独成模块
----------------
价格在页面上的**渲染形态**和它的**数值**不是一回事，而三个平台的形态还不同：

    ¥ ⏎ 2 ⏎ .30 ⏎ 万       → 23000     （闲鱼：整数/小数/单位被拆成三行）
    ¥ ⏎ 18 ⏎ .88           → 18.88
    ¥ ⏎ 4030               → 4030
    ¥4599.00               → 4599      （京东：价格在一个节点里）

2026-10-01 实测（闲鱼搜索「RTX 4090 24G」，卡片 innerText 原文）：

    华硕RTX4090+24G猛禽满血版… ⏎ 37分钟前发布 ⏎ ¥ ⏎ 2 ⏎ .30 ⏎ 万 ⏎ 1人想要 ⏎ 山西

旧实现只取「¥ 的下一行」→ 拿到 `"2"` → **把 2.3 万的显卡记成 ¥2.00**。
实测那一页 30 条里有 4 条中招；而这些假低价随后会被数据质量闸门当垃圾丢掉，
净效果是**所有万元级二手报价被静默丢弃**（闲鱼库里 max 恰好卡在 10000）。

所以：JS 只负责把「¥ 之后的连续价格片段」原样取出来，**拼装与判解读懂放这里** ——
JS 里没法写单测，这里可以。
"""

from __future__ import annotations

import re

# 全角数字 → 半角（页面偶尔混用）
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９．，", "0123456789.,")

# 价格单位。万/w 是闲鱼高价商品的实际用法（W 大小写都出现过）
_UNIT_MULTIPLIERS: dict[str, float] = {
    "万": 10_000.0,
    "w": 10_000.0,
    "W": 10_000.0,
    "k": 1_000.0,
    "K": 1_000.0,
}

# 一个数字：允许千分位逗号、允许省略整数部分（.88）
_NUM_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?|\.\d+)")
# 单位必须**紧跟在数字之后**（"万" 出现在"万图师"这种品牌名里时不能被当单位）
_UNIT_RE = re.compile(r"(?<=[\d.])(万|[wWkK])")

# 需要剥掉的前后缀噪声
_NOISE_RE = re.compile(r"[¥￥$]|起|元起|元|包邮|含邮|到手价|券后")


def parse_price(text: object, *, unit_multiplier_hint: float | None = None) -> float | None:
    """把一段价格文本解析成数值。

    Args:
        text: 价格文本（可为 None / 数字 / 字符串）。
        unit_multiplier_hint: 外部已知的单位倍率（一般不用）。

    Returns:
        正的价格，或 None（解析不出 / 非正数）。

    Examples:
        >>> parse_price("¥1,234.56")
        1234.56
        >>> parse_price("2.30万")
        23000.0
        >>> parse_price(".88")
        0.88
        >>> parse_price("¥")
        >>> parse_price("面议")
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text) if text > 0 else None

    s = str(text).strip().translate(_FULLWIDTH_DIGITS)
    if not s:
        return None

    s = _NOISE_RE.sub("", s)

    # ---- 单位：只认「紧跟在数字后面」的万/w/k ----
    mult = 1.0
    unit = _UNIT_RE.search(s)
    if unit:
        mult = _UNIT_MULTIPLIERS.get(unit.group(1), 1.0)
        s = s[: unit.start()] + s[unit.end():]
    if unit_multiplier_hint:
        mult *= unit_multiplier_hint

    m = _NUM_RE.search(s)
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None

    value *= mult
    return value if value > 0 else None


# 片段形状：首段是数字，后续段只能是「小数部分」或「万」。
#
# ⚠️ 形状校验必须在 **Python 侧**（而不是只在 JS 的循环条件里）——
#    否则"把「56人想拼」的 56 拼成 268856"这类错误无法写单测，
#    只能靠读 JS 相信它。校验放这里，JS 退化成"尽可能多取几行"。
_SEG_FIRST_RE = re.compile(r"^(\d[\d,]*|\d+\.\d+|\.\d+)$")
_SEG_CONT_RE = re.compile(r"^(\.\d+|万)$")
_MAX_SEGMENTS = 4


def parse_split_price(segments: object, *, unit_multiplier_hint: float | None = None) -> float | None:
    """把「被拆成多行的价格片段」拼回一个数。

    闲鱼会把 `2.30万` 渲染成三行（`2` / `.30` / `万`），把 `18.88` 渲染成两行
    （`18` / `.88`）。这里把片段拼起来再交给 `parse_price`。

    **形状校验在此处**：首段必须是数字，后续段只能是小数部分或「万」——
    遇到别的就停（例如 `["2688", "56"]` 只取 `2688`，不会拼成 268856）。

    Args:
        segments: 片段列表，如 `["2", ".30", "万"]`；也接受字符串/None。

    Examples:
        >>> parse_split_price(["2", ".30", "万"])
        23000.0
        >>> parse_split_price(["18", ".88"])
        18.88
        >>> parse_split_price(["2688", "56"])
        2688.0
        >>> parse_split_price([])
    """
    if segments is None:
        return None
    if isinstance(segments, (str, int, float)):
        return parse_price(segments, unit_multiplier_hint=unit_multiplier_hint)

    try:
        parts = [str(x).strip() for x in segments if str(x).strip()]
    except TypeError:
        return None
    if not parts:
        return None

    if not _SEG_FIRST_RE.match(parts[0]):
        return None

    accepted = [parts[0]]
    for part in parts[1:_MAX_SEGMENTS]:
        if _SEG_CONT_RE.match(part):
            accepted.append(part)
        else:
            break

    return parse_price("".join(accepted), unit_multiplier_hint=unit_multiplier_hint)


# ----------------------------------------------------------------------
# 链接
# ----------------------------------------------------------------------

# 闲鱼/淘宝系在 App 端用私有协议，落到网页上要换回 https
_SCHEME_REWRITES: tuple[tuple[str, str], ...] = (
    ("fleamarket://", "https://www.goofish.com/"),
    ("taobao://", "https://www.taobao.com/"),
)

# 跟踪参数：不影响商品身份，去掉后同一商品只有一种写法（去重键才稳定）
_TRACKING_PARAMS = re.compile(
    r"^(utm_[a-z]+|spm|scm|from|share_\w+|source|refer|src|ttid|tk|_t|timestamp"
    r"|referpageargs|gulsource|search_from_page|original_q|bizfrom)$",
    re.I,
)

# 商品页**只保留身份参数** —— 闲鱼的商品页链接会跟一长串检索来源参数
# （referPageArgs / gulSource / …），全留着会让"同一个商品"有多个不同键，
# 去重就会失效。identity 参数才是商品身份。
_IDENTITY_ONLY = (
    ("goofish.com", "/item", ("id",)),
)


def normalize_url(url: object) -> str:
    """把商品链接归一到「可点开、且同一商品唯一」的形态。

    做三件事：
      1. 私有协议换 https（闲鱼 `fleamarket://` 存进库是点不开的）
      2. 补协议相对链接（`//x.com/a` → `https://x.com/a`）
      3. 去掉跟踪参数、保留片段之外的其余部分

    ⚠️ 只去**参数**，不碰路径与 id —— id 才是商品身份。
    """
    if not url:
        return ""
    s = str(url).strip()
    if not s:
        return ""
    for old, new in _SCHEME_REWRITES:
        if s.startswith(old):
            s = new + s[len(old):].lstrip("/")
            break
    if s.startswith("//"):
        s = "https:" + s
    if "://" not in s:
        # 不是 URL 就返回空串 —— **不要原样返回**。
        # 实测（2026-10-02）闲鱼响应里部分 targetUrl 是加密的不透明串
        # （如 `z9xpXnwzz6eu3JHQ…=`），原样返回会被当成"链接"存进库，
        # 既点不开、也会污染去重键。
        return ""

    s = s.split("#", 1)[0]
    if "?" not in s:
        return s

    head, _, query = s.partition("?")
    kept = [
        pair for pair in query.split("&")
        if pair and not _TRACKING_PARAMS.match(pair.split("=", 1)[0])
    ]

    # 商品页只留身份参数（见 _IDENTITY_ONLY 的说明）
    from urllib.parse import urlsplit

    parts = urlsplit(head)
    for host_suffix, path_prefix, names in _IDENTITY_ONLY:
        if parts.netloc.endswith(host_suffix) and path_prefix in parts.path:
            identity = [p for p in kept if p.split("=", 1)[0] in names]
            if identity:
                kept = identity
            break

    return head + ("?" + "&".join(kept) if kept else "")


def link_key(url: object) -> str:
    """商品链接的**去重键** —— 同一商品的稳定标识。

    闲鱼/拼多多的商品页 id 在 query 里（`?id=123` / `?goods_id=123`），
    所以保留归一后的完整链接即可（跟踪参数已被去掉）。

    返回空串表示"这条链接不足以做身份判定"，调用方应回退到别的键。
    """
    s = normalize_url(url)
    if not s or "://" not in s:
        return ""
    # 搜索页 / 列表页不是商品身份，不能拿来当键
    lowered = s.lower()
    if any(tok in lowered for tok in ("/search", "keyword=", "search_key=", "q=")):
        return ""
    return s
