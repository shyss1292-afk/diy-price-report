"""REST 接口层。"""
from __future__ import annotations

import threading
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import APP_VERSION
from ..db import get_db
from ..models import CrawlLog, Listing, Platform, PriceDaily, Product
from ..seed_bench import bench_score
from ..seed_data import (
    subcategories_of,
    CATEGORIES,
    CATEGORY_GROUPS,
    CATEGORY_ORDER,
    category_group,
    category_label,
)
from ..services import coverage as coverage_svc
from ..services import report as report_svc
from ..services import trend as trend_svc
from ..services import listing_history
from ..services.pipeline import run_pipeline

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------- 采集任务状态

class _CollectState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.result: dict | None = None
        self.error: str | None = None
        self.trigger: str = "manual"


STATE = _CollectState()


def _run_collect_job(sources: list[str] | None, backfill_days: int, trigger: str) -> None:
    try:
        STATE.result = run_pipeline(
            day=date.today(),
            sources=sources,
            trigger=trigger,
            backfill_days=backfill_days,
        )
        STATE.error = None
    except Exception as exc:  # 后台线程内必须兜住，否则状态会卡住
        STATE.error = str(exc)
    finally:
        STATE.running = False
        STATE.finished_at = datetime.now().isoformat(timespec="seconds")


def _start_collect(sources: list[str] | None, backfill_days: int, trigger: str) -> dict:
    with STATE.lock:
        if STATE.running:
            raise HTTPException(status_code=409, detail="已有采集任务正在运行")
        STATE.running = True
        STATE.started_at = datetime.now().isoformat(timespec="seconds")
        STATE.finished_at = None
        STATE.trigger = trigger
    threading.Thread(
        target=_run_collect_job,
        args=(sources, backfill_days, trigger),
        daemon=True,
        name="diyprice-collect",
    ).start()
    return {"status": "started", "started_at": STATE.started_at, "trigger": trigger}


# ---------------------------------------------------------------- 采集覆盖

@router.get("/coverage")
def get_coverage(db: Session = Depends(get_db)) -> dict:
    """真实采集覆盖：共多少型号、采完多少、完成率、各平台明细。

    口径见 `services/coverage.py` 顶部说明 —— 只统计真实（非 mock）数据。
    """
    return coverage_svc.build_coverage(db)


# ---------------------------------------------------------------- 元数据

@router.get("/meta")
def get_meta(db: Session = Depends(get_db)) -> dict:
    platforms = db.execute(select(Platform).order_by(Platform.sort_order)).scalars().all()
    # ⚠️ 必须过滤 is_active —— 归档品类（主板/固态/散热/电源/机箱/内存）
    #    仍留在 products 表里（历史数据要能查），但**不算监控范围**。
    #    不过滤的话界面会显示「全部 351」，而实际只有 130 个型号 ——
    #    数字和列表对不上，比不显示还糟。
    counts = dict(
        db.execute(
            select(Product.category, func.count(Product.id))
            .where(Product.is_active.is_(True))
            .group_by(Product.category)
        ).all()
    )
    brand_counts = {
        (cat, brand): n
        for cat, brand, n in db.execute(
            select(Product.category, Product.brand, func.count(Product.id))
            .where(Product.is_active.is_(True))
            .group_by(Product.category, Product.brand)
        ).all()
    }
    categories = [
        {
            "code": code,
            "label": CATEGORIES[code],
            "count": counts.get(code, 0),
            # 板块分组：前端据此把「核心配件」与「其他硬件」分开排
            "group": category_group(code),
        }
        for code in CATEGORY_ORDER
    ]
    return {
        "version": APP_VERSION,
        "categories": categories,
        # 二级细分：前端据此渲染「N卡 / A卡」「IU / AU」chip。
        # counts 按 brand 统计，同样只算 active —— 与一级分类口径一致。
        "subcategories": [
            {
                "category": cat,
                "items": [
                    {**sub, "count": brand_counts.get((cat, sub["code"]), 0)}
                    for sub in subcategories_of(cat)
                ],
            }
            for cat in CATEGORY_ORDER
        ],
        "groups": [
            {**g, "count": sum(counts.get(c, 0) for c in g["categories"])}
            for g in CATEGORY_GROUPS
        ],
        "platforms": [
            {
                "code": p.code,
                "name": p.name,
                "kind": p.kind,
                "color": p.color,
                "is_active": p.is_active,
            }
            for p in platforms
        ],
        "product_count": sum(counts.values()),
    }


