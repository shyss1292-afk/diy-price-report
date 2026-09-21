"""型号归一化：把各平台千奇百怪的标题匹配到标准型号。

真实平台标题写法差异极大（"RTX5070Ti" / "5070 Ti" / "RTX 5070TI 显卡"），
这里用「别名 + 长关键词优先 + 字母数字边界校验 + 品类隔离」四招兜住。

其中两条防线是接入 ZOL 实测后补上的：
  1. 所有关键词都要字母/数字边界 —— 否则 "9950X" 会命中 "9950X3D"；
  2. 支持按品类限定匹配范围 —— 否则机箱标题里的"支持5090显卡"
     会被当成显卡型号（ZOL 机箱页真实出现过）。

接入真实源后，只要把新发现的写法补进 seed_data._EXTRA_ALIASES 即可。
"""
from __future__ import annotations

import re

_SEP = re.compile(r"[^0-9A-Z\u4e00-\u9fff×]+")

# 标题里的"容量类数字"：8G / 8GB / 12 GB / O16G / OC16G …
#
# ⚠️ 左侧**只能断言"不是数字"**，不能断言"不是字母数字"。
#    电商标题里容量常常紧跟在字母后面：
#        "华硕DUAL GeForce RTX 5060 Ti O16G"   ← O16G = OC 16G
#        "索泰RTX5060Ti月白OC16G电竞"
#        "华硕ATS-RTX5060TI-O8G"
#    若断言 `(?<![0-9A-Za-z])`，这些**全部抽不到容量** —— 实测导致 57 条
#    本来能正确判容量的明细被误判成"容量歧义"而放弃入库。
#    只断言"不是数字"仍能挡住 "13080G" 这种从中间截出 "080" 的情况。
#
# ⚠️ 右侧用 (?![0-9A-Za-z]) 而不是 \b —— 中文是 Unicode 词字符，
#    `\b` 在 "8G显卡" 的 G 后面**不成立**，会导致整类漏抽。
_CAPACITY_IN_TEXT = re.compile(r"(?<![0-9])(\d{1,3})\s*GB?(?![0-9A-Za-z])", re.IGNORECASE)

# 型号里的容量：取**第一个**匹配作为该型号的主容量。
#
# 为什么是第一个而不是最后一个：像 "DDR5 32GB 16GB×2 6000 C30" 这种型号，
# 第一个 32 才是整条的容量，后面的 16 是单条容量。取最后一个会拿反。
_CAPACITY_IN_MODEL = re.compile(r"(?<![0-9])(\d{1,3})\s*GB?(?![0-9A-Za-z])", re.IGNORECASE)


def extract_capacities(text: str) -> set[int]:
    """从标题里抽出**所有**容量类数字。

    为什么要抽"全部"而不是"最像显卡那个"：标题里往往同时有内存容量
    （"16G DDR4 内存"）和显存容量（"RX 6500XT 4G 独显"），无法可靠区分。
    但消歧不需要区分 —— 只需要判断"某个候选型号的容量**是否在标题里出现过**"。
    多抽几个只会让判定更保守（出现歧义就放弃），不会写错数据。
    """
    return {int(m.group(1)) for m in _CAPACITY_IN_TEXT.finditer(text or "")}


def model_capacity(model: str) -> int | None:
    """型号的**主容量**（取第一个容量类数字），没有则 None。"""
    m = _CAPACITY_IN_MODEL.search(model or "")
    return int(m.group(1)) if m else None


def normalize_text(text: str) -> str:
    """统一大写、把任意分隔符折叠成单空格，便于做词边界匹配。"""
    if not text:
        return ""
    s = text.upper().replace("　", " ")
    s = _SEP.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


