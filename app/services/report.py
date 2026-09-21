"""「显卡日报 / CPU日报」表格服务。

1:1 对照参考视频（@芝士华莱士 显卡日报 第1834期 · 显卡价格大汇总）
的四张表，逐列落到本地数据。

参考表格的字段（从 4K 视频帧逐字核对得到）
------------------------------------------------
表一「显卡价格每日更新」共 18 列：
  显卡 | 史低价 | 日低价 | 原价 | TSE跑分 | 今日最低平台 | 店铺 | 最低品牌 | 型号
  | 今日性价比 ‖ 某东自营(现价,涨跌幅) | 某宝(现价,涨跌幅)
  | 某多多(现价,涨跌幅) | 某Yin(现价,涨跌幅)

表二「CPU价格每日更新」共 14 列（与表一的差异：无原价 / 无最低品牌型号 / 无性价比；
跑分口径换成 R23；平台名带「散片」后缀）：
  CPU | 史低价 | 日低价 | 今日最低平台 | 店铺 | R23跑分 ‖ 各平台(现价,涨跌幅)

两个容易忽略的细节（已按此实现）：
  1. **涨跌幅是绝对金额（元），不是百分比** —— 视频里是 +165 / -101 / +354 这种。
  2. 型号文字**按芯片厂商着色**：NVIDIA 绿、AMD 红、Intel 蓝。

本实现的字段映射
----------------
型号 / 今日最低平台 / 店铺 / 现价 / 涨跌幅  —— listings 直接可得
史低价                                    —— 窗口内最低报价（只认真实数据）
日低价                                    —— 最新批次的最低报价
原价                                      —— products.base_price（**我方口径是自建基准
                                             参考价，不是厂商官方指导价**）
跑分                                      —— seed_bench.py（公开基准参考值）
最低品牌 / 型号                            —— services/brands.py 从商品标题提取
今日性价比                                 —— 跑分 ÷ 日低价（与参考表公式一致，单位"分/元"）

与参考表格的已知差异（不掩盖）
------------------------------
1. 平台用**真实名**（京东 / 拼多多 / 闲鱼），不用"某东自营"这类规避写法。
2. 参考表区分「散片/盒装」，本项目数据无此维度，故 CPU 表不加"散片"后缀。
3. 「原价」口径不同（见上）。
4. 参考表还有「内存硬盘表」「小黄鱼成交价表」，按需求暂不做（内存表不在范围内）。

涨跌幅的比较基准
----------------
用**采集批次**（listings.batch，小时粒度）而不是交易日：
真实源今天才有数据，若只按交易日比较则第一天永远是空；引入批次后，
同一天多次采集也能算出变化。比较时**只在同血缘批次之间进行**，
避免拿模拟历史给真实数据编出涨跌。
"""
from __future__ import annotations

import statistics
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Listing, Platform, Product
from ..seed_bench import bench_label, bench_score, bench_tooltip
from ..seed_data import CATEGORIES
from .brands import clean_title, guess_brand, short_model

# 本页只覆盖显卡与 CPU
REPORT_CATEGORIES = ("gpu", "cpu")

# 本页展示的平台（顺序即列顺序）
FOCUS_PLATFORMS = ("jd", "pdd", "xianyu")

PLATFORM_LABELS = {
    "jd": "京东",
    "pdd": "拼多多",
    "xianyu": "闲鱼",
}

BASIS_KEYS = {"all": "全市场（含二手）", "new": "仅全新", "used": "仅二手"}

# 史低价至少需要这么多天的**真实**数据才有参考意义
MIN_DAYS_FOR_HIST_LOW = 3
# 往回找多少个采集批次用于计算涨跌幅（分钟粒度下一天可能有很多批）
BATCH_LOOKBACK = 400

