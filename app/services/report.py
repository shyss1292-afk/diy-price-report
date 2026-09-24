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
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Listing, Platform, Product
from ..seed_bench import bench_label, bench_score, bench_tooltip
from ..seed_data import CATEGORIES, subcategories_of
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


def _day_gap(cur: str | None, prev: str | None) -> int | None:
    """两个采集交易日之间差几天（用于给涨跌幅**如实标注**比较跨度）。

    为什么要它：原来的 `change_basis` 只有「日间 / 批次」两档，而「日间」的
    判据仅仅是"两个批次的交易日不同" —— 实测有 6 天跨度的比较也被标成「日间」，
    于是 RTX 5090 在拼多多上被算出一个 +19,783 元的"日涨"（其实是 09-16 → 09-22）。
    数字本身没错，标签错了；标签错了，涨跌榜就会把陈年价差顶到榜首。
    """
    if not cur or not prev:
        return None
    try:
        return (date.fromisoformat(cur) - date.fromisoformat(prev)).days
    except ValueError:
        return None


# 单批次变动超过这个比例就**标疑**（不是丢弃，是打标）。
#
# 为什么要它：`_pick_representative` 的低价引流剔除需要 ≥4 条样本才启用
# （样本太少时中位数本身不可靠）。实测 2 条的批次直接漏过，于是
# RTX 5090 D 32G 在京东被算成 44,899 → 15,499（-29,400，-65%）：
# 两条标题都写着「RTX 5090D V2 5080 5070TI 5060TI 魔鹰显卡」，是典型的
# 多型号堆砌标题被规则匹配到了 5090D，价格则是引流价。
# 显卡单日跌 50% 在真实市场基本不可能，所以这类一律打标给用户看，
# 并**排除出涨跌榜** —— 但不改数字本身（改数才是真的掩盖）。
SUSPECT_CHANGE_RATIO = 0.5


