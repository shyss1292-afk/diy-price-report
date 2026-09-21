"""归一化诊断工具：检查型号匹配率，列出未匹配的原始标题。

接入新数据源后必跑 —— 匹配率低说明型号库覆盖不足，
未匹配清单则直接告诉你该往 seed_data._EXTRA_ALIASES 补什么。

用法：
    python -m scripts.diagnose                     # 全部源，抽样今天
    python -m scripts.diagnose --days 3            # 抽样最近 3 天
    python -m scripts.diagnose --sources jd      # 只看某个源
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date, timedelta

from app.db import session_scope
from app.models import Platform, Product
from app.services.normalize import ModelMatcher, normalize_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="型号归一化匹配诊断")
    parser.add_argument("--days", type=int, default=1, help="抽样天数")
    parser.add_argument("--sources", type=str, default=None, help="逗号分隔的采集源 code，默认全部")
    parser.add_argument("--top", type=int, default=25, help="展示多少条未匹配标题")
    args = parser.parse_args(argv)

    from app.collectors import get_collectors

    codes = args.sources.split(",") if args.sources else None
    collectors = get_collectors(codes)
    if not collectors:
        print(f"没有可用的采集源（指定：{codes}）")
        return 1

    with session_scope() as session:
        products = list(session.query(Product).all())
        platforms = list(session.query(Platform).order_by(Platform.sort_order).all())
        matcher = ModelMatcher(products)
        print(
            f"型号 {len(products)} 个 / 平台 {len(platforms)} 个 / "
            f"匹配规则 {matcher.rule_count} 条 / 采集源 {[c.code for c in collectors]}\n"
        )

        today = date.today()
        # 每个源单独统计，避免混淆（Mock 是标准标题，真实源是平台标题）
        stats: dict[str, dict] = {}
        misses: Counter[str] = Counter()

        for collector in collectors:
            bucket = stats.setdefault(collector.code, {"total": 0, "matched": 0})
            for offset in range(args.days):
                day = today - timedelta(days=offset)
                for platform in platforms:
                    for quote in collector.collect(platform, products, day):
                        bucket["total"] += 1
                        hit = matcher.match(
                            quote.title_raw, category=quote.extra.get("category")
                        )
                        if hit is None:
                            misses[quote.title_raw] += 1
                        else:
                            bucket["matched"] += 1

    print("=== 分源匹配率 ===")
    grand_total = grand_matched = 0
    for code, bucket in stats.items():
        total, matched = bucket["total"], bucket["matched"]
        grand_total += total
        grand_matched += matched
        if total == 0:
            print(f"  {code:<8} 无数据（该源在抽样日期内未产出）")
            continue
        rate = matched / total * 100
        print(f"  {code:<8} {matched}/{total} = {rate:.1f}%")

    if grand_total:
        print(f"  {'合计':<8} {grand_matched}/{grand_total} = {grand_matched / grand_total * 100:.1f}%")

    if misses:
        print(f"\n=== 未匹配标题 TOP {args.top}（即型号库缺的型号）===")
        for title, cnt in misses.most_common(args.top):
            print(f"  ×{cnt:<4} {title[:58]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
