"""趋势指标计算。

所有指标都基于「跨平台口径」的日线序列：
  all_low  —— 全市场最低价（含二手），最贴近"最低多少钱能买到"
  new_low  —— 全新平台最低价
  used_low —— 二手平台最低价
  all_avg  —— 全市场均价
"""
from __future__ import annotations

import math
import statistics
import threading
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Listing, Platform, PriceDaily, Product

SERIES_KEYS = ("new_low", "used_low", "all_low", "new_avg", "all_avg", "all_high")


# ---------------------------------------------------------------- 市场序列缓存

# 为什么需要缓存
# ---------------
# `load_market_series()` 要把 300+ 型号 × 180 天的聚合行全部读出来再在 Python 里
# 拼成序列，实测约 110ms（冷启动首次要读 7 万行，接近 800ms）。而打开一次首页会
# 触发 **5 次**：
#     /api/overview  内部 3 次（自身快照 + 涨跌榜上下各一次）
#     /api/market-index  1 次
#     /api/products      1 次（按板块两次）
# 它们在同一个服务里并发执行，于是首屏要等好几秒。
#
# 序列只依赖「天数 + 品类」和库里的数据，而数据只在采集时变化（每小时一轮），
# 所以按数据版本号做进程内缓存即可 —— 版本没变就直接复用。
#
# 返回值视为**只读**：调用方不得修改 frame 或其内部的列表，否则会污染缓存。

_FRAME_CACHE: dict[tuple, tuple[str, dict]] = {}
_FRAME_CACHE_MAX = 16

# 单飞锁：首屏那 5 个请求会同时到达、同时发现缓存是空的。
# 没有这把锁，每个请求都会把整份序列算一遍（实测首屏 4 秒）；
# 有了它，只有第一个真正去算，其余等结果复用。
_FRAME_LOCKS: dict[tuple, threading.Lock] = {}
_FRAME_LOCKS_GUARD = threading.Lock()


def _frame_lock(key: tuple) -> threading.Lock:
    with _FRAME_LOCKS_GUARD:
        lock = _FRAME_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _FRAME_LOCKS[key] = lock
        return lock


def data_version(session: Session) -> str:
    """数据版本号 —— 库里数据变了它才变，用于让缓存失效。

    取多个维度是因为不同变更方式留下的痕迹不同：
      · PriceDaily 的 MAX(id) 能捕获"删除后重新插入"（采集流水线的固定行为）
      · MAX(trade_date) 能捕获跨日
      · COUNT(*) 能捕获纯粹的删除
      · 型号数 / 启用平台数变化会改变口径，也必须算进去

    ⚠️ **必须同时盯 `listings`**（踩过的坑）
    --------------------------------------------------
    最早只盯 `price_daily`（聚合表），因为趋势缓存读的就是它。但覆盖统计
    （`services/coverage.py`）读的是 **`listings` 明细表** —— 于是采集刚写完明细、
    聚合还没跑完的那段时间里，覆盖数会停在旧值：界面上"已采到 84 个"明明该变成
    85，刷新却纹丝不动。实测就是这么发现的（在库副本上写一条真实报价，覆盖数不变）。

    这里只用 `MAX(listing.id)` 而**不**加 `COUNT(*)`：主键 MAX 走索引，实测 **0.1ms**；
    而 COUNT(*) 要 8.6ms —— 这个函数每个请求都要调，不能为它多花 8ms。
    明细的删除只发生在流水线里、且总是伴随重新插入（纯删除会被跳过），
    所以 MAX(id) 足够捕获变更。
    """
    row = session.execute(
        select(
            func.max(PriceDaily.id),
            func.max(PriceDaily.trade_date),
            func.count(PriceDaily.id),
        )
    ).one()
    listing_max = session.execute(select(func.max(Listing.id))).scalar()
    product_count = session.execute(select(func.count(Product.id))).scalar() or 0
    # 平台启停会改变口径（只统计启用平台），也必须纳入版本号
    platform_count = (
        session.execute(
            select(func.count()).select_from(Platform).where(Platform.is_active.is_(True))
        ).scalar()
        or 0
    )
    return f"{row[0]}|{row[1]}|{row[2]}|{listing_max}|{product_count}|{platform_count}"