@router.get("/overview")
def get_overview(
    days: int = Query(180, ge=7, le=730),
    period: int = Query(7, ge=1, le=90),
    basis: str = Query("all", pattern="^(all|new|used)$"),
    db: Session = Depends(get_db),
) -> dict:
    return trend_svc.build_overview(db, days=days, period=period, basis=basis)


# ---------------------------------------------------------------- 型号

# 品牌排序时的固定顺序 —— 把三家主要厂商排在前面，未收录的品牌按名称附后
_BRAND_ORDER = ("NVIDIA", "AMD", "Intel")


def _sort_products(rows: list[dict], sort: str) -> list[dict]:
    """型号列表的排序 —— **排序逻辑只放这一处**，前端不再自己排一遍。

    价格类：
      · `price`（**默认**）—— 价格从高到低。保持默认是必须的：首页「重点关注型号」
        依赖这个顺序，它的 `diversify()` 按价格降序在每个品类里取代表作。
      · `price_asc` —— 价格从低到高
    性能类：
      · `brand` —— 品牌分组（NVIDIA → AMD → Intel → 其他），组内按性能跑分降序
      · `bench` —— 性能跑分从高到低
    行情类（保留原有能力）：
      · `change` / `change_asc` —— 涨幅优先 / 跌幅优先
      · `pctile` —— 90 天分位从低到高（找历史低位）

    跑分/涨跌缺失时用 0（或 ±999）参与排序使其沉底，**不伪造数值** ——
    界面上该显示 — 的仍显示 —。
    """
    if sort == "brand":
        rank = {b: i for i, b in enumerate(_BRAND_ORDER)}
        return sorted(
            rows,
            key=lambda r: (
                rank.get(r["brand"], len(_BRAND_ORDER)),
                r["brand"],
                -(r.get("bench") or 0),
                r["model"],
            ),
        )
    if sort == "bench":
        return sorted(rows, key=lambda r: (-(r.get("bench") or 0), r["model"]))
    if sort == "price_asc":
        return sorted(rows, key=lambda r: (r["latest"] is None, r["latest"] or 0, r["model"]))
    if sort == "change":
        return sorted(rows, key=lambda r: (-(r["change_pct"] if r["change_pct"] is not None else -999), r["model"]))
    if sort == "change_asc":
        return sorted(rows, key=lambda r: (r["change_pct"] if r["change_pct"] is not None else 999, r["model"]))
    if sort == "pctile":
        return sorted(rows, key=lambda r: (r["percentile_90d"] if r["percentile_90d"] is not None else 999, r["model"]))
    return sorted(rows, key=lambda r: (r["latest"] or 0), reverse=True)