# 列定义 —— 严格对照参考表格。
#
# 显卡表（参考为 18 列：10 固定 + 4 平台 × 2）比 CPU 表多出
# 「原价 / 最低品牌 / 型号 / 今日性价比」四列；
# CPU 表（参考为 14 列：6 固定 + 4 平台 × 2）则没有这四列 ——
# 少了「性价比」是合理的：R23 多核分的量级比 TSE 大一个数量级，
# 两者放在同一张表里做成同一套"分/元"没有可比性。
_COLUMN_SPECS: dict[str, list[dict]] = {
    "gpu": [
        {"key": "model", "label": "显卡", "align": "left", "width": 150},
        {"key": "hist_low", "label": "史低价", "align": "right", "tip": "统计窗口内最低的真实报价"},
        {"key": "day_low", "label": "日低价", "align": "right", "tip": "最新批次的最低报价"},
        {"key": "base_price", "label": "原价", "align": "right",
         "tip": "本系统自建的基准参考价，非厂商官方指导价"},
        {"key": "bench", "label": "TSE跑分", "align": "right", "tip": "3DMark Time Spy Extreme 参考值"},
        {"key": "cheapest_platform", "label": "今日最低平台", "align": "left"},
        {"key": "cheapest_shop", "label": "店铺", "align": "left"},
        {"key": "cheapest_brand", "label": "最低品牌", "align": "left"},
        {"key": "cheapest_model", "label": "型号", "align": "left", "width": 200},
        {"key": "value_index", "label": "今日性价比", "align": "right",
         "tip": "性价比 = 跑分 ÷ 日低价，单位「分/元」，越高越划算"},
    ],
    "cpu": [
        {"key": "model", "label": "CPU", "align": "left", "width": 150},
        {"key": "hist_low", "label": "史低价", "align": "right", "tip": "统计窗口内最低的真实报价"},
        {"key": "day_low", "label": "日低价", "align": "right", "tip": "最新批次的最低报价"},
        {"key": "cheapest_platform", "label": "今日最低平台", "align": "left"},
        {"key": "cheapest_shop", "label": "店铺", "align": "left"},
        {"key": "bench", "label": "R23跑分", "align": "right", "tip": "Cinebench R23 多核参考值"},
    ],
}

# 型号文字按芯片厂商着色（与参考表格一致：NVIDIA 绿 / AMD 红 / Intel 蓝）
VENDOR_COLORS = {"NVIDIA": "#3a9b3a", "AMD": "#d93025", "Intel": "#2f6fed"}


def _r(value, digits: int = 2):
    if value is None:
        return None
    return round(float(value), digits)


def _platform_filter(basis: str):
    if basis == "new":
        return Platform.kind == "new"
    if basis == "used":
        return Platform.kind == "used"
    return None


def _pick_representative(rows: list[dict], trim_ratio: float = 0.6) -> dict | None:
    """从同一批次、同一平台的报价里挑一条"可信的最低价"。

    为什么要做这件事
    ----------------
    电商平台普遍存在**低价引流 SKU**：标题写着正常商品，价格却远低于市场
    （实测拼多多 RTX 5070 标 ¥2688，而同日正常价 ¥6999）。直接取 min 的后果：
      · 「日低价」被拉低，看着像捡漏；
      · 「涨跌幅」出现几千元的假波动（实测 +4311 元）。
    做法：以报价中位数为基准，把低于中位数 60% 的视为引流剔除，再取最低。
    报价少于 4 条时不启用 —— 样本太少，中位数本身不可靠，误删真便宜更糟。
    """
    if not rows:
        return None
    ordered = sorted(rows, key=lambda r: r["price"])
    if len(ordered) < 4:
        return ordered[0]
    med = statistics.median(r["price"] for r in ordered)
    kept = [r for r in ordered if r["price"] >= med * trim_ratio]
    return (kept or ordered)[0]