def warm_market_cache(session: Session, days: int = 180) -> None:
    """预计算默认窗口的市场序列，供服务启动时预热调用。

    目的：让**第一个**用户请求也走缓存。否则服务重启后的首次访问要等整份序列
    算完（实测约 800ms），用户会感觉"打开网站要等一下"。
    """
    load_market_series(session, days=days)


# ---------------------------------------------------------------- 工具

def _pct(cur: float | None, prev: float | None) -> float | None:
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / prev * 100.0


def _lookup(dates: list[date], values: list[float | None], target: date) -> float | None:
    """取 target 当日或之前最近的可用值。"""
    for i in range(len(dates) - 1, -1, -1):
        if dates[i] <= target:
            return values[i]
    return None


def _clean(values: list[float | None]) -> list[float]:
    return [v for v in values if v is not None]


def _ma(values: list[float | None], window: int) -> float | None:
    seq = _clean(values[-window:])
    if len(seq) < max(2, window // 3):
        return None
    return sum(seq) / len(seq)


def _volatility(values: list[float | None], window: int = 30) -> float | None:
    seq = _clean(values[-(window + 1) :])
    if len(seq) < 5:
        return None
    rets = [
        (seq[i] - seq[i - 1]) / seq[i - 1]
        for i in range(1, len(seq))
        if seq[i - 1]
    ]
    if len(rets) < 4:
        return None
    return statistics.pstdev(rets) * 100.0


def _percentile(values: list[float | None], window: int = 90) -> float | None:
    seq = _clean(values[-window:])
    if len(seq) < 5:
        return None
    last = seq[-1]
    below = sum(1 for v in seq if v <= last)
    return below / len(seq) * 100.0


# ---------------------------------------------------------------- 市场序列

def load_market_series(
    session: Session, days: int = 180, category: str | None = None
) -> dict[int, dict]:
    """加载每个型号的跨平台日线序列（带进程内缓存，返回值视为只读）。"""
    key = (days, category)
    version = data_version(session)

    hit = _FRAME_CACHE.get(key)
    if hit is not None and hit[0] == version:
        return hit[1]

    with _frame_lock(key):
        # 双重检查：等在锁上的这段时间里，可能已经有别的线程算好了
        hit = _FRAME_CACHE.get(key)
        if hit is not None and hit[0] == version:
            return hit[1]

        frame = _compute_market_series(session, days=days, category=category)

        # 简单 LRU：满了就丢掉最早插入的一项（组合数很少，不会真成为瓶颈）
        if len(_FRAME_CACHE) >= _FRAME_CACHE_MAX and key not in _FRAME_CACHE:
            _FRAME_CACHE.pop(next(iter(_FRAME_CACHE)))
        _FRAME_CACHE[key] = (version, frame)
        return frame


def _compute_market_series(
    session: Session, days: int = 180, category: str | None = None
) -> dict[int, dict]:
    """真正干活的实现 —— 只允许 `load_market_series()` 调用。"""
    since = date.today() - timedelta(days=days - 1)

    stmt = (
        select(
            PriceDaily.product_id,
            PriceDaily.trade_date,
            Platform.kind,
            func.min(PriceDaily.min_price),
            func.max(PriceDaily.max_price),
            func.avg(PriceDaily.avg_price),
        )
        .join(Platform, Platform.id == PriceDaily.platform_id)
        .where(
            PriceDaily.trade_date >= since,
            # 停用平台不参与市场均线 —— 它们的报价口径不同（媒体参考价系统性偏高），
            # 混进来会把"市场行情线"整体抬高。
            Platform.is_active.is_(True),
        )
        .group_by(PriceDaily.product_id, PriceDaily.trade_date, Platform.kind)
    )
    if category:
        stmt = stmt.join(Product, Product.id == PriceDaily.product_id).where(
            Product.category == category
        )

    raw: dict[int, dict[date, dict[str, tuple[float, float, float]]]] = {}
    for pid, d, kind, lo, hi, avg in session.execute(stmt).all():
        raw.setdefault(pid, {}).setdefault(d, {})[kind] = (float(lo), float(hi), float(avg))

    frame: dict[int, dict] = {}
    for pid, by_date in raw.items():
        dates = sorted(by_date)
        series: dict = {"dates": dates}
        for key in SERIES_KEYS:
            series[key] = []

        for d in dates:
            kinds = by_date[d]
            new = kinds.get("new")
            used = kinds.get("used")

            lows = [t[0] for t in (new, used) if t is not None]
            highs = [t[1] for t in (new, used) if t is not None]
            avgs = [t[2] for t in (new, used) if t is not None]

            series["new_low"].append(new[0] if new else None)
            series["used_low"].append(used[0] if used else None)
            series["all_low"].append(min(lows) if lows else None)
            series["all_high"].append(max(highs) if highs else None)
            series["new_avg"].append(new[2] if new else None)
            series["all_avg"].append(sum(avgs) / len(avgs) if avgs else None)

        # 首次有数据之前的 None 用后面的首个有效值回填，保证图表连续
        for key in SERIES_KEYS:
            seq = series[key]
            first = next((v for v in seq if v is not None), None)
            for i, v in enumerate(seq):
                if v is None:
                    seq[i] = first

        frame[pid] = series
    return frame


# ---------------------------------------------------------------- 指标

def compute_metrics(series: dict) -> dict:
    """基于日线序列计算趋势指标。"""
    dates: list[date] = series["dates"]
    low: list[float | None] = series["all_low"]
    if not dates:
        return {}

    today = dates[-1]
    last = low[-1]

    changes: dict[str, dict | None] = {}
    for label, back in (("d1", 1), ("d7", 7), ("d30", 30)):
        prev = _lookup(dates, low, today - timedelta(days=back))
        pct = _pct(last, prev)
        changes[label] = (
            None
            if pct is None
            else {"abs": round(last - prev, 2), "pct": round(pct, 2)}
        )

    clean_low = _clean(low)
    hist_min = min(clean_low) if clean_low else None
    hist_max = max(clean_low) if clean_low else None
    min_date = dates[low.index(hist_min)] if hist_min is not None else None
    max_date = dates[low.index(hist_max)] if hist_max is not None else None

    pct90 = _percentile(low, 90)
    chg30 = changes["d30"]["pct"] if changes["d30"] else None
    chg7 = changes["d7"]["pct"] if changes["d7"] else None

    return {
        "last_date": today.isoformat(),
        "last_low": round(last, 2) if last is not None else None,
        "last_new_low": _r(series["new_low"][-1]),
        "last_used_low": _r(series["used_low"][-1]),
        "last_avg": _r(series["all_avg"][-1]),
        "changes": changes,
        "ma7": _r(_ma(low, 7)),
        "ma30": _r(_ma(low, 30)),
        "volatility_30d": _r(_volatility(low, 30), 3),
        "percentile_90d": _r(pct90, 1),
        "history": {
            "min": _r(hist_min),
            "max": _r(hist_max),
            "min_date": min_date.isoformat() if min_date else None,
            "max_date": max_date.isoformat() if max_date else None,
            "span_pct": _r(_pct(hist_max, hist_min), 1),
        },
        "signal": _signal(pct90, chg30, chg7),
    }


def _r(v, nd: int = 2):
    return None if v is None else round(float(v), nd)


def _signal(pct90: float | None, chg30: float | None, chg7: float | None) -> dict:
    if pct90 is None:
        return {"level": "neutral", "text": "数据积累中"}
    if pct90 <= 20:
        return {"level": "good", "text": "处于近 90 天低位，可考虑入手"}
    if pct90 >= 80:
        return {"level": "warn", "text": "处于近 90 天高位，建议观望"}
    if chg30 is not None and chg30 >= 8:
        return {"level": "warn", "text": "近 30 天涨幅明显，留意追高"}
    if chg30 is not None and chg30 <= -8:
        return {"level": "good", "text": "近 30 天明显回落"}
    if chg7 is not None and abs(chg7) < 0.5:
        return {"level": "neutral", "text": "价格平稳，波动很小"}
    return {"level": "neutral", "text": "价格区间内正常波动"}


# ---------------------------------------------------------------- 平台维度

def load_platform_series(session: Session, product_id: int, days: int = 180) -> list[dict]:
    """加载某型号在各平台的日线序列。"""
    since = date.today() - timedelta(days=days - 1)
    stmt = (
        select(
            Platform.id,
            Platform.code,
            Platform.name,
            Platform.kind,
            Platform.color,
            PriceDaily.trade_date,
            PriceDaily.min_price,
            PriceDaily.max_price,
            PriceDaily.avg_price,
            PriceDaily.sample_count,
        )
        .join(Platform, Platform.id == PriceDaily.platform_id)
        .where(
            PriceDaily.product_id == product_id,
            PriceDaily.trade_date >= since,
            # 只看仍在用的平台 —— 停用平台的聚合行会残留在表里（数据未删，
            # 便于回滚），不过滤就会在型号详情/对比页冒出来。
            Platform.is_active.is_(True),
        )
        .order_by(Platform.sort_order, PriceDaily.trade_date)
    )

    grouped: dict[str, dict] = {}
    for pid, code, name, kind, color, d, lo, hi, avg, cnt in session.execute(stmt).all():
        item = grouped.setdefault(
            code,
            {
                "platform_id": pid,
                "code": code,
                "name": name,
                "kind": kind,
                "color": color,
                "dates": [],
                "min": [],
                "max": [],
                "avg": [],
                "last_count": 0,
            },
        )
        item["dates"].append(d)
        item["min"].append(round(float(lo), 2))
        item["max"].append(round(float(hi), 2))
        item["avg"].append(round(float(avg), 2))
        item["last_count"] = int(cnt)

    return list(grouped.values())


def _spread_of(rows: list[dict]) -> dict | None:
    """同口径平台间价差（少于 2 个平台无意义）。"""
    if len(rows) < 2:
        return None
    lo, hi = rows[0], rows[-1]
    return {
        "cheapest": {"code": lo["code"], "name": lo["name"], "price": lo["min"]},
        "priciest": {"code": hi["code"], "name": hi["name"], "price": hi["min"]},
        "abs": _r(hi["min"] - lo["min"]),
        "pct": _r(_pct(hi["min"], lo["min"]), 2),
    }


def build_latest_platform_table(platform_series: list[dict]) -> tuple[list[dict], dict]:
    """取每个平台最后一天的价格，用于横向比价。

    价差按口径分开计算：全新平台之间比、二手平台之间比，
    避免把二手价和全新价混在一起得出无意义的 90% 价差。
    """
    table: list[dict] = []
    for item in platform_series:
        if not item["dates"]:
            continue
        table.append(
            {
                "code": item["code"],
                "name": item["name"],
                "kind": item["kind"],
                "color": item["color"],
                "date": item["dates"][-1].isoformat(),
                "min": item["min"][-1],
                "avg": item["avg"][-1],
                "max": item["max"][-1],
                "sample_count": item["last_count"],
            }
        )
    table.sort(key=lambda r: r["min"])

    if table:
        base = table[0]["min"]
        for row in table:
            row["vs_cheapest_pct"] = _r(_pct(row["min"], base), 2)
            row["is_cheapest"] = row["min"] == base

    spread = {
        "new": _spread_of([r for r in table if r["kind"] == "new"]),
        "used": _spread_of([r for r in table if r["kind"] == "used"]),
        "overall": _spread_of(table),
    }
    return table, spread


# ---------------------------------------------------------------- 对外入口

def build_product_trend(session: Session, product_id: int, days: int = 180) -> dict | None:
    product = session.get(Product, product_id)
    if product is None:
        return None

    frame = load_market_series(session, days=days)
    series = frame.get(product_id)
    if not series or not series["dates"]:
        return {
            "product": _product_brief(product),
            "metrics": {},
            "series": {"dates": [], **{k: [] for k in SERIES_KEYS}},
            "platforms": [],
            "latest_platforms": [],
            "spread": None,
        }

    metrics = compute_metrics(series)
    platform_series = load_platform_series(session, product_id, days=days)
    latest_table, spread = build_latest_platform_table(platform_series)
    if spread:
        metrics["platform_spread"] = spread

    return {
        "product": _product_brief(product),
        "metrics": metrics,
        "series": {
            "dates": [d.isoformat() for d in series["dates"]],
            **{k: series[k] for k in SERIES_KEYS},
        },
        "platforms": [
            {
                **{k: v for k, v in item.items() if k not in ("dates", "min", "max", "avg")},
                "dates": [d.isoformat() for d in item["dates"]],
                "min": item["min"],
                "max": item["max"],
                "avg": item["avg"],
            }
            for item in platform_series
        ],
        "latest_platforms": latest_table,
        "spread": spread,
    }


def _product_brief(product: Product) -> dict:
    from ..seed_data import category_label

    return {
        "id": product.id,
        "category": product.category,
        "category_label": category_label(product.category),
        "brand": product.brand,
        "model": product.model,
        "spec": product.spec,
        "base_price": product.base_price,
    }


BASIS_KEYS = {"all": "all_low", "new": "new_low", "used": "used_low"}


def build_snapshot(
    session: Session,
    period: int = 1,
    category: str | None = None,
    days: int = 180,
    basis: str = "all",
) -> list[dict]:
    """全型号最新价格快照（含区间涨跌与迷你走势）。

    Args:
        basis: 主口径 —— all 全市场最低价 / new 全新最低价 / used 二手最低价
    """
    from ..seed_data import category_label

    key = BASIS_KEYS.get(basis, "all_low")
    frame = load_market_series(session, days=days, category=category)
    if not frame:
        return []

    products = {
        p.id: p
        for p in session.execute(
            select(Product).where(Product.id.in_(list(frame.keys())))
        ).scalars()
    }

    rows: list[dict] = []
    for pid, series in frame.items():
        product = products.get(pid)
        if product is None:
            continue
        dates, low = series["dates"], series[key]
        if not dates:
            continue
        last = low[-1]
        prev = _lookup(dates, low, dates[-1] - timedelta(days=period))
        pct = _pct(last, prev)
        spark = [v for v in low[-30:] if v is not None]
        per90 = _percentile(low, 90)
        rows.append(
            {
                "product_id": pid,
                "model": product.model,
                "brand": product.brand,
                "spec": product.spec,
                "category": product.category,
                "category_label": category_label(product.category),
                "basis": basis,
                "latest": _r(last),
                "latest_new_low": _r(series["new_low"][-1]),
                "latest_used_low": _r(series["used_low"][-1]),
                "prev": _r(prev),
                "abs": _r((last - prev) if (last is not None and prev is not None) else None),
                "change_pct": None if pct is None else _r(pct, 2),
                "percentile_90d": _r(per90, 1),
                "sparkline": [round(v, 2) for v in spark],
            }
        )
    return rows


def build_ranking(
    session: Session,
    period: int = 1,
    category: str | None = None,
    direction: str = "up",
    limit: int = 20,
    days: int = 180,
    basis: str = "all",
    rows: list[dict] | None = None,
) -> list[dict]:
    """涨跌排行榜。

    约定：
      · `up`   **只收录上涨的型号**（change_pct > 0），从高到低
      · `down` **只收录下跌的型号**（change_pct < 0），从低到高
      · 没有可比基准（change_pct 为 None）的一律排除 —— 它们既不是涨也不是跌

    为什么必须先过滤再排序（踩过的坑）：
    早先是 `sorted(rows, key=(change_pct is None, value), reverse=direction=="up")`，
    想用元组第一项把"无数据"压到最后。但 `reverse=True` 会把**整个 key 一起反转** ——
    `True > False`，于是涨榜里 None 行反而排到了最前面，一屏 8 条全是"—"，
    而跌榜因为 `reverse=False` 恰好正确，所以这个 bug 只在涨榜露出。
    正确做法是先把 None 滤掉，排序键里就不再需要那个守卫。

    rows 可传入已算好的快照（build_overview 同时要涨榜和跌榜，
    复用同一份快照即可，不必把 300+ 型号的指标算两遍）。
    """
    if rows is None:
        rows = build_snapshot(session, period=period, category=category, days=days, basis=basis)

    want_up = direction == "up"
    picked = [
        r
        for r in rows
        if r["change_pct"] is not None and (r["change_pct"] > 0 if want_up else r["change_pct"] < 0)
    ]
    # 这里是新列表，可以原地排序（rows 本身可能是调用方还要用的共享快照）
    picked.sort(key=lambda r: r["change_pct"], reverse=want_up)
    return picked[:limit]


def build_market_index(session: Session, days: int = 180) -> dict:
    """品类价格指数：每个型号以首日价为 100 归一化，再按品类取平均。

    用于观察"整个品类是涨是跌"，不受个别型号绝对价格量级影响。
    """
    from ..seed_data import CATEGORY_ORDER, category_label

    frame = load_market_series(session, days=days)
    products = {p.id: p for p in session.execute(select(Product)).scalars()}

    buckets: dict[str, dict[date, list[float]]] = {}
    for pid, series in frame.items():
        product = products.get(pid)
        if product is None:
            continue
        base = next((v for v in series["new_low"] if v), None)
        if not base:
            continue
        for d, value in zip(series["dates"], series["new_low"]):
            if value is None:
                continue
            buckets.setdefault(product.category, {}).setdefault(d, []).append(value / base * 100.0)

    series_out = []
    for code in CATEGORY_ORDER:
        by_date = buckets.get(code)
        if not by_date:
            continue
        dates = sorted(by_date)
        series_out.append(
            {
                "category": code,
                "label": category_label(code),
                "data": [
                    [d.isoformat(), round(sum(by_date[d]) / len(by_date[d]), 2)] for d in dates
                ],
            }
        )
    return {"days": days, "series": series_out}


def build_overview(session: Session, days: int = 180, period: int = 1, basis: str = "all") -> dict:
    """总览统计：品类分布 + 整体涨跌计数。"""
    from ..seed_data import CATEGORY_ORDER

    snapshot = build_snapshot(session, period=period, days=days, basis=basis)
    by_category: dict[str, dict] = {}
    for row in snapshot:
        item = by_category.setdefault(
            row["category"],
            {
                "code": row["category"],
                "label": row["category_label"],
                "count": 0,
                "up": 0,
                "down": 0,
                "flat": 0,
            },
        )
        item["count"] += 1
        pct = row["change_pct"]
        if pct is None:
            item["flat"] += 1
        elif pct > 0:
            item["up"] += 1
        elif pct < 0:
            item["down"] += 1
        else:
            item["flat"] += 1

    ordered = [by_category[c] for c in CATEGORY_ORDER if c in by_category]
    newest = session.execute(select(func.max(PriceDaily.trade_date))).scalar()

    return {
        "tracked": len(snapshot),
        "period": period,
        "basis": basis,
        "newest_date": newest.isoformat() if newest else None,
        "up_count": sum(1 for r in snapshot if (r["change_pct"] or 0) > 0),
        "down_count": sum(1 for r in snapshot if (r["change_pct"] or 0) < 0),
        "flat_count": sum(1 for r in snapshot if r["change_pct"] == 0),
        "categories": ordered,
        "top_gainers": build_ranking(
            session, period=period, direction="up", limit=8, days=days, basis=basis, rows=snapshot
        ),
        "top_losers": build_ranking(
            session, period=period, direction="down", limit=8, days=days, basis=basis, rows=snapshot
        ),
    }
