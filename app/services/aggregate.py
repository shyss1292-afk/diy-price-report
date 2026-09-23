"""日聚合：把明细快照归并成 型号 × 平台 × 日期 的价格区间。"""
from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..models import Listing, Platform, PriceDaily, Product

_BATCH = 800


def refresh_daily(session: Session, since: date | None = None) -> int:
    """从 listings 重算 price_daily（幂等 upsert）。

    Args:
        since: 只重算该日期之后的数据；None 表示全量重算。

    Returns:
        写入的聚合行数。

    三条硬过滤（2026-09-23 补）
    ---------------------------
    `price_daily` 是**行情计算的底座**，脏数据一旦进来，上层所有指标
    （最低价 / 均线 / 涨跌幅 / 分位数）全被污染，而且事后无法分辨
    "这条脏值是采集来的还是聚合来的"。所以过滤必须在**入口**做。

      · `is_synthetic == 0` —— 模拟/占位报价不参与任何行情计算。
        它们只是为了让界面在数据空窗期不至于全空，混进统计会把
        "真实市场最低价"整体拉偏。实测旧表 87883 行里，
        按真实明细只算得出 701 行 —— **98% 是模拟数据的聚合**。
      · `Platform.is_active` —— 只统计当前激活平台。停用平台
        （zol / pconline）是媒体参考价，口径与电商成交价不同，
        且早已不再采集；留着只会让历史序列前后不可比。
      · `Product.is_active` —— 只统计**在监控范围内**的型号。
        2026-09-23 起监控收窄到三大件（显卡/CPU/内存），
        长尾品类（主板/固态/电源/散热/机箱）样本稀疏到没有统计意义。

    ⚠️ 加过滤后必须**全量重算**（`since=None`）—— 否则被排除记录的
       旧聚合行会残留在表里，等于没清。
    """
    agg_stmt = (
        select(
            Listing.product_id,
            Listing.platform_id,
            Listing.trade_date,
            func.min(Listing.price),
            func.max(Listing.price),
            func.avg(Listing.price),
            func.count(Listing.id),
        )
        .join(Platform, Platform.id == Listing.platform_id)
        .join(Product, Product.id == Listing.product_id)
        .where(
            Listing.is_synthetic.is_(False),
            Platform.is_active.is_(True),
            Product.is_active.is_(True),
        )
        .group_by(Listing.product_id, Listing.platform_id, Listing.trade_date)
    )

    if since is not None:
        agg_stmt = agg_stmt.where(Listing.trade_date >= since)

    rows = [
        {
            "product_id": pid,
            "platform_id": plid,
            "trade_date": d,
            "min_price": float(lo),
            "max_price": float(hi),
            "avg_price": float(avg),
            "sample_count": int(cnt),
        }
        for pid, plid, d, lo, hi, avg, cnt in session.execute(agg_stmt).all()
    ]

    if not rows:
        return 0

    # 全量重算 = **整表重建**，先把旧聚合行清空。
    #
    # 为什么不能只靠 upsert：upsert 只覆盖**这次算出来的**行，删不掉历史遗留的。
    # 实测（2026-09-23）旧表 87883 行，加过滤后按真实明细只算得出 701 行 ——
    # 也就是说 **98% 的聚合行来自模拟数据**。光加 WHERE 条件重算，
    # 那 8 万多行会原样留着继续污染行情，等于没清。
    if since is None:
        from sqlalchemy import delete

        session.execute(delete(PriceDaily))
        session.flush()

    for i in range(0, len(rows), _BATCH):
        chunk = rows[i : i + _BATCH]
        stmt = sqlite_insert(PriceDaily).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=["product_id", "platform_id", "trade_date"],
            set_={
                "min_price": stmt.excluded.min_price,
                "max_price": stmt.excluded.max_price,
                "avg_price": stmt.excluded.avg_price,
                "sample_count": stmt.excluded.sample_count,
            },
        )
        session.execute(stmt)
        session.flush()

    return len(rows)