def build_daily_report(
    session: Session,
    category: str = "gpu",
    days: int = 180,
    basis: str = "all",
    platforms: tuple[str, ...] | None = None,
    real_only: bool = True,
    source_filter=None,
    **kwargs,
) -> dict:
    """生成某个品类的日报表。

    platforms 为要展示的平台 code 序列；默认只展示重点三平台。
    real_only 控制是否把模拟数据算进来（默认 False 时也**只把模拟平台
    作为参照列展示**，绝不用它参与史低价与涨跌）。
    """
    if category not in REPORT_CATEGORIES:
        return _empty_report(category, basis)

    wanted = list(platforms or FOCUS_PLATFORMS)
    # source_filter 兼容旧参数名（real_only 的别名）
    if source_filter is not None:
        real_only = bool(source_filter)

    from ..collectors.mock_source import REAL_PLATFORMS

    plat_rows = [
        p
        for p in session.execute(
            select(Platform).where(Platform.is_active.is_(True)).order_by(Platform.sort_order)
        ).scalars()
    ]
    by_code = {p.code: p for p in plat_rows}
    platforms_sel = [by_code[c] for c in wanted if c in by_code]
    if basis != "all":
        want_kind = "new" if basis == "new" else "used"
        platforms_sel = [p for p in platforms_sel if p.kind == want_kind]
    if not platforms_sel:
        return _empty_report(category, basis)

    plat_ids = [p.id for p in platforms_sel]
    plat_by_id = {p.id: p for p in platforms_sel}

    products = list(
        session.execute(
            select(Product).where(Product.category == category, Product.is_active.is_(True))
        ).scalars()
    )
    if not products:
        return _empty_report(category, basis)
    # 与参考表格一致：按性能/价位从高到低排列
    products.sort(key=lambda p: p.base_price, reverse=True)
    pids = [p.id for p in products]

    latest_date = session.execute(
        select(func.max(Listing.trade_date)).where(
            Listing.product_id.in_(pids), Listing.platform_id.in_(plat_ids)
        )
    ).scalar()
    if latest_date is None:
        return _empty_report(category, basis)

    coverage = _coverage(session, pids, plat_ids)

    # ---- 批次轴：最近若干个采集批次（batch 形如 20260916T1841，字典序即时间序）
    #
    # ⚠️ 关键：jd / pdd / xianyu 这三个平台在接入真实适配器**之前被 mock 写过**，
    # 那些历史行虽然平台名相同，价格却是程序生成的。若不按血缘过滤，表格里
    # 就会出现"京东 ¥17998"这种看着像真、其实是编的价格。默认只取真实采集。
    def _lineage(stmt):
        return stmt.where(Listing.is_synthetic.is_(False)) if real_only else stmt

    batches = [
        b
        for (b,) in session.execute(
            _lineage(
                select(Listing.batch)
                .where(
                    Listing.product_id.in_(pids),
                    Listing.platform_id.in_(plat_ids),
                    Listing.batch != "",
                )
                .distinct()
            )
            .order_by(Listing.batch.desc())
            .limit(BATCH_LOOKBACK)
        ).all()
    ]

    # 批次 -> 交易日，用于把「涨跌幅」锚定到上一交易日（与参考表格语义一致）
    batch_dates: dict[str, str] = {
        b: d.isoformat()
        for b, d in session.execute(
            _lineage(
                select(Listing.batch, func.min(Listing.trade_date))
                .where(Listing.batch.in_(batches))
                .group_by(Listing.batch)
            )
        ).all()
    }
    cur_date = batch_dates.get(batches[0]) if batches else None

    # ---- 分组概览：每个 (型号, 平台, 血缘, 批次) 有几条报价、最低多少。
    # 这是轻量查询，只用来确定"每个分组有哪些批次"；真正的取价在下一步用
    # 原始明细做 —— 因为需要剔除低价引流 SKU（见 _pick_representative）。
    group_index: dict[tuple[int, int, bool], list[tuple[str, float, int]]] = {}
    for pid, plat_id, batch, synth, lo, cnt in session.execute(
        _lineage(
            select(
                Listing.product_id,
                Listing.platform_id,
                Listing.batch,
                Listing.is_synthetic,
                func.min(Listing.price),
                func.count(),
            ).where(
                Listing.product_id.in_(pids),
                Listing.platform_id.in_(plat_ids),
                Listing.batch.in_(batches),
            )
        ).group_by(Listing.product_id, Listing.platform_id, Listing.batch, Listing.is_synthetic)
    ).all():
        group_index.setdefault((pid, plat_id, bool(synth)), []).append((batch, lo, cnt))

    # 每个分组只关心最近 2 个批次：一个用于显示，一个用于比涨跌幅。
    # 只拉这两批的明细，避免把整段回看窗口的原始报价都读进内存。
    needed_batches: set[str] = set()
    for items in group_index.values():
        items.sort(key=lambda x: x[0], reverse=True)
        for b, _, _ in items[:2]:
            needed_batches.add(b)

    raw_by_group: dict[tuple[int, int, str, bool], list[dict]] = {}
    if needed_batches:
        for pid, plat_id, batch, price, seller, title, url, cond, synth in session.execute(
            _lineage(
                select(
                    Listing.product_id,
                    Listing.platform_id,
                    Listing.batch,
                    Listing.price,
                    Listing.seller,
                    Listing.title_raw,
                    Listing.url,
                    Listing.condition,
                    Listing.is_synthetic,
                ).where(
                    Listing.product_id.in_(pids),
                    Listing.platform_id.in_(plat_ids),
                    Listing.batch.in_(needed_batches),
                )
            )
        ).all():
            raw_by_group.setdefault((pid, plat_id, batch, bool(synth)), []).append(
                {
                    "price": price,
                    "seller": seller or "",
                    "title": title or "",
                    "url": url or "",
                    "condition": cond or "",
                    "synthetic": bool(synth),
                }
            )

    # ---- 稳健取价：每个 (型号, 平台, 血缘) 得到 {批次: 代表报价}
    price_by_batch: dict[tuple[int, int, bool], dict[str, dict]] = {}
    for (pid, plat_id, batch, synth), rows in raw_by_group.items():
        best = _pick_representative(rows)
        if best is not None:
            price_by_batch.setdefault((pid, plat_id, synth), {})[batch] = best

    batch_rank = {b: i for i, b in enumerate(batches)}   # 越小越新
    newest_batch = batches[0] if batches else ""

    # ---- 史低价：只认真实数据（模拟数据是程序生成的，当"历史最低价"没有意义）
    hist_low = {
        pid: lo
        for pid, lo in session.execute(
            select(Listing.product_id, func.min(Listing.price))
            .where(
                Listing.trade_date >= latest_date - timedelta(days=days),
                Listing.product_id.in_(pids),
                Listing.platform_id.in_(plat_ids),
                Listing.is_synthetic.is_(False),
            )
            .group_by(Listing.product_id)
        ).all()
    }

    bench_field = bench_label(category)
    hist_ok = coverage["real_days"] >= MIN_DAYS_FOR_HIST_LOW

    # ---- 组装
    rows: list[dict] = []
    for product in products:
        cells: list[dict] = []
        candidates: list[tuple[float, int, dict]] = []

        for plat in platforms_sel:
            real_map = price_by_batch.get((product.id, plat.id, False), {})
            syn_map = price_by_batch.get((product.id, plat.id, True), {})
            # 同一平台若两种血缘都有，优先真实
            series = real_map or syn_map

            price = prev = None
            prev_batch = None
            det: dict = {}
            if series:
                ordered = sorted(series.items(), reverse=True)   # 新 -> 旧
                cur_batch, det = ordered[0]
                price = det["price"]
                # 比较基准优先取**上一交易日**的批次（参考表格的涨跌幅就是日间变化）；
                # 若还没有跨交易日的数据，退化为上一个批次（日内变化）。
                for b, d in ordered[1:]:
                    if cur_date and batch_dates.get(b) != cur_date:
                        prev, prev_batch = d["price"], b
                        break
                if prev is None and len(ordered) > 1:
                    prev, prev_batch = ordered[1][1]["price"], ordered[1][0]

            # 涨跌幅 = 绝对金额（元），与参考表格一致
            change = None if (price is None or prev is None) else price - prev

            cells.append(
                {
                    "code": plat.code,
                    "name": PLATFORM_LABELS.get(plat.code, plat.name),
                    "kind": plat.kind,
                    "is_real": plat.code in REAL_PLATFORMS,
                    "price": _r(price),
                    "change": _r(change),
                    "change_basis": (
                        "日间" if (change is not None and batch_dates.get(prev_batch) != cur_date)
                        else ("批次" if change is not None else None)
                    ),
                    "has_data": price is not None,
                    "seller": det.get("seller", ""),
                    "url": det.get("url", ""),
                    "synthetic": det.get("synthetic", None),
                }
            )
            if price is not None:
                candidates.append((price, plat.id, det))

        if not candidates:
            continue

        # 真实平台优先，其次比价
        def _rank(item):
            det = item[2]
            return (bool(det.get("synthetic", False)), item[0])

        candidates.sort(key=_rank)
        day_low, best_plat_id, best_det = candidates[0]
        best_plat = plat_by_id.get(best_plat_id)

        bench = bench_score(product.model, product.category)
        value_index = _r(bench / day_low, 2) if (bench and day_low) else None
        hist = hist_low.get(product.id)
        title = best_det.get("title", "")

        rows.append(
            {
                "product_id": product.id,
                "model": product.model,
                "short_model": short_model(product.model, product.category),
                "brand": product.brand,
                "spec": product.spec,
                "base_price": _r(product.base_price),
                "vs_base_pct": _r((day_low - product.base_price) / product.base_price * 100.0)
                if product.base_price
                else None,
                "hist_low": _r(hist) if (hist is not None and hist_ok) else None,
                "hist_low_pct": _r((day_low - hist) / hist * 100.0)
                if (hist is not None and hist_ok and hist)
                else None,
                "day_low": _r(day_low),
                "day_low_is_real": not best_det.get("synthetic", False),
                "bench": bench,
                "value_index": value_index,
                "cheapest_platform": {
                    "code": best_plat.code,
                    "name": PLATFORM_LABELS.get(best_plat.code, best_plat.name),
                    "kind": best_plat.kind,
                    "is_real": best_plat.code in REAL_PLATFORMS,
                }
                if best_plat
                else None,
                "cheapest_shop": best_det.get("seller", ""),
                "cheapest_brand": guess_brand(title, product.category),
                "cheapest_model": clean_title(title),
                "cheapest_url": best_det.get("url", ""),
                "cheapest_condition": best_det.get("condition", ""),
                "has_real": any(c["is_real"] and c["has_data"] for c in cells),
                "platforms": cells,
            }
        )

    return {
        "category": category,
        "category_label": CATEGORIES.get(category, category),
        "date": latest_date.isoformat(),
        "newest_batch": newest_batch,
        "batch_count": len(batches),
        "days": days,
        "basis": basis,
        "basis_label": BASIS_KEYS.get(basis, basis),
        "real_only": real_only,
        "bench_field": bench_field,
        "bench_tooltip": bench_tooltip(category),
        "columns": _COLUMN_SPECS.get(category, _COLUMN_SPECS["gpu"]),
        "vendor_colors": VENDOR_COLORS,
        "coverage": coverage,
        "platforms": [
            {
                "code": p.code,
                "name": PLATFORM_LABELS.get(p.code, p.name),
                "kind": p.kind,
                "is_real": p.code in REAL_PLATFORMS,
                "color": p.color,
            }
            for p in platforms_sel
        ],
        "stats": _row_stats(rows),
        "rows": rows,
    }