@router.get("/products")
def list_products(
    category: str | None = None,
    brand: str | None = Query(None, description="厂商细分：NVIDIA / AMD / Intel"),
    group: str | None = Query(None, description="板块分组：core / other"),
    q: str | None = None,
    period: int = Query(1, ge=1, le=90),
    basis: str = Query("all", pattern="^(all|new|used)$"),
    days: int = Query(180, ge=7, le=730),
    limit: int | None = Query(None, ge=1, le=1000),
    sort: str = Query(
        "price",
        pattern="^(price|price_asc|brand|bench|change|change_asc|pctile)$",
        description="price=价格降序 price_asc=价格升序 brand=品牌分组 bench=跑分降序 change=涨幅优先 change_asc=跌幅优先 pctile=历史分位低到高",
    ),
    with_platforms: bool = Query(False, description="是否附上三个平台各自的最新价"),
    db: Session = Depends(get_db),
) -> dict:
    group_codes: set[str] | None = None
    if group:
        spec = next((g for g in CATEGORY_GROUPS if g["code"] == group), None)
        if spec is None:
            raise HTTPException(status_code=400, detail=f"未知板块：{group}")
        group_codes = set(spec["categories"])

    rows = trend_svc.build_snapshot(db, period=period, category=category, days=days, basis=basis)

    # 补上「型号库里有、但还没有任何行情数据」的型号。
    #
    # 为什么必须补：`build_snapshot` 是遍历 price_daily 聚合结果生成的，所以
    # **刚加入型号库、采集还没轮到的型号根本不会出现** —— 用户搜「6800 XT」
    # 会看到"没有结果"，而其实型号已经建好了、只是还没价格。
    # 型号列表是产品目录，应该列全；没价格的格子显示 — 即可。
    known = {r["product_id"] for r in rows}
    stmt = select(Product).where(Product.is_active.is_(True))
    if category:
        stmt = stmt.where(Product.category == category)
    if brand:
        stmt = stmt.where(Product.brand == brand)
    for p in db.execute(stmt.order_by(Product.id)).scalars():
        if p.id in known:
            continue
        rows.append(
            {
                "product_id": p.id,
                "model": p.model,
                "brand": p.brand,
                "spec": p.spec,
                "category": p.category,
                "category_label": category_label(p.category),
                "basis": basis,
                "latest": None,
                "latest_new_low": None,
                "latest_used_low": None,
                "prev": None,
                "abs": None,
                "change_pct": None,
                "percentile_90d": None,
                "sparkline": [],
                # 从没采到过任何行情 —— 与"今天没轮到、回退到历史"要区分开：
                # 前者 captured_date 为 None（界面显示「暂无数据」），
                # 后者有日期（界面显示「昨日」/「N 天前」）
                "captured_date": None,
                "is_today": False,
                "stale_days": None,
            }
        )

    if brand:
        # build_snapshot 不过滤品牌（它是全量快照），这里补一刀
        rows = [r for r in rows if (r.get("brand") or "") == brand]
    if group_codes is not None:
        rows = [r for r in rows if r["category"] in group_codes]
    if q:
        needle = q.strip().lower()
        rows = [
            r
            for r in rows
            if needle in r["model"].lower() or needle in r["brand"].lower() or needle in (r["spec"] or "").lower()
        ]

    for r in rows:
        r["bench"] = bench_score(r["model"], r["category"])

    if with_platforms:
        # 三个平台各自的最新价（真实价优先，详见 coverage._compute_platform_latest）。
        # 默认不带 —— 首页用不到，而首次计算要约 0.5s。
        latest = coverage_svc.build_platform_latest(db, days=days)
        codes = coverage_svc.DISPLAY_PLATFORMS
        for r in rows:
            cells = latest.get(r["product_id"], {})
            r["platforms"] = {c: cells.get(c) for c in codes}

    rows = _sort_products(rows, sort)
    # total 始终是**过滤后**的总数，limit 只截断返回项 ——
    # 否则前端拿 limit 去显示"共 N 个型号"会得到错误的数字。
    total = len(rows)
    if limit is not None:
        rows = rows[:limit]
    return {"total": total, "items": rows}


@router.get("/products/{product_id}")
def get_product(product_id: int, db: Session = Depends(get_db)) -> dict:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="型号不存在")
    from ..seed_data import category_label

    return {
        "id": product.id,
        "category": product.category,
        "category_label": category_label(product.category),
        "brand": product.brand,
        "model": product.model,
        "spec": product.spec,
        "base_price": product.base_price,
        "aliases": [a for a in (product.aliases or "").split(",") if a],
    }