class ModelMatcher:
    """基于关键词的型号匹配器。"""

    def __init__(self, products: list) -> None:
        by_category: dict[str, list[tuple[str, int, re.Pattern]]] = {}

        for product in products:
            keys = {product.model}
            if product.aliases:
                keys.update(a.strip() for a in product.aliases.split(",") if a.strip())
            for key in keys:
                norm = normalize_text(key)
                if len(norm) < 2:
                    continue  # 单字符关键词噪声太大，直接丢弃
                pattern = re.compile(rf"(?<![0-9A-Z]){re.escape(norm)}(?![0-9A-Z])")
                by_category.setdefault(product.category, []).append(
                    (norm, product.id, pattern)
                )

        # 长关键词优先，避免 "5070" 抢走 "RTX 5070 Ti" 的标题
        for rules in by_category.values():
            rules.sort(key=lambda r: (-len(r[0]), r[0]))

        self._by_category = by_category
        self._all: list[tuple[str, int, re.Pattern]] = sorted(
            (rule for rules in by_category.values() for rule in rules),
            key=lambda r: (-len(r[0]), r[0]),
        )
        # 型号 → 主容量，供平局消歧用（见 _resolve）
        self._capacity_by_pid: dict[int, int | None] = {
            p.id: model_capacity(p.model) for p in products
        }
        self._cache: dict[str, int | None] = {}

    def _resolve(self, norm_title: str, candidates: list[int]) -> int | None:
        """平局决断：容量一致就选它；容量判不出来就**放弃**。

        ⚠️ 底线：**绝不随机漂移。**
        标题通篇没写容量时（如"自用 3080 出"），10G 与 12G 两个候选都
        "说得通" —— 此时返回 None（放弃入库），而不是按关键词字母序随便挑
        一个、把错容量写进库里。错数据比缺数据更难发现，也更难清理。

        返回 None 的样本会落到 `unmatched`，这是**有意的**：宁可少一条，
        也不要在 3080 12G 的均价里混进 10G 的价格。
        """
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        caps = {self._capacity_by_pid.get(pid) for pid in candidates}
        if len(caps) == 1:
            # 候选容量完全一致 —— 歧义不在容量上（属于品牌/版本差异），
            # 按最长关键词定即可，不会写错容量，保持原有行为。
            return candidates[0]

        caps_in_title = extract_capacities(norm_title)
        if caps_in_title:
            matched = [
                pid for pid in candidates if self._capacity_by_pid.get(pid) in caps_in_title
            ]
            if len(matched) == 1:
                return matched[0]
        return None

    def match(self, title: str, category: str | None = None) -> int | None:
        """把标题匹配到型号 ID。

        Args:
            title: 平台原始标题
            category: 已知品类时传入，把匹配范围限制在该品类内，
                      避免跨品类误匹配（如机箱标题提到显卡型号）。
        """
        cache_key = f"{category or '*'}|{title[:140]}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        norm = normalize_text(title)
        rules = self._by_category.get(category, []) if category else self._all

        # 先找出**最长命中关键词**，再把"并列最长的所有候选"当作平局组。
        #
        # 为什么不能像以前那样命中即返回：像 "RTX4060 Ti" 这种"去容量"别名，
        # 8G 版和 16G 版**都有**，命中即返回等于按字典序随机挑一个 ——
        # 实测会把 "微星RTX4060 Ti魔龙X Trio 8G" 判成 16G 版。
        #
        # rules 已按 (-长度, 关键词) 排序，所以关键词长度一旦小于当前最长命中，
        # 后面的不可能再并列，可以直接停 —— 不必扫完整张表。
        best_len = -1
        tied: list[int] = []
        for keyword, product_id, pattern in rules:
            if best_len >= 0 and len(keyword) < best_len:
                break
            if pattern.search(norm):
                if len(keyword) > best_len:
                    best_len = len(keyword)
                    tied = [product_id]
                elif product_id not in tied:
                    tied.append(product_id)

        hit = self._resolve(norm, tied)
        self._cache[cache_key] = hit
        return hit

    @property
    def rule_count(self) -> int:
        return len(self._all)