def _coverage(session: Session, pids: list[int], plat_ids: list[int]) -> dict:
    base = select(Listing.trade_date).where(
        Listing.product_id.in_(pids), Listing.platform_id.in_(plat_ids)
    )
    all_days = {d for (d,) in session.execute(base.distinct()).all()}
    real_days = {
        d for (d,) in session.execute(base.where(Listing.is_synthetic.is_(False)).distinct()).all()
    }
    real_rows = int(
        session.execute(
            select(func.count()).select_from(Listing).where(
                Listing.product_id.in_(pids),
                Listing.platform_id.in_(plat_ids),
                Listing.is_synthetic.is_(False),
            )
        ).scalar()
        or 0
    )
    real_models = int(
        session.execute(
            select(func.count(func.distinct(Listing.product_id))).where(
                Listing.product_id.in_(pids),
                Listing.platform_id.in_(plat_ids),
                Listing.is_synthetic.is_(False),
            )
        ).scalar()
        or 0
    )
    batches = int(
        session.execute(
            select(func.count(func.distinct(Listing.batch))).where(
                Listing.product_id.in_(pids),
                Listing.platform_id.in_(plat_ids),
                Listing.is_synthetic.is_(False),
            )
        ).scalar()
        or 0
    )
    return {
        "days": len(all_days),
        "real_days": len(real_days),
        "real_rows": real_rows,
        "real_models": real_models,
        "real_batches": batches,
        "min_days_for_hist": MIN_DAYS_FOR_HIST_LOW,
        "hist_available": len(real_days) >= MIN_DAYS_FOR_HIST_LOW,
        "change_available": batches >= 2,
    }