@router.get("/products/{product_id}/trend")
def get_product_trend(
    product_id: int,
    days: int = Query(180, ge=7, le=730),
    db: Session = Depends(get_db),
) -> dict:
    data = trend_svc.build_product_trend(db, product_id, days=days)
    if data is None:
        raise HTTPException(status_code=404, detail="型号不存在")
    return data


@router.get("/compare")
def compare_products(
    ids: str = Query(..., description="逗号分隔的型号 ID"),
    days: int = Query(90, ge=7, le=730),
    db: Session = Depends(get_db),
) -> dict:
    try:
        id_list = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="ids 参数格式错误") from exc

    id_list = id_list[:6]
    if not id_list:
        raise HTTPException(status_code=400, detail="至少需要一个型号 ID")

    products = {
        p.id: p for p in db.execute(select(Product).where(Product.id.in_(id_list))).scalars()
    }
    frame = trend_svc.load_market_series(db, days=days)

    series = []
    for pid in id_list:
        product = products.get(pid)
        item = frame.get(pid)
        if product is None or not item:
            continue
        series.append(
            {
                "product_id": pid,
                "model": product.model,
                "brand": product.brand,
                "category": product.category,
                "data": [
                    [d.isoformat(), v]
                    for d, v in zip(item["dates"], item["all_low"])
                    if v is not None
                ],
            }
        )
    return {"days": days, "series": series}


# ---------------------------------------------------------------- 排行

@router.get("/market-index")
def get_market_index(
    days: int = Query(180, ge=7, le=730), db: Session = Depends(get_db)
) -> dict:
    """品类价格指数（首日 = 100）。"""
    return trend_svc.build_market_index(db, days=days)


@router.get("/ranking")
def get_ranking(
    period: int = Query(1, ge=1, le=90),
    direction: str = Query("up", pattern="^(up|down)$"),
    basis: str = Query("all", pattern="^(all|new|used)$"),
    category: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    days: int = Query(180, ge=7, le=730),
    db: Session = Depends(get_db),
) -> dict:
    rows = trend_svc.build_ranking(
        db, period=period, category=category, direction=direction, limit=limit, days=days, basis=basis
    )
    return {"period": period, "basis": basis, "direction": direction, "items": rows}


# ---------------------------------------------------------------- 日报

@router.get("/daily-report/categories")
def daily_report_categories() -> dict:
    """日报可选的品类清单（只含显卡与 CPU）。"""
    return {"items": report_svc.report_categories()}


@router.get("/daily-report/platforms")
def daily_report_platforms() -> dict:
    """日报可选平台清单。"""
    return {"items": report_svc.report_platforms()}


@router.get("/daily-report")
def get_daily_report(
    category: str = Query("gpu"),
    days: int = Query(180, ge=7, le=730),
    basis: str = Query("all", pattern="^(all|new|used)$"),
    platforms: str | None = Query(None, description="逗号分隔的平台 code，默认重点三平台"),
    db: Session = Depends(get_db),
) -> dict:
    """日报表：型号 × 平台横向比价，含史低价 / 日低价 / 跑分 / 性价比 / 涨跌幅。"""
    if category not in report_svc.REPORT_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"日报暂不支持该品类：{category}")
    wanted = None
    if platforms:
        wanted = tuple(p.strip() for p in platforms.split(",") if p.strip())
    return report_svc.build_daily_report(
        db, category=category, days=days, basis=basis, platforms=wanted
    )


