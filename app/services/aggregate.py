"""日聚合：把明细快照归并成 型号 × 平台 × 日期 的价格区间。"""
from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..models import Listing, PriceDaily

_BATCH = 800


def refresh_daily(session: Session, since: date | None = None) -> int:
    """从 listings 重算 price_daily（幂等 upsert）。

    Args:
        since: 只重算该日期之后的数据；None 表示全量重算。

    Returns:
        写入的聚合行数。
    """
    agg_stmt = select(
        Listing.product_id,
        Listing.platform_id,
        Listing.trade_date,
        func.min(Listing.price),
        func.max(Listing.price),
        func.avg(Listing.price),
        func.count(Listing.id),
    ).group_by(Listing.product_id, Listing.platform_id, Listing.trade_date)

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
