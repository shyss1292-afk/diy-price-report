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
        self._cache: dict[str, int | None] = {}

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

        hit: int | None = None
        for _keyword, product_id, pattern in rules:
            if pattern.search(norm):
                hit = product_id
                break

        self._cache[cache_key] = hit
        return hit

    @property
    def rule_count(self) -> int:
        return len(self._all)
