"""真实数据覆盖缺口统计。

回答「还有多少型号没有真实数据 / 没有今天的新数据」，并按板块+品类拆分。

用法：
    python scripts/coverage.py            # 概要 + 按品类 + 缺今日明细
    python scripts/coverage.py --brief    # 只输出概要，不列型号

⚠️ 与 `app/services/healthcheck.check_coverage()` 的分工（**两套口径**）
--------------------------------------------------------------------
| | 本脚本 | healthcheck.check_coverage |
|---|---|---|
| 范围 | **全品类（351 个型号）** | 只 gpu + cpu（130 个，= 采集范围） |
| 口径 | 累计有过 / 今天有没有 | 从未采到 / 超 N 天没采到 |
| 用途 | **人工查看**缺口明细 | **自动告警**（每天一次，进轮次收尾） |

两者的数字**本来就不该相等**（351 vs 130）。要对比先看范围是否一致。
自动告警那条见 `python -m app.cli health --check`。
"""
import argparse
import sqlite3
import sys
from pathlib import Path

# 项目根目录：从本文件位置推导，**不要硬编码作者本机路径** ——
# 公开仓库里别人 clone 下来会直接 ImportError。
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from app.seed_data import CATEGORIES, CATEGORY_ORDER, category_group  # noqa: E402

con = sqlite3.connect(str(_ROOT / "data" / "diyprice.db"))
con.execute("PRAGMA busy_timeout=10000")
TODAY = con.execute("SELECT date('now','localtime')").fetchone()[0]

REAL = """EXISTS (SELECT 1 FROM listings l JOIN platforms pf ON pf.id = l.platform_id
           WHERE l.product_id = p.id AND l.is_synthetic = 0 AND pf.is_active = 1)"""
TODAY_REAL = f"""EXISTS (SELECT 1 FROM listings l JOIN platforms pf ON pf.id = l.platform_id
           WHERE l.product_id = p.id AND l.is_synthetic = 0
             AND l.trade_date = '{TODAY}' AND pf.is_active = 1)"""
ANY_TODAY = f"""EXISTS (SELECT 1 FROM listings l WHERE l.product_id = p.id
             AND l.trade_date = '{TODAY}')"""

rows = con.execute(f"""
    SELECT p.category,
           COUNT(*) AS n,
           SUM(CASE WHEN {REAL} THEN 1 ELSE 0 END) AS real_n,
           SUM(CASE WHEN {TODAY_REAL} THEN 1 ELSE 0 END) AS today_n,
           SUM(CASE WHEN {ANY_TODAY} THEN 1 ELSE 0 END) AS any_today
    FROM products p GROUP BY p.category
""").fetchall()
by_cat = {r[0]: r for r in rows}

TOTAL = sum(r[1] for r in rows)
REAL_T = sum(r[2] for r in rows)
TODAY_T = sum(r[3] for r in rows)

print("=" * 72)
print(f"  型号总数                         {TOTAL}")
print(f"  累计有过真实数据的型号           {REAL_T}    缺 {TOTAL - REAL_T}")
print(f"  今天({TODAY})有真实数据的型号     {TODAY_T}    缺 {TOTAL - TODAY_T}")
print("=" * 72)
print()
print("【按板块 / 品类】")
print(f"  {'板块':<8}{'品类':<10}{'型号':>5}{'有真实':>7}{'有今日':>7}{'缺今日':>7}")
for gcode, glabel in (("core", "核心配件"), ("other", "其他硬件")):
    sub = [c for c in CATEGORY_ORDER if category_group(c) == gcode]
    gsum = [0, 0, 0]
    first = True
    for c in sub:
        if c not in by_cat:
            continue
        _, n, r, t, _a = by_cat[c]
        print(f"  {glabel if first else '':<8}{CATEGORIES[c]:<10}{n:>5}{r or 0:>7}{t or 0:>7}{(n - (t or 0)):>7}")
        first = False
        gsum[0] += n
        gsum[1] += r or 0
        gsum[2] += t or 0
    print(f"  {'':<8}{'小计':<10}{gsum[0]:>5}{gsum[1]:>7}{gsum[2]:>7}{gsum[0] - gsum[2]:>7}")
print()

print("【今天各平台实际采到的型号数】")
for name, n, models in con.execute(f"""
    SELECT pf.name, COUNT(*), COUNT(DISTINCT l.product_id)
    FROM listings l JOIN platforms pf ON pf.id = l.platform_id
    WHERE l.is_synthetic = 0 AND l.trade_date = '{TODAY}' AND pf.is_active = 1
    GROUP BY pf.id ORDER BY COUNT(*) DESC
"""):
    print(f"  {name:<10} {n:>4} 条明细 / {models:>3} 个型号")
print()

args = argparse.ArgumentParser()
args.add_argument("--brief", action="store_true", help="只输出概要")
opts = args.parse_args()

missing_today = con.execute(f"""
    SELECT p.category, p.brand, p.model, p.spec
    FROM products p WHERE NOT ({TODAY_REAL})
    ORDER BY p.category, p.model
""").fetchall()
if opts.brief:
    raise SystemExit(0)
print(f"【缺今日数据的型号：{len(missing_today)} 个】")
cur = None
for cat, brand, model, spec in missing_today:
    if cat != cur:
        cur = cat
        print(f"\n  ── {CATEGORIES.get(cat, cat)} ──")
    print(f"     {brand:<10} {model:<26} {spec or ''}")
