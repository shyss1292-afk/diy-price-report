"""诊断：为 95 条未匹配样本定位"该补哪个最小别名"。

对每条样本，取其目标型号，生成一组**候选变体**（去品牌前缀 / 去空格 /
G↔GB / Ti 与 SUPER 写法），看规范化后的标题里究竟命中了哪个变体。
命中的**最长**变体就是"最小充分别名" —— 只补它，不要补更宽的形式。

判定仍然用与 ModelMatcher 完全相同的边界规则
（`(?<![0-9A-Z])kw(?![0-9A-Z])`），否则结论会和线上不一致。
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

PROJ = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from app.services.normalize import normalize_text  # noqa: E402

_BRAND_PREFIXES = ("RTX ", "RX ", "GTX ", "ARC ", "GEFORCE RTX ", "GEFORCE GTX ")

# "数字 + 空格 + 短字母后缀" → 允许粘连。
# 真实标题里 "5500 XT" 与 "5500XT"、"5070 Ti" 与 "5070Ti" 都很常见，
# 而 normalize_text 把空格折叠掉、却不会把粘连的拆开 —— 于是这是两个 token。
# 这个模式只在这种位置生效，不会碰 "8G" 这类"数字+字母"（那是同一 token）。
_GLUE = re.compile(r"(\d)\s+([A-Za-z]{1,4})(?=\s|$)")


def variants(model: str) -> list[str]:
    """目标型号的候选变体（越靠前越具体）。"""
    out: list[str] = []

    def add(s: str) -> None:
        if s and s not in out:
            out.append(s)

    for src in (model, *[model[len(p):] for p in _BRAND_PREFIXES
                         if model.upper().startswith(p)]):
        glued = _GLUE.sub(r"\1\2", src)
        for base in {src, glued}:
            add(base)
            add(base.replace(" ", ""))                       # RTX2080
            add(re.sub(r"(\d)\s*G\b", r"\1GB", base))        # 8G → 8GB
            add(re.sub(r"(\d)\s*GB\b", r"\1G", base))        # 8GB → 8G
    return out


def hit(norm_title: str, keyword: str) -> bool:
    """与 ModelMatcher 完全相同的边界判定。"""
    kw = normalize_text(keyword)
    if len(kw) < 2:
        return False
    pat = re.compile(rf"(?<![0-9A-Z]){re.escape(kw)}(?![0-9A-Z])")
    return bool(pat.search(norm_title))


def main() -> int:
    samples = json.loads((PROJ / "data/unmatched_samples.json").read_text(encoding="utf-8"))
    print(f"样本 {len(samples)} 条\n")
    print("=" * 100)

    best: dict[str, list[tuple[str, str, float]]] = {}
    nothing: list[tuple[str, str, float]] = []

    for s in samples:
        norm = normalize_text(s["title"])
        hits = [v for v in variants(s["kw"]) if hit(norm, v)]
        if hits:
            pick = max(hits, key=len)          # 最长 = 最具体 = 最小充分别名
            best.setdefault(pick, []).append((s["kw"], s["title"], s["price"]))
        else:
            nothing.append((s["kw"], s["title"], s["price"]))

    total_hit = sum(len(v) for v in best.values())
    print(f"\n【A】可用**补别名**救回：{total_hit} 条，涉及 {len(best)} 个候选别名\n")
    for alias, items in sorted(best.items(), key=lambda kv: -len(kv[1])):
        kws = sorted({k for k, _, _ in items})
        print(f"  别名 {alias!r}  ← 可救回 {len(items)} 条  目标型号 {kws}")
        for kw, title, price in items[:2]:
            print(f"        ¥{price:<7.0f} {title[:78]}")
    print()
    print("=" * 100)
    print(f"\n【B】补别名也救不回（型号不符 / 配件 / 整机 —— 应保持未匹配）：{len(nothing)} 条\n")
    for kw, title, price in nothing:
        print(f"  [{kw}] ¥{price:<7.0f} {title[:80]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
