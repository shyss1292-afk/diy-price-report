"""单品（挂牌）历史 —— 写入快照 + 查询「降价 Top N」。

设计要点（对照 `ai-goofish-monitor/src/services/price_history_service.py`）：

  · **写入幂等**：同 `(平台, item_id, 日期)` 走 upsert 覆盖，重跑/多批次
    不会堆行。理由：同一轮里同一商品可能被搜到两次（不同关键词命中同一型号），
    堆行会让"见过几次"这种统计直接失真。
  · **读时过滤**：`is_synthetic=0` + `platforms.is_active=1` + `products.is_active=1`
    三条闸门都在 SQL 里（项目教训：过滤条件散落多处必漏，见归档品类那次）。
  · **降价判据**：与本商品**自己上一次被看到的价格**比（`LAG`），不是与市场均价比。
    `ai-goofish-monitor` 用 market_avg 算 `deal_score`，但那只回答"便不便宜"；
    "降没降价"必须用自身历史。

⚠️ 与 `price_daily` 的分工（不要混用）：
    price_daily      型号级 —— 日低价/均价/涨跌幅/性价比指数都走它
    listing_snapshots 单品级 —— 只用于"这条挂牌降了没""新上架"这类**个体信号**
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..models import ListingSnapshot

logger = logging.getLogger("diyprice.listings")

# 单条 INSERT 的绑定参数有上限，分块写
_CHUNK = 200

# 快照字段的**唯一**清单 —— 写入与落库两处共用，避免"加字段时漏一处"
SNAPSHOT_KEYS = (
    "platform_id", "product_id", "item_id", "trade_date", "captured_at", "batch",
    "price", "ori_price", "title_raw", "seller", "area", "condition", "url",
    "publish_time", "want_num", "tags", "is_synthetic",
)

# 覆盖更新时**不**改的列：身份与日期是键的一部分
_UPSERT_UPDATE = (
    "captured_at", "batch", "price", "ori_price", "title_raw", "seller", "area",
    "condition", "url", "publish_time", "want_num", "tags",
)


def record_snapshots(session, rows: list[dict]) -> int:
    """写入单品快照。返回处理行数。

    行必须是 `SNAPSHOT_KEYS` 的完整字典（缺键会让整批 INSERT 的参数个数不一致，
    SQLAlchemy 会直接报错）—— 所以这里显式校验，宁可在入口拦下。
    """
    if not rows:
        return 0

    missing = [i for i, r in enumerate(rows) if set(r) != set(SNAPSHOT_KEYS)]
    if missing:
        raise ValueError(
            f"快照行字段不齐（第 {missing[:3]} 行）；"
            f"要求恰好是 {sorted(SNAPSHOT_KEYS)}"
        )

    written = 0
    for i in range(0, len(rows), _CHUNK):
        chunk = rows[i : i + _CHUNK]
        stmt = sqlite_insert(ListingSnapshot).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=["platform_id", "item_id", "trade_date"],
            set_={col: getattr(stmt.excluded, col) for col in _UPSERT_UPDATE},
        )
        session.execute(stmt)
        written += len(chunk)
    return written


def snapshot_count(session, days: int | None = None) -> int:
    """快照条数（可选只看最近 N 天）—— 给管理页/自检用。"""
    sql = "SELECT COUNT(*) FROM listing_snapshots WHERE is_synthetic = 0"
    params: dict = {}
    if days:
        sql += " AND trade_date >= :since"
        params["since"] = (date.today() - timedelta(days=days)).isoformat()
    return int(session.execute(text(sql), params).scalar() or 0)


def build_listing_history(session, platform_code: str, item_id: str, days: int = 180) -> dict:
    """单条挂牌的价格历史（按天）。

    Returns:
        {"platform_code", "item_id", "title", "seller", "area", "url",
         "first_seen", "last_seen", "seen_days", "min_price", "max_price",
         "points": [{"day", "price"}, ...]}

    没有记录时返回 `{"item_id": ..., "points": []}`（而不是 None）——
    调用方拿到空列表比拿到 None 更容易处理，也少一处 None 判断。
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = session.execute(
        text(
            """
            SELECT s.trade_date, s.price, s.title_raw, s.seller, s.area, s.url,
                   p.model AS product_model
            FROM listing_snapshots s
            JOIN platforms pf ON pf.id = s.platform_id AND pf.is_active = 1
            JOIN products  p  ON p.id  = s.product_id  AND p.is_active = 1
            WHERE s.is_synthetic = 0 AND pf.code = :code AND s.item_id = :item
              AND s.trade_date >= :since
            ORDER BY s.trade_date
            """
        ),
        {"code": platform_code, "item": item_id, "since": since},
    ).mappings().all()

    if not rows:
        return {"platform_code": platform_code, "item_id": item_id, "points": []}

    prices = [float(r["price"]) for r in rows]
    return {
        "platform_code": platform_code,
        "item_id": item_id,
        "title": rows[-1]["title_raw"],
        "seller": rows[-1]["seller"],
        "area": rows[-1]["area"],
        "url": rows[-1]["url"],
        "product_model": rows[-1]["product_model"],
        "first_seen": str(rows[0]["trade_date"]),
        "last_seen": str(rows[-1]["trade_date"]),
        "seen_days": len(rows),
        "min_price": min(prices),
        "max_price": max(prices),
        "points": [{"day": str(r["trade_date"]), "price": float(r["price"])} for r in rows],
    }


