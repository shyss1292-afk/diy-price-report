"""未匹配样本分析（探索用）。

思路：pipeline 在 `pid is None` 时**只计数就丢弃标题**，所以 95 条未匹配
样本无法直接查库。但原始报价在匹配**之前**已由 `task_queue.append_quotes`
落盘到 `data/queue_results/<source>-<date>.jsonl`。

记录没有时间戳，不过每个型号每轮的报价是**追加**写入的 ——
因此"每型号取最后 N 条"（N = 该轮日志里该型号的条数）可以重建那一轮。
重建后跑一遍 match + clean，若得到同样的 127/95 就说明提取准确。
"""
from __future__ import annotations

import collections
import io
import json
import pathlib
import re
import sys

PROJ = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from app.db import session_scope  # noqa: E402
from app.models import Product  # noqa: E402
from app.services.clean import check as clean_check  # noqa: E402
from app.services.normalize import ModelMatcher  # noqa: E402
from sqlalchemy import select  # noqa: E402


def round_tasks(log_path: pathlib.Path, stamp: str = "16:00:05") -> list[tuple[str, int]]:
    """从日志里取出某一轮闲鱼的 (型号, 报价条数)。"""
    lines = io.open(log_path, encoding="utf-8", errors="replace").read().splitlines()
    start = next(i for i, l in enumerate(lines) if stamp in l and "轮次开始" in l)
    pat = re.compile(r"闲鱼 \[\d+/\d+\] (.+?) → (\d+) 条")
    out: list[tuple[str, int]] = []
    for line in lines[start:]:
        m = pat.search(line)
        if m:
            out.append((m.group(1).strip(), int(m.group(2))))
    return out


def rebuild_round(records: list[dict], tasks: list[tuple[str, int]]) -> list[dict]:
    """每型号取最后 N 条，重建该轮的原始报价（返回 quote 本体）。"""
    by_kw: dict[str, list[dict]] = collections.defaultdict(list)
    for rec in records:
        by_kw[rec["quote"]["extra"].get("keyword")].append(rec["quote"])
    picked: list[dict] = []
    for model, count in tasks:
        picked.extend(by_kw.get(model, [])[-count:])
    return picked


def dedupe(quotes: list[dict]) -> list[dict]:
    seen, out = set(), []
    for q in quotes:
        key = (q["platform_code"], q["title_raw"], q["price"])
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
    return out


def main() -> int:
    # 可选：指定轮次起始时间戳（日志里的 "HH:MM:SS ── 轮次开始 ──"）。
    # 不指定就用最近一次含该标记的轮次。
    stamp = sys.argv[1] if len(sys.argv) > 1 else "16:00:05"

    records = [
        json.loads(l)
        for l in io.open(
            PROJ / "data/queue_results/xianyu-2026-09-21.jsonl", encoding="utf-8"
        )
    ]
    tasks = round_tasks(PROJ / "data/collect.log", stamp)
    raw = rebuild_round(records, tasks)
    print(f"重建：{len(tasks)} 个型号 → {len(raw)} 条原始报价", end="")
    uniq = dedupe(raw)
    print(f"，去重后 {len(uniq)} 条（日志记录本轮 393 条）")

    with session_scope() as session:
        products = list(
            session.execute(select(Product).where(Product.is_active.is_(True))).scalars()
        )
    matcher = ModelMatcher(products)
    base_by_id = {p.id: float(p.base_price or 0.0) for p in products}

    matched, unmatched, filtered = [], [], []
    for q in uniq:
        pid = matcher.match(q["title_raw"], category=q["extra"].get("category"))
        if pid is None:
            unmatched.append(q)
            continue
        keep, reason = clean_check(q["price"], base_by_id.get(pid), q["title_raw"])
        (matched if keep else filtered).append((q, reason))

    print(f"\n复现结果：匹配 {len(matched)} / 未匹配 {len(unmatched)} / 被过滤 {len(filtered)}")
    print("日志实际：匹配 127 / 未匹配 95 / 被过滤 171")
    ok = len(matched) == 127 and len(unmatched) == 95
    print(f"{'✅ 复现成功，样本可信' if ok else '⚠️ 未能精确复现（样本仍可用于定性分析）'}")

    out = PROJ / "data/unmatched_samples.json"
    out.write_text(
        json.dumps(
            [{"kw": q["extra"].get("keyword"), "cat": q["extra"].get("category"),
              "price": q["price"], "title": q["title_raw"]} for q in unmatched],
            ensure_ascii=False, indent=1,
        ),
        encoding="utf-8",
    )
    print(f"未匹配样本已写出：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
