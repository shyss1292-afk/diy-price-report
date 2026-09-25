"""日报分板块的**契约测试** —— 跑真实数据库，验证「隔离」不是嘴上说的。

为什么单独一个脚本而不是塞进 selftest：`scripts/selftest.py` 的定位是
**纯逻辑、不碰数据库**（见它的模块头）。而分板块最要命的契约恰恰是
"真实数据下也成立"：

  · 全新板块的平台集合 ⊆ {kind=new}，二手板块 ⊆ {kind=used}，且两者不相交
  · 每个板块的行只属于该板块的厂商（N/A/I 互不穿透）
  · 每个型号只出现在一个板块里（不丢行、不重复）
  · 卡片要透出的指标字段齐全（均价 / 样本数 / 日环比额幅 / 性价比异动 / 走势）

这些断言在合成数据上过了不代表在真实数据上过 —— 比如某个品牌的
`products.brand` 拼写和 `SUBCATEGORIES` 对不上，只有在真库上才暴露。

    python -m scripts.contract_report          # 退出码 0 = 契约成立
"""
from __future__ import annotations

import sys

from app.db import SessionLocal
from app.services import report as R

# 卡片必须透出的字段（规格明确要求）
REQUIRED_ROW_FIELDS = (
    "product_id", "model", "short_model", "brand", "brand_code",
    "day_low", "day_avg", "day_samples",
    "day_change", "day_change_pct",
    "value_index", "value_index_change", "avg_trend",
    "captured_date", "is_today", "stale_days",
)

_fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        _fails.append(name)


def main() -> int:
    session = SessionLocal()
    try:
        m = R.build_report_matrix(session, days=180)
        sections = m["sections"]
        print(f"日报板块化契约检查（date={m['date']} batch={m['newest_batch']} "
              f"板块 {len(sections)} 个）\n")

        # ---- 契约①：品相数据源隔离 ----
        print("① 全新 / 二手 数据源隔离")
        by_basis: dict[str, set[str]] = {}
        for sec in sections:
            kinds = {p["kind"] for p in sec["platforms"]}
            codes = {p["code"] for p in sec["platforms"]}
            by_basis.setdefault(sec["basis"], set()).update(codes)
            want = {"new"} if sec["basis"] == "new" else {"used"}
            check(f"「{sec['title']}」的平台全部是 {want} 类",
                  kinds <= want if sec["basis"] in ("new", "used") else True,
                  f"实际 kinds={kinds} codes={sorted(codes)}")

        new_codes, used_codes = by_basis.get("new", set()), by_basis.get("used", set())
        check("全新与二手平台集合不相交", not (new_codes & used_codes),
              f"new={sorted(new_codes)} used={sorted(used_codes)}")
        check("全新口径含京东/拼多多", {"jd", "pdd"} <= new_codes, f"{sorted(new_codes)}")
        check("二手口径含闲鱼", "xianyu" in used_codes, f"{sorted(used_codes)}")

        # 行内平台格也不能混：全新板块的行里不许出现 kind=used 的格子
        print("\n② 行内平台格与板块品相一致")
        bad_cells = []
        for sec in sections:
            if sec["empty"] or sec["basis"] not in ("new", "used"):
                continue
            for r in sec["rows"]:
                for c in r["platforms"]:
                    if c["kind"] != sec["basis"]:
                        bad_cells.append((sec["title"], r["model"], c["code"], c["kind"]))
        check("板块内每一行的平台格 kind 都与板块品相一致",
              not bad_cells, f"越界 {len(bad_cells)} 处：{bad_cells[:3]}")

        # ---- 契约②：厂商阵营互不穿透 ----
        #
        # ⚠️ 不变量是「同一型号在**同一品相内**只出现一次」，不是"全局只出现一次"。
        #    同一张卡同时出现在「全新在售」和「二手流转」是**设计要求**（品相分轨），
        #    把它当成重复计入是断言写错了（实测第一版就这么写歪过）。
        print("\n③ 厂商阵营互不穿透（N/A/I、IU/AU）")
        seen: dict[tuple[int, str], str] = {}
        dup: list[str] = []
        for sec in sections:
            for r in sec["rows"]:
                if r["brand"] != sec["brand"]:
                    dup.append(f"{sec['title']} 混入 {r['brand']}/{r['model']}")
                key = (r["product_id"], sec["basis"])
                if key in seen:
                    dup.append(
                        f"{r['model']} 在同一品相内被「{seen[key]}」与「{sec['title']}」重复计入"
                    )
                seen[key] = sec["title"]
        check("每个板块的行只属于该板块的厂商，且同一品相内不重复计入",
              not dup, f"{dup[:3]}")

        for cat in m["categories"]:
            ids = {s["brand"]: {r["product_id"] for r in s["rows"]} for s in cat["sections"]}
            names = sorted(ids)
            overlap = [
                f"{a}∩{b}"
                for i, a in enumerate(names)
                for b in names[i + 1:]
                if ids[a] & ids[b]
            ]
            check(f"[{cat['category_label']}] 各厂商型号集合两两不相交",
                  not overlap, f"交集：{overlap}")

        # ---- 契约③：卡片字段齐全 ----
        print("\n④ 卡片必须透出的字段")
        rows = [r for sec in sections if not sec["empty"] for r in sec["rows"]]
        missing = sorted({k for r in rows for k in REQUIRED_ROW_FIELDS if k not in r})
        check(f"{len(rows)} 行全部带齐 {len(REQUIRED_ROW_FIELDS)} 个必需字段",
              not missing, f"缺：{missing}")

        with_avg = sum(1 for r in rows if r["day_avg"] is not None)
        with_samples = sum(1 for r in rows if r["day_samples"])
        with_trend = sum(1 for r in rows if (r.get("avg_trend") or {}).get("avg"))
        check("每行都有均价", with_avg == len(rows), f"{with_avg}/{len(rows)}")
        check("每行都有有效样本数", with_samples == len(rows), f"{with_samples}/{len(rows)}")
        print(f"    （均价走势：{with_trend}/{len(rows)} 行有 ≥1 个历史点 —— "
              f"数据只有 09-16 起，新入表型号没有历史属正常）")

        # ---- 契约④：涨跌榜/观察的条数自洽 ----
        print("\n⑤ 涨跌榜与重点观察")
        bad_mv = []
        for sec in sections:
            if sec["empty"]:
                continue
            for key in ("up", "down"):
                for mv in sec["movers"][key]:
                    if mv["brand"] != sec["brand"]:
                        bad_mv.append(f"{sec['title']} {key} {mv['model']}({mv['brand']})")
        check("涨跌榜条目全部来自本板块厂商", not bad_mv, f"{bad_mv[:3]}")
        check("观察条数不超过上限",
              all(len(s["watch"]) <= m["watch_limit"] for s in sections),
              f"limit={m['watch_limit']}")

        print()
        if _fails:
            print(f"结论：❌ {len(_fails)} 条契约不成立 —— {_fails}")
            return 1
        print("结论：✅ 全部分板块契约成立")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