@router.get("/daily-report/matrix")
def get_daily_report_matrix(
    days: int = Query(180, ge=7, le=730),
    categories: str | None = Query(None, description="逗号分隔，默认显卡+处理器"),
    platforms: str | None = Query(None, description="逗号分隔的平台 code，默认重点三平台"),
    db: Session = Depends(get_db),
) -> dict:
    """**板块化**日报：品类 × 厂商（N卡/A卡/I卡、IU/AU）× 品相（全新/二手）各自成块。

    与 `/api/daily-report`（单张大表）的区别在**口径**而非排版：

      · 每块的「日低价 / 今日最低平台 / 涨跌幅」都带着**该块自己的平台集合**
        重算 —— 全新块只含京东+拼多多，二手块只含闲鱼。先按全市场算完再拆，
        全新块的日低价会被闲鱼二手价污染。
      · 「涨跌榜」与「今日重点观察」**只在块内**排序，不跨品牌、不跨品相混排。

    `/api/daily-report` 保持原样不变（向后兼容），本接口是新增的板块视图。
    """
    cats = None
    if categories:
        cats = tuple(c.strip() for c in categories.split(",") if c.strip())
        bad = [c for c in cats if c not in report_svc.REPORT_CATEGORIES]
        if bad:
            raise HTTPException(status_code=400, detail=f"日报暂不支持该品类：{','.join(bad)}")
    wanted = None
    if platforms:
        wanted = tuple(p.strip() for p in platforms.split(",") if p.strip())
    return report_svc.build_report_matrix(
        db, days=days, platforms=wanted, categories=cats
    )


# ---------------------------------------------------------------- 采集管理

@router.get("/admin/status")
def admin_status(db: Session = Depends(get_db)) -> dict:
    def count(model) -> int:
        return int(db.execute(select(func.count()).select_from(model)).scalar() or 0)

    date_range = db.execute(
        select(func.min(PriceDaily.trade_date), func.max(PriceDaily.trade_date))
    ).one()

    return {
        "running": STATE.running,
        "started_at": STATE.started_at,
        "finished_at": STATE.finished_at,
        "trigger": STATE.trigger,
        "last_result": STATE.result,
        "last_error": STATE.error,
        "counts": {
            "products": count(Product),
            # 只数启用中的平台 —— 停用平台的明细还在库里（便于回滚），
            # 若照全表计数，管理页会显示一个与实际可用源对不上的平台数。
            "platforms": db.execute(
                select(func.count()).select_from(Platform).where(Platform.is_active.is_(True))
            ).scalar_one(),
            "listings": count(Listing),
            "price_daily": count(PriceDaily),
            "crawl_log": count(CrawlLog),
        },
        "data_range": {
            "from": date_range[0].isoformat() if date_range[0] else None,
            "to": date_range[1].isoformat() if date_range[1] else None,
        },
    }


@router.get("/admin/health")
def admin_health(db: Session = Depends(get_db)) -> dict:
    """采集健康 + 告警状态。

    为什么要暴露到网页：2026-09-29 全天 0 条跑了一整天没人知道 ——
    光有 macOS 通知还不够（通知会消失、机器可能没人看）。
    网页上是**可回看**的：今天覆盖了多少、哪个源在熔断、最近报过什么警。
    """
    from ..services import alerting, breaker, healthcheck

    # 今日覆盖率：有真实报价的在追型号数 / 在追型号总数
    total = int(db.execute(
        select(func.count()).select_from(Product).where(
            Product.is_active.is_(True), Product.category.in_(("gpu", "cpu"))
        )
    ).scalar() or 0)
    today = date.today()
    covered = int(db.execute(
        select(func.count(func.distinct(Listing.product_id))).select_from(Listing).join(
            Platform, Platform.id == Listing.platform_id
        ).where(
            Listing.is_synthetic.is_(False),
            Platform.is_active.is_(True),
            Listing.trade_date == today,
        )
    ).scalar() or 0)

    return {
        "coverage": {
            "date": today.isoformat(),
            "covered": covered,
            "total": total,
            "pct": round(100.0 * covered / total, 1) if total else 0.0,
        },
        "health": healthcheck.status(),
        "alerting": alerting.status(),
        "breaker": breaker.snapshot(),
        "breaker_summary": breaker.active_summary(),
    }