def _row_stats(rows: list[dict]) -> dict:
    """只统计真实平台的涨跌；模拟数据的涨跌是程序生成的，不当作行情汇报。"""
    up = down = flat = 0
    biggest_up = biggest_down = None
    for row in rows:
        changes = []
        for c in row["platforms"]:
            if not c["is_real"] or c["change"] is None:
                continue
            changes.append(c["change"])
            if c["change"] > 0 and (biggest_up is None or c["change"] > biggest_up["change"]):
                biggest_up = {
                    "model": row["short_model"],
                    "platform": c["name"],
                    "change": _r(c["change"]),
                }
            if c["change"] < 0 and (biggest_down is None or c["change"] < biggest_down["change"]):
                biggest_down = {
                    "model": row["short_model"],
                    "platform": c["name"],
                    "change": _r(c["change"]),
                }
        if not changes:
            continue
        net = sum(changes)
        if net > 0:
            up += 1
        elif net < 0:
            down += 1
        else:
            flat += 1
    return {
        "count": len(rows),
        "real_count": sum(1 for r in rows if r["has_real"]),
        "up": up,
        "down": down,
        "flat": flat,
        "compared": up + down + flat,
        "biggest_up": biggest_up,
        "biggest_down": biggest_down,
    }


def _empty_report(category: str, basis: str) -> dict:
    return {
        "category": category,
        "category_label": CATEGORIES.get(category, category),
        "date": None,
        "newest_batch": "",
        "batch_count": 0,
        "days": 180,
        "basis": basis,
        "basis_label": BASIS_KEYS.get(basis, basis),
        "real_only": True,
        "bench_field": bench_label(category),
        "bench_tooltip": bench_tooltip(category),
        "columns": _COLUMN_SPECS.get(category, _COLUMN_SPECS["gpu"]),
        "vendor_colors": VENDOR_COLORS,
        "coverage": {"days": 0, "real_days": 0, "real_rows": 0, "real_models": 0,
                     "real_batches": 0, "min_days_for_hist": MIN_DAYS_FOR_HIST_LOW,
                     "hist_available": False, "change_available": False},
        "platforms": [],
        "stats": {"count": 0, "real_count": 0, "up": 0, "down": 0, "flat": 0,
                  "compared": 0, "biggest_up": None, "biggest_down": None},
        "rows": [],
    }


def report_categories() -> list[dict]:
    return [
        {"code": code, "label": CATEGORIES.get(code, code)}
        for code in REPORT_CATEGORIES
        if code in CATEGORIES
    ]


def report_platforms() -> list[dict]:
    """日报可选的平台清单。"""
    return [
        {"code": c, "label": PLATFORM_LABELS.get(c, c), "focus": True}
        for c in FOCUS_PLATFORMS
    ]