def change_basis_label(gap: int | None) -> str | None:
    """涨跌幅的比较基准标签：同批次 / 隔一天 / 跨 N 天。

    ⚠️ 三档而不是两档（2026-09-24 改）。原来只有「日间 / 批次」，而「日间」的
       判据仅仅是"两个批次的交易日不同" —— 实测有 6 天跨度的比较也被标成「日间」，
       于是 RTX 5090 在拼多多上被算出一个 +19,783 元的"日涨"
       （其实是 09-16 → 09-22）。数字本身没错，标签错了；
       标签错了，涨跌榜就会把陈年价差顶到榜首。
    """
    if gap is None:
        return None
    if gap == 0:
        return "批次"
    if gap == 1:
        return "日间"
    return f"跨{gap}天"


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
            cur_batch = None
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
            gap = _day_gap(batch_dates.get(cur_batch), batch_dates.get(prev_batch))

            cells.append(
                {
                    "code": plat.code,
                    "name": PLATFORM_LABELS.get(plat.code, plat.name),
                    "kind": plat.kind,
                    "is_real": plat.code in REAL_PLATFORMS,
                    "price": _r(price),
                    "change": _r(change),
                    # ⚠️ 三档而不是两档：同批次（日内）、隔一天（真日间）、跨多天。
                    #    「跨 N 天」这一档以前被错标成「日间」，会把陈年价差当今日异动。
                    "change_basis": (
                        None if change is None else (change_basis_label(gap) or "批次")
                    ),
                    "gap_days": gap,
                    "prev_price": _r(prev),
                    # 标疑而不改数：见 SUSPECT_CHANGE_RATIO 的说明
                    "suspect": bool(
                        change is not None
                        and prev
                        and abs(change) >= abs(prev) * SUSPECT_CHANGE_RATIO
                    ),
                    "has_data": price is not None,
                    "seller": det.get("seller", ""),
                    "url": det.get("url", ""),
                    "synthetic": det.get("synthetic", None),
                    # 供前端标「昨日 / N 天前」用（见 _row_freshness）
                    "batch": cur_batch,
                    "date": batch_dates.get(cur_batch) if cur_batch else None,
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

        # 该行**最新**的一个平台批次日期 —— 只要有一个平台今天是新采的，
        # 这一行就不该被标成「昨日」。字段名与 trend.build_snapshot 对齐，
        # 否则 common.js 的 freshBadge() 认不出来（见下方行内注释）。
        cap_dates = [c["date"] for c in cells if c.get("date")]
        captured = max(cap_dates) if cap_dates else None
        today_iso = date.today().isoformat()

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
                # ---- 数据新鲜度：前端据此标「今日」/「昨日」/「N 天前」----
                # ⚠️ 字段名必须与 trend.build_snapshot 完全一致（captured_date /
                #    is_today / stale_days），否则 common.js 的 freshBadge() 认不出来。
                #    取该行**最新**的一个平台批次 —— 只要有一个平台今天是新采的，
                #    这一行就不该被标成「昨日」。
                "captured_date": captured,
                "is_today": captured == today_iso,
                "stale_days": (
                    None
                    if captured is None
                    else (date.today() - date.fromisoformat(captured)).days
                ),
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


# ================================================================ 板块化矩阵
#
# 为什么要把日报拆成板块（2026-09-24）
# ----------------------------------
# 原来的日报是「一个品类一张大表」：N卡/A卡/I卡、全新/二手全部混排，排序只看参考价。
# 后果有两个，都是口径问题而不是排版问题：
#
#   1. 「涨跌榜」「跌幅最大」是在**混合池**里算的 —— A卡的涨幅会把 N卡的行情顶掉，
#      闲鱼二手的异动会盖过京东新品的价格。这种排名没有解释力。
#   2. 想看「N卡二手今天怎么样」，得自己在一张大表里滚着找。
#
# 所以按 **品类 × 厂商 × 品相** 切成独立板块，每块的统计 / 涨跌榜 / 重点观察
# **只在块内计算**，绝不跨块混排。

# 板块的品相维度与标题后缀。全新只覆盖 jd/pdd，二手只覆盖闲鱼（见 Platform.kind）
SECTION_BASIS: tuple[tuple[str, str], ...] = (
    ("new", "全新在售"),
    ("used", "二手流转"),
)
SECTION_BASIS_LABEL = dict(SECTION_BASIS)

# 板块内「今日重点观察」：最多挑几条
WATCH_LIMIT = 4
# 判「贴近史低」的容差（日低价 ≤ 史低价 × 该系数）
WATCH_NEAR_LOW_RATIO = 1.02


def section_title(brand_label: str, basis: str) -> str:
    """板块标题，如「N卡 · 全新在售」。"""
    return f"{brand_label} · {SECTION_BASIS_LABEL.get(basis, basis)}"


def section_movers(rows: list[dict], limit: int = 3) -> dict:
    """板块内的涨跌榜（Top Movers）。

    ⚠️ 只在传入的 `rows`（= 同一品牌、同一品相的子池）里排序。跨品牌混排会造出
       「N卡全新跌了、被 A卡二手的涨幅顶掉」这种没有意义的对比 —— 这正是本次
       重构要修的口径问题。

    ⚠️ 排序键取**单平台最大变动**，不把各平台的涨跌**求和**。
       求和看着像"净变动"，其实是量纲错误：京东 44000 的卡涨 10000、拼多多
       涨 9000，加起来 19000 既不是任何一个平台的真实变动，也会把双平台型号
       系统性顶到榜首。单平台口径与结论条的「涨幅最大/跌幅最大」同源。

    ⚠️ 只收**跨度 ≤ 1 天**的变动（同批次 / 隔一天）。跨度更大的（实测有 6 天的）
       是"陈年价差"，不是今日异动 —— 把它算进"今日涨跌榜"会让榜首全是历史欠账。
       被排除的条数记在 `stale_excluded` 里**如实报出**，不静默丢弃。

    ⚠️ 也排除**标疑**的变动（单批次变动 ≥ 50%，见 `SUSPECT_CHANGE_RATIO`）。
       这类多半是标题匹配错位或引流 SKU，数字留着给用户看，但不上榜。
       条数记在 `suspect_excluded`。

    模拟平台不参与（与 `_row_stats` 一致）—— 程序生成的价格不该进行情榜。
    """
    ups: list[dict] = []
    downs: list[dict] = []
    compared = 0
    stale_excluded = 0
    suspect_excluded = 0
    for row in rows:
        moves = []
        for c in row.get("platforms", []):
            if not c.get("is_real") or c.get("change") is None:
                continue
            gap = c.get("gap_days")
            if gap is not None and gap > 1:
                stale_excluded += 1
                continue
            if c.get("suspect"):
                suspect_excluded += 1
                continue
            moves.append((c["change"], c["name"], c.get("change_basis")))
        if not moves:
            continue
        compared += 1

        def _entry(change: float, platform: str, basis: str | None) -> dict:
            return {
                "product_id": row.get("product_id"),
                "model": row.get("short_model") or row.get("model"),
                "full_model": row.get("model"),
                "brand": row.get("brand"),
                "day_low": row.get("day_low"),
                "change": _r(change),
                "platform": platform,
                "change_basis": basis,
                "cheapest_platform": (row.get("cheapest_platform") or {}).get("name"),
                "captured_date": row.get("captured_date"),
                "is_today": row.get("is_today"),
                "stale_days": row.get("stale_days"),
            }

        best_up = max((m for m in moves if m[0] > 0), key=lambda m: m[0], default=None)
        best_down = min((m for m in moves if m[0] < 0), key=lambda m: m[0], default=None)
        if best_up:
            ups.append(_entry(*best_up))
        if best_down:
            downs.append(_entry(*best_down))

    ups.sort(key=lambda e: -e["change"])
    downs.sort(key=lambda e: e["change"])
    return {
        "up": ups[:limit],
        "down": downs[:limit],
        "compared": compared,
        "stale_excluded": stale_excluded,
        "suspect_excluded": suspect_excluded,
    }


def section_watch(rows: list[dict], limit: int = WATCH_LIMIT) -> list[dict]:
    """板块内的「今日重点观察」。

    规则（按优先级依次挑，按型号去重，最多 `limit` 条）—— **只在板块内**：

      1. `贴近史低` —— 日低价 ≤ 史低价 × 1.02（且史低价可用），**最多占 2 个名额**
      2. `跌幅最大` —— 板块内单平台跌幅最大的那个（< 0）
      3. `涨幅最大` —— 板块内单平台涨幅最大的那个（> 0）
      4. `性价比最高` —— value_index 最大（CPU 表无此列，自动跳过）

    每条都带 `reason`，前端直接透出 —— 不做「黑箱推荐」。

    ⚠️ 第 1 条要限名额：史低价本身是"窗口内最低"，一旦某型号触及它，
       规则 1 会把所有名额吃光（实测 N卡全新块 4 个名额全被"贴近史低"占满，
       涨跌两端一个都进不来）。所以封 2 个，保证后三条一定有机会。
    """
    movers = section_movers(rows, limit=1)
    picked: dict[int, dict] = {}

    def _add(row: dict, reason: str, detail: str) -> None:
        pid = row.get("product_id")
        if pid in picked:
            return
        picked[pid] = {
            "product_id": pid,
            "model": row.get("short_model") or row.get("model"),
            "full_model": row.get("model"),
            "brand": row.get("brand"),
            "reason": reason,
            "detail": detail,
            "day_low": row.get("day_low"),
            "hist_low": row.get("hist_low"),
            "value_index": row.get("value_index"),
            "cheapest_platform": (row.get("cheapest_platform") or {}).get("name"),
            "captured_date": row.get("captured_date"),
            "is_today": row.get("is_today"),
            "stale_days": row.get("stale_days"),
        }

    # 1) 贴近史低（限 2 个名额）
    near_low = 0
    for row in sorted(rows, key=lambda r: r.get("day_low") or 1e18):
        if near_low >= 2 or len(picked) >= limit:
            break
        low, hist = row.get("day_low"), row.get("hist_low")
        if low and hist and low <= hist * WATCH_NEAR_LOW_RATIO:
            _add(row, "贴近史低", f"日低 {low:,.0f} / 史低 {hist:,.0f}")
            near_low += 1

    # 2) / 3) 板块内涨跌两端各一
    for key, reason in (("down", "跌幅最大"), ("up", "涨幅最大")):
        for mv in movers.get(key, []):
            src = next((r for r in rows if r.get("product_id") == mv["product_id"]), None)
            if src is not None:
                _add(src, reason, f"{mv['change']:+,.0f} 元 @{mv['platform']}")
        if len(picked) >= limit:
            return list(picked.values())

    # 4) 性价比最高
    best = max(
        (r for r in rows if r.get("value_index")),
        key=lambda r: r["value_index"],
        default=None,
    )
    if best is not None:
        _add(best, "性价比最高", f"{best['value_index']:.2f} 分/元")
    return list(picked.values())[:limit]


def split_sections(category: str, basis: str, rows: list[dict], subcategories: list[dict]) -> list[dict]:
    """把一个 (品类, 品相) 的 rows 按**厂商**拆成独立板块。

    ⚠️ 这是「不跨品牌混排」的唯一实现点 —— 每个板块只拿自己品牌的行，
       统计 / 涨跌榜 / 重点观察都在这个子集上算。
       **纯函数、不碰数据库**，所以自检可以直接喂合成数据断言，不必起服务。

    认不出的品牌不会被丢掉，兜进一个「其他」板块 —— 静默吞数据比多一个板块更糟。
    """
    by_brand: dict[str, list[dict]] = {}
    for row in rows:
        by_brand.setdefault(row.get("brand") or "", []).append(row)

    known = [s.get("code") for s in subcategories]
    out: list[dict] = []
    for sub in subcategories:
        brand = sub.get("code")
        brand_rows = by_brand.get(brand, [])
        out.append(
            {
                "key": f"{category}|{brand}|{basis}",
                "category": category,
                "brand": brand,
                "brand_label": sub.get("label") or brand,
                "brand_hint": sub.get("hint") or "",
                "basis": basis,
                "basis_label": SECTION_BASIS_LABEL.get(basis, basis),
                "title": section_title(sub.get("label") or brand, basis),
                "rows": brand_rows,
                "empty": not brand_rows,
                "stats": _row_stats(brand_rows),
                "movers": section_movers(brand_rows),
                "watch": section_watch(brand_rows),
            }
        )

    leftovers = {b: rs for b, rs in by_brand.items() if b and b not in known}
    for brand, brand_rows in sorted(leftovers.items()):
        out.append(
            {
                "key": f"{category}|{brand}|{basis}",
                "category": category,
                "brand": brand,
                "brand_label": brand,
                "brand_hint": "未在子分类矩阵中登记的厂商",
                "basis": basis,
                "basis_label": SECTION_BASIS_LABEL.get(basis, basis),
                "title": section_title(brand, basis),
                "rows": brand_rows,
                "empty": False,
                "stats": _row_stats(brand_rows),
                "movers": section_movers(brand_rows),
                "watch": section_watch(brand_rows),
            }
        )
    return out


def build_report_matrix(
    session: Session,
    days: int = 180,
    platforms: tuple[str, ...] | None = None,
    real_only: bool = True,
    categories: tuple[str, ...] | None = None,
) -> dict:
    """板块化日报：品类 × 厂商（N卡/A卡/I卡、IU/AU）× 品相（全新/二手）各自成块。

    ⚠️ 为什么是「每块各调一次 `build_daily_report`」，而不是「算一次再在前端拆」：
       日低价 / 今日最低平台 / 涨跌幅都是**在平台集合上算出来的**。全新板块的
       平台集合只有京东 + 拼多多，二手只有闲鱼 —— 若先按全市场算完再拆，
       全新板块的「日低价」会被闲鱼二手价污染，涨跌幅也会跨品相乱比。
       所以必须带着各自的平台集合**重新算一遍**。代价是每个品类多跑一遍查询，
       本地 SQLite + 百级型号，可忽略。

    ⚠️ 也**不复用** basis="all" 的结果：那样拆出来的「全新日低价」是假的。
    """
    cats = tuple(categories or REPORT_CATEGORIES)
    sections: list[dict] = []
    meta: dict = {"date": None, "newest_batch": "", "batch_count": 0}
    category_blocks: list[dict] = []

    for category in cats:
        subs = subcategories_of(category)
        cat_sections: list[dict] = []
        for basis, _label in SECTION_BASIS:
            rep = build_daily_report(
                session,
                category=category,
                days=days,
                basis=basis,
                platforms=platforms,
                real_only=real_only,
            )
            for sec in split_sections(category, basis, rep["rows"], subs):
                sec.update(
                    {
                        "category_label": rep["category_label"],
                        "columns": rep["columns"],
                        "vendor_colors": rep["vendor_colors"],
                        "platforms": rep["platforms"],
                        "coverage": rep["coverage"],
                        "bench_field": rep["bench_field"],
                        "date": rep["date"],
                        "newest_batch": rep["newest_batch"],
                    }
                )
                cat_sections.append(sec)
                sections.append(sec)
            if rep["date"] and (meta["date"] is None or rep["date"] > meta["date"]):
                meta["date"] = rep["date"]
            if rep["newest_batch"] and rep["newest_batch"] > meta["newest_batch"]:
                meta["newest_batch"] = rep["newest_batch"]
            meta["batch_count"] = max(meta["batch_count"], rep["batch_count"])

        category_blocks.append(
            {
                "category": category,
                "category_label": CATEGORIES.get(category, category),
                "sections": cat_sections,
                "filled": sum(1 for s in cat_sections if not s["empty"]),
                "total": len(cat_sections),
            }
        )

    return {
        "date": meta["date"],
        "newest_batch": meta["newest_batch"],
        "batch_count": meta["batch_count"],
        "days": days,
        "real_only": real_only,
        "vendor_colors": VENDOR_COLORS,
        "basis_labels": dict(SECTION_BASIS),
        "watch_limit": WATCH_LIMIT,
        "categories": category_blocks,
        "sections": sections,
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