@router.get("/admin/logs")
def admin_logs(limit: int = Query(30, ge=1, le=200), db: Session = Depends(get_db)) -> dict:
    rows = (
        db.execute(select(CrawlLog).order_by(CrawlLog.id.desc()).limit(limit)).scalars().all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "source": r.source,
                "trigger": r.trigger,
                "status": r.status,
                "started_at": r.started_at.isoformat(sep=" ", timespec="seconds"),
                "finished_at": r.finished_at.isoformat(sep=" ", timespec="seconds") if r.finished_at else None,
                "duration_sec": r.duration_sec,
                "items": r.items,
                "message": r.message,
            }
            for r in rows
        ]
    }


@router.post("/admin/collect")
def admin_collect(payload: dict | None = None) -> dict:
    payload = payload or {}
    sources = payload.get("sources") or None
    backfill_days = int(payload.get("backfill_days") or 0)
    trigger = "backfill" if backfill_days > 1 else "manual"
    return _start_collect(sources, backfill_days, trigger)


@router.post("/admin/backfill")
def admin_backfill(payload: dict | None = None) -> dict:
    payload = payload or {}
    days = int(payload.get("days") or 180)
    days = max(2, min(days, 730))
    return _start_collect(payload.get("sources") or None, days, "backfill")


# ======================================================================
# 单品（挂牌）信号 —— 降价榜 / 新上架 / 单品历史
# ======================================================================
#
# 这三条读的是 `listing_snapshots`（粒度 = 平台 × 挂牌 × 日期），
# 而其余所有报表读的是 `price_daily`（粒度 = 型号 × 平台 × 日期）。
#
# 为什么两者都得有：市场均价会把**单条挂牌的降价抹平** ——
# 一个卖家把 3500 的卡砍到 2800，撼动不了 400 条样本的中位数，
# 但二手捡漏恰恰就发生在这一条上。所以这套报表回答的是
# 「哪条具体商品在降」「哪条是新挂出来的」，而不是「这个型号便宜了没」。


@router.get("/listings/drops")
def listing_drops(
    days: int = Query(7, ge=1, le=180),
    min_drop_pct: float = Query(3.0, ge=0.0, le=90.0),
    limit: int = Query(30, ge=1, le=200),
    category: str | None = Query(None),
    db: Session = Depends(get_db),
) -> dict:
    """降价榜 —— 与**该挂牌自己上一次**被看到的价格比。

    `min_drop_pct` 默认 3%：低于它的波动多是改价凑整/包邮折算，噪声大于信号。
    """
    rows = listing_history.listing_drops(
        db, days=days, min_drop_pct=min_drop_pct, limit=limit, category=category
    )
    return {
        "days": days,
        "min_drop_pct": min_drop_pct,
        "category": category,
        "count": len(rows),
        "items": rows,
    }


@router.get("/listings/new")
def listing_new(
    days: int = Query(3, ge=1, le=60),
    limit: int = Query(30, ge=1, le=200),
    category: str | None = Query(None),
    db: Session = Depends(get_db),
) -> dict:
    """新上架 —— 我们**第一次见到**它、且在最近 N 天内。

    注意与 `publish_time` 的区别：那是平台口径的发布时间（卖家可能把挂了很久的
    商品重新推送），这里是**我们自己的观测口径**。两个值都返回，供人自行判断。
    """
    rows = listing_history.newest_listings(db, days=days, limit=limit, category=category)
    return {"days": days, "category": category, "count": len(rows), "items": rows}


@router.get("/listings/{platform_code}/{item_id}/history")
def listing_history_of(
    platform_code: str,
    item_id: str,
    days: int = Query(180, ge=1, le=730),
    db: Session = Depends(get_db),
) -> dict:
    """单条挂牌的价格历史（按天）。"""
    return listing_history.build_listing_history(
        db, platform_code=platform_code, item_id=item_id, days=days
    )


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "version": APP_VERSION}