def listing_drops(
    session,
    days: int = 7,
    min_drop_pct: float = 3.0,
    limit: int = 30,
    category: str | None = None,
) -> list[dict]:
    """「降价 Top N」—— 与本商品自己上一次被看到的价格比。

    这是**二手捡漏**的核心信号，而且它天然不会被市场均价抹平：
    400 条样本的中位数不会因为一个卖家砍价而变，但那条挂牌会。

    Args:
        min_drop_pct: 降幅门槛（百分比）。默认 3% —— 低于它的波动多是
            改价凑整/包邮折算，噪声大于信号。
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    sql = """
        WITH base AS (
            SELECT platform_id, item_id, product_id, trade_date, price,
                   title_raw, seller, area, url, want_num, publish_time
            FROM listing_snapshots
            WHERE is_synthetic = 0 AND trade_date >= :since
        ),
        stats AS (
            SELECT platform_id, item_id,
                   MIN(trade_date) AS first_seen,
                   COUNT(*)        AS seen_days,
                   MIN(price)      AS min_price,
                   MAX(price)      AS max_price
            FROM base GROUP BY platform_id, item_id
        ),
        seq AS (
            SELECT b.*,
                   LAG(price)      OVER (PARTITION BY platform_id, item_id ORDER BY trade_date)
                       AS prev_price,
                   LAG(trade_date) OVER (PARTITION BY platform_id, item_id ORDER BY trade_date)
                       AS prev_date
            FROM base b
        )
        SELECT s.item_id, s.trade_date, s.price, s.prev_price, s.prev_date,
               st.first_seen, st.seen_days, st.min_price, st.max_price,
               s.title_raw, s.seller, s.area, s.url, s.want_num, s.publish_time,
               pf.code AS platform_code, pf.name AS platform_name,
               p.id AS product_id, p.model AS product_model, p.category AS product_category
        FROM seq s
        JOIN stats st ON st.platform_id = s.platform_id AND st.item_id = s.item_id
        JOIN platforms pf ON pf.id = s.platform_id AND pf.is_active = 1
        JOIN products  p  ON p.id  = s.product_id  AND p.is_active = 1
        WHERE s.prev_price IS NOT NULL
          AND s.price > 0
          AND s.price <= s.prev_price * (1 - :ratio)
          AND (:category IS NULL OR p.category = :category)
        ORDER BY (s.prev_price - s.price) / s.prev_price DESC, s.trade_date DESC
        LIMIT :limit
    """
    rows = session.execute(
        text(sql),
        {
            "since": since,
            "ratio": max(0.0, float(min_drop_pct) / 100.0),
            "limit": max(1, int(limit)),
            "category": category,
        },
    ).mappings().all()

    out: list[dict] = []
    for r in rows:
        prev = float(r["prev_price"])
        now = float(r["price"])
        out.append(
            {
                "item_id": r["item_id"],
                "platform_code": r["platform_code"],
                "platform_name": r["platform_name"],
                "product_id": r["product_id"],
                "product_model": r["product_model"],
                "product_category": r["product_category"],
                "title": r["title_raw"],
                "seller": r["seller"],
                "area": r["area"],
                "url": r["url"],
                "want_num": int(r["want_num"] or 0),
                "publish_time": r["publish_time"],
                "price": round(now, 2),
                "prev_price": round(prev, 2),
                "drop": round(prev - now, 2),
                "drop_pct": round((prev - now) / prev * 100.0, 2) if prev else None,
                "prev_date": str(r["prev_date"]),
                "last_date": str(r["trade_date"]),
                "first_seen": str(r["first_seen"]),
                "seen_days": int(r["seen_days"]),
                "is_new": str(r["first_seen"]) == str(r["trade_date"]),
                "min_price": round(float(r["min_price"]), 2),
                "max_price": round(float(r["max_price"]), 2),
            }
        )
    return out


def newest_listings(
    session, days: int = 3, limit: int = 30, category: str | None = None
) -> list[dict]:
    """「新上架」—— 我们**第一次见到**它、且是在最近 N 天内。

    与 `publish_time`（平台口径的发布时间）是两件事：平台可能把一条挂了很久的
    商品重新推送，而"我们第一次见到"是我们自己的观测口径。
    两者都给出来，让人自己判断。
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = session.execute(
        text(
            """
            WITH base AS (
                SELECT platform_id, item_id, product_id, trade_date, price, title_raw,
                       seller, area, url, want_num, publish_time
                FROM listing_snapshots
                WHERE is_synthetic = 0
            ),
            firsts AS (
                SELECT platform_id, item_id, MIN(trade_date) AS first_seen
                FROM base GROUP BY platform_id, item_id
            )
            SELECT b.*, f.first_seen,
                   pf.code AS platform_code, pf.name AS platform_name,
                   p.id AS product_id, p.model AS product_model, p.category AS product_category
            FROM firsts f
            JOIN base b ON b.platform_id = f.platform_id AND b.item_id = f.item_id
                        AND b.trade_date = f.first_seen
            JOIN platforms pf ON pf.id = b.platform_id AND pf.is_active = 1
            JOIN products  p  ON p.id  = b.product_id AND p.is_active = 1
            WHERE f.first_seen >= :since
              AND (:category IS NULL OR p.category = :category)
            ORDER BY b.trade_date DESC, b.price ASC
            LIMIT :limit
            """
        ),
        {"since": since, "limit": max(1, int(limit)), "category": category},
    ).mappings().all()

    return [
        {
            "item_id": r["item_id"],
            "platform_code": r["platform_code"],
            "platform_name": r["platform_name"],
            "product_id": r["product_id"],
            "product_model": r["product_model"],
            "title": r["title_raw"],
            "seller": r["seller"],
            "area": r["area"],
            "url": r["url"],
            "price": round(float(r["price"]), 2),
            "want_num": int(r["want_num"] or 0),
            "publish_time": r["publish_time"],
            "first_seen": str(r["first_seen"]),
        }
        for r in rows
    ]
