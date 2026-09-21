"""真实采集覆盖统计 —— 首页「真实采集覆盖」板块的数据源。

口径（务必和界面上写的说明一致，否则数字会被误读）
------------------------------------------------
* **真实**：`listings.is_synthetic = 0` 且平台启用。合成（mock）数据一律不算 ——
  否则会把"界面有 180 天曲线"误当成"采集做完了"。
* **采集范围**：由 `DIYPRICE_FOCUS_CATEGORY` 决定，默认只有显卡 + CPU。
  和 `scripts/collect_scheduled.sh` 用的是同一个环境变量，避免界面与实际采集脱节。
* **完成率** = 范围内已采到真实价格的型号数 / 范围内型号总数。
  不是"报价条数"的比率 —— 一个型号采到 1 条或 500 条都算完成。
"""
from __future__ import annotations

import os
import threading

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from ..models import Listing, Platform, Product
from ..seed_data import CATEGORIES, CATEGORY_ORDER
from .trend import data_version

# 与采集脚本默认值保持一致
DEFAULT_SCOPE = "gpu,cpu"

# 型号列表里横向展示的三个平台。
# 平台表里还有天猫 / 慢慢买 / 转转（is_active=1 但没有真实适配器），
# 它们的价格全是模拟数据 —— 摊在表里只会让人分不清哪个能信，所以不列。
DISPLAY_PLATFORMS: tuple[str, ...] = ("jd", "pdd", "xianyu")


def focus_categories() -> list[str]:
    """当前采集范围（品类代码列表）。"""
    raw = os.environ.get("DIYPRICE_FOCUS_CATEGORY", DEFAULT_SCOPE)
    codes = [c.strip() for c in raw.split(",") if c.strip()]
    return [c for c in codes if c in CATEGORIES] or list(CATEGORY_ORDER)


def _fmt_ts(value) -> str:
    """把 max(captured_at) 之类的结果统一成 'YYYY-MM-DD HH:MM:SS'。

    SQLite 走原生驱动时可能是字符串，走 ORM 类型时不带时区的 datetime —— 两种都要接住。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value[:19].replace("T", " ")
    return value.strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ 进程内缓存
#
# 为什么需要：真实明细有 4 万多行，"不重复型号数"这类 `COUNT(DISTINCT ...)`
# 单次约 200ms，整份统计要跑 5~6 个这样的查询 —— 冷启动实测 865ms。
# 而覆盖数据**只在采集时变化**（每小时一轮），所以按数据版本号缓存即可。
#
# 与 trend 的用法一致：单飞锁 + 双重检查，避免首屏几个请求同时打进来时
# 各算一遍（那会让最慢的那个决定整体耗时）。
_COVERAGE_CACHE: dict[str, tuple[str, dict]] = {}
_COVERAGE_LOCK = threading.Lock()
_CACHE_KEY = "coverage"


def build_coverage(session: Session) -> dict:
    """采集覆盖统计（带进程内缓存，按数据版本号失效）。

    ⚠️ 返回值视为**只读**：它是缓存里的同一份对象，调用方改它等于污染缓存。
    """
    # 采集范围也进缓存键 —— 改了 DIYPRICE_FOCUS_CATEGORY 应当立刻重算
    key = f"{_CACHE_KEY}:{','.join(focus_categories())}"
    version = data_version(session)
    hit = _COVERAGE_CACHE.get(key)
    if hit is not None and hit[0] == version:
        return hit[1]

    with _COVERAGE_LOCK:
        hit = _COVERAGE_CACHE.get(key)
        if hit is not None and hit[0] == version:
            return hit[1]
        data = _compute_coverage(session)
        _COVERAGE_CACHE[key] = (version, data)
        return data


def _compute_coverage(session: Session) -> dict:
    scope = focus_categories()
    real = (Listing.is_synthetic.is_(False), Platform.is_active.is_(True))

    def _count_products(categories: list[str] | None = None) -> int:
        stmt = select(func.count(Product.id))
        if categories:
            stmt = stmt.where(Product.category.in_(categories))
        return session.execute(stmt).scalar() or 0

    def _count_covered(categories: list[str] | None = None) -> int:
        stmt = (
            select(func.count(distinct(Listing.product_id)))
            .select_from(Listing)
            .join(Platform, Platform.id == Listing.platform_id)
            .join(Product, Product.id == Listing.product_id)
            .where(*real)
        )
        if categories:
            stmt = stmt.where(Product.category.in_(categories))
        return session.execute(stmt).scalar() or 0

    scope_total = _count_products(scope)
    scope_covered = _count_covered(scope)
    lib_total = _count_products()
    lib_covered = _count_covered()

    # ------------------------------------------------------------ 平台明细
    rows = session.execute(
        select(
            Platform.code,
            Platform.name,
            Platform.kind,
            func.count(distinct(Listing.product_id)).label("models"),
            func.count(Listing.id).label("listings"),
            func.count(distinct(Listing.trade_date)).label("days"),
            func.max(Listing.captured_at).label("last_at"),
        )
        .select_from(Listing)
        .join(Platform, Platform.id == Listing.platform_id)
        .join(Product, Product.id == Listing.product_id)
        .where(*real, Product.category.in_(scope))
        .group_by(Platform.id)
        .order_by(func.count(distinct(Listing.product_id)).desc())
    ).all()

    platforms = [
        {
            "code": code,
            "name": name,
            "kind": kind,
            "models": models,
            # 完成率按"覆盖型号 / 范围内型号"算，这样三个平台可以直接横向比
            "completion": round(models / scope_total * 100, 1) if scope_total else 0.0,
            "listings": listings,
            "days": days,
            "last_at": _fmt_ts(last_at),
        }
        for code, name, kind, models, listings, days, last_at in rows
    ]

    # ------------------------------------------------------ 每个型号覆盖几个平台
    # 用来回答"能不能横向比价"：只有 1 个平台的型号是没法比价的
    one_platform_sub = (
        select(Listing.product_id)
        .join(Platform, Platform.id == Listing.platform_id)
        .join(Product, Product.id == Listing.product_id)
        .where(*real, Product.category.in_(scope))
        .group_by(Listing.product_id)
        .having(func.count(distinct(Listing.platform_id)) == 1)
        .subquery()
    )
    combo = session.execute(select(func.count()).select_from(one_platform_sub)).scalar() or 0

    span = session.execute(
        select(
            func.min(Listing.trade_date).label("d0"),
            func.max(Listing.trade_date).label("d1"),
            func.max(Listing.captured_at).label("last_at"),
        )
        .select_from(Listing)
        .join(Platform, Platform.id == Listing.platform_id)
        .join(Product, Product.id == Listing.product_id)
        .where(*real, Product.category.in_(scope))
    ).one()

    return {
        "scope": {
            "categories": scope,
            "labels": [CATEGORIES.get(c, c) for c in scope],
            "total": scope_total,
            "covered": scope_covered,
            "missing": scope_total - scope_covered,
            "completion": round(scope_covered / scope_total * 100, 1) if scope_total else 0.0,
        },
        # 全库口径：让用户看得见"网站追踪 309 个，但只采了显卡+CPU"
        "library": {
            "total": lib_total,
            "covered": lib_covered,
            "completion": round(lib_covered / lib_total * 100, 1) if lib_total else 0.0,
        },
        "platforms": platforms,
        "single_platform_models": combo,
        "date_range": {
            "from": span.d0.isoformat() if span.d0 else None,
            "to": span.d1.isoformat() if span.d1 else None,
            "latest_at": _fmt_ts(span.last_at),
        },
    }


# ------------------------------------------------------------------ 三平台最新价
#
# 型号列表要横向比价，就得拿到"每个型号在每个平台的最新价"。
# 价格取 `price_daily`（已按 型号×平台×日期 聚合好的最低价），
# 另外单独标出"这个格子的数据是不是真实采集来的" —— price_daily 没有
# is_synthetic 字段，聚合时也没过滤，所以里面混着 mock。
# 不标出来的话，用户会拿模拟价去比价，那比没有更糟。

_PLATFORM_CACHE: dict[str, tuple[str, dict]] = {}
_PLATFORM_LOCK = threading.Lock()


def build_platform_latest(session: Session, days: int = 180) -> dict[int, dict]:
    """每个型号在三个平台上的最新价，返回 {product_id: {code: {...}}}。

    每个格子形如：
        {"price": 3799.0, "date": "2026-09-18", "real": True}

    `real=False` 表示该价格只来自模拟数据 —— 界面必须弱化显示，
    不能让用户拿它去比价。

    ⚠️ 返回值视为**只读**（缓存里的同一份对象）。
    """
    codes = DISPLAY_PLATFORMS
    key = f"plat:{','.join(codes)}:{days}"
    version = data_version(session)
    hit = _PLATFORM_CACHE.get(key)
    if hit is not None and hit[0] == version:
        return hit[1]

    with _PLATFORM_LOCK:
        hit = _PLATFORM_CACHE.get(key)
        if hit is not None and hit[0] == version:
            return hit[1]
        data = _compute_platform_latest(session, days)
        _PLATFORM_CACHE[key] = (version, data)
        return data


def _compute_platform_latest(session: Session, days: int) -> dict[int, dict]:
    from datetime import date, timedelta

    from ..models import PriceDaily

    since = date.today() - timedelta(days=days)
    codes = DISPLAY_PLATFORMS

    # ⚠️ 真实价必须**只从 is_synthetic=0 的明细算**，不能直接读 price_daily。
    #
    # 原因：`refresh_daily()` 聚合时不过滤 is_synthetic，而 mock 往京东/拼多多/闲鱼
    # 也写数据。于是 price_daily.min_price 取的是「真实 + 模拟」的最小值 ——
    # mock 的假低价会把真实价盖掉（实测 RTX 5090 京东格显示 17997 的模拟值）。
    # 拿这种数字去比价，比空着更糟。
    #
    # 做法：先按 (型号, 平台, 日期) 聚合真实明细，再取每个组合的最新一天。
    real_daily = (
        select(
            Listing.product_id.label("pid"),
            Listing.platform_id.label("plid"),
            Listing.trade_date.label("d"),
            func.min(Listing.price).label("p"),
        )
        .where(Listing.is_synthetic.is_(False), Listing.trade_date >= since)
        .group_by(Listing.product_id, Listing.platform_id, Listing.trade_date)
        .subquery()
    )
    rn = (
        func.row_number()
        .over(partition_by=(real_daily.c.pid, real_daily.c.plid),
              order_by=real_daily.c.d.desc())
        .label("rn")
    )
    ranked = select(
        real_daily.c.pid, real_daily.c.plid, real_daily.c.d, real_daily.c.p, rn
    ).subquery()
    real_latest = session.execute(
        select(ranked.c.pid, Platform.code, ranked.c.d, ranked.c.p)
        .join(Platform, Platform.id == ranked.c.plid)
        .where(ranked.c.rn == 1, Platform.code.in_(codes))
    ).all()

    out: dict[int, dict] = {}
    for pid, code, trade_date, price in real_latest:
        out.setdefault(pid, {})[code] = {
            "price": None if price is None else round(float(price), 2),
            "date": trade_date.isoformat() if trade_date else None,
            "real": True,
        }

    # 没有真实数据的格子，退回 price_daily 的最新值并**标记 real=False** ——
    # 界面上会弱化显示，让人一眼看出这格不能用来比价。
    rn2 = (
        func.row_number()
        .over(partition_by=(PriceDaily.product_id, PriceDaily.platform_id),
              order_by=PriceDaily.trade_date.desc())
        .label("rn")
    )
    ranked2 = (
        select(
            PriceDaily.product_id.label("pid"),
            PriceDaily.platform_id.label("plid"),
            PriceDaily.trade_date.label("d"),
            PriceDaily.min_price.label("p"),
            rn2,
        )
        .where(PriceDaily.trade_date >= since)
        .subquery()
    )
    fallback = session.execute(
        select(ranked2.c.pid, Platform.code, ranked2.c.d, ranked2.c.p)
        .join(Platform, Platform.id == ranked2.c.plid)
        .where(ranked2.c.rn == 1, Platform.code.in_(codes))
    ).all()

    for pid, code, trade_date, price in fallback:
        cell = out.setdefault(pid, {}).get(code)
        if cell is None:
            out[pid][code] = {
                "price": None if price is None else round(float(price), 2),
                "date": trade_date.isoformat() if trade_date else None,
                "real": False,
            }
    return out
