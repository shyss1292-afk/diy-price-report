"""日聚合：把明细快照归并成 型号 × 平台 × 日期 的价格区间。"""
from __future__ import annotations

import statistics
from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..models import Listing, Platform, PriceDaily, Product

_BATCH = 800

# 少于这么多条报价就不算分位（退回 min）—— 3 条里的"25 分位"没有统计意义。
#
# ⚠️ **这个数字必须与 `services/trend.py` 的 `MIN_SAMPLES` 一致**，两边不
# 一致时会出这种怪事：某天 4 条报价用 min 当稳健价、另一天 14 条用真 p25，
# 两个数根本不在一个尺度上，相减就是假涨跌（实测 Ryzen 9 9950X3D：
# 4 条的 ¥2400 vs 14 条的 p25 ¥3500 → 假 +45.83%）。
# trend 直接 import 这个常量，不另定义。
P25_MIN_SAMPLES = 5


def _p25(prices: list[float]) -> float:
    """一组报价的 25 分位价（稳健底价）。

    为什么要它（2026-10-03，由"4080 Super 一天涨 44%"引出）
    ------------------------------------------------------
    `min_price` 是**单点**：当天最便宜的那一条说了算。于是"最便宜的那条
    换人了"就被读成"涨价了"。实测 RX 7600 8G：
      · 10-02 闲鱼 22 条，最低 ¥1050（一条描述完全正常、却比次低价 ¥1480
        低 29% 的挂牌）→ 10-03 最低 ¥1420 → 涨跌榜算出 **+35.24%**
      · 而当天全市场均价 ¥1600 → ¥1618，实际上只动了约 1%
    那条 ¥1050 不是坏卡、不是捆绑，质量标记抓不到 —— 只能用**分位**
    把"最低的极少数"从基准里拿掉。

    为什么是 25 分位、且样本 < 5 时退回 min
    ------------------------------------
      · 22 条的 25 分位 ≈ 去掉最低 5 条后的价格，正好把"引流价 / 已售出"
        这类离群点挡在外面，同时仍贴着市场下沿（比中位数更接近"能买到的低价"）。
      · 样本少于 5 条时分位没有统计意义（3 条的第 1 条就是 min），
        此时**如实退回 min**，不制造虚假的"稳健"。
    """
    if len(prices) < P25_MIN_SAMPLES:
        return min(prices)
    return statistics.quantiles(prices, n=4, method="inclusive")[0]


def refresh_daily(session: Session, since: date | None = None) -> int:
    """从 listings 重算 price_daily（幂等 upsert）。

    Args:
        since: 只重算该日期之后的数据；None 表示全量重算。

    Returns:
        写入的聚合行数。

    四条硬过滤（2026-09-23 三条，2026-10-03 补第四条）
    ----------------------------------------------
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
      · `Listing.quality_flags == ''` —— 嫌疑报价不进行情（2026-10-03 补）。
        见 `collectors/quality.py`：bundle / defective / industrial。
        **这是"4080 Super 一天涨 44%"的成因**：京东 10-02 那轮 3 条全是
        涡轮/工包卡（¥9800/¥11515/¥15000），把最低价从零售卡的 ¥7039
        顶到了 ¥9800 —— 口径换人，却被读成 +39%。

    ⚠️ 加过滤后必须**全量重算**（`since=None`）—— 否则被排除记录的
       旧聚合行会残留在表里，等于没清。
    """
    det_stmt = (
        select(
            Listing.product_id,
            Listing.platform_id,
            Listing.trade_date,
            Listing.price,
        )
        .join(Platform, Platform.id == Listing.platform_id)
        .join(Product, Product.id == Listing.product_id)
        .where(
            Listing.is_synthetic.is_(False),
            # ---- 第四条硬过滤（2026-10-03 补）：嫌疑报价不进聚合 ----
            #
            # 见 `collectors/quality.py`：bundle（多商品捆绑列表，价格归属不可靠）、
            # defective（坏卡）、industrial（涡轮/工包/算力卡，价格体系完全不同）。
            #
            # 用 `== ''` 而不是 `NOT IN (...)`：这条路径要能吃到 `quality_flags`
            # 的**新增取值**（以后加新标记时忘记改这里，新标记反而会静默放行）；
            # 而"什么都没标"才是唯一可信的状态。
            Listing.quality_flags == "",
            Platform.is_active.is_(True),
            Product.is_active.is_(True),
        )
    )

    if since is not None:
        det_stmt = det_stmt.where(Listing.trade_date >= since)

    # 在 Python 侧分组 —— 因为要算 **25 分位**，SQLite 没有 percentile 函数。
    # 真实明细只有 2.5 万行量级，全量拉进来分组是毫秒级的；
    # 而不这么做就得把 min 交给 SQL、把 p25 交给另一条查询，两条口径容易漂移。
    grouped: dict[tuple[int, int, date], list[float]] = {}
    for pid, plid, d, price in session.execute(det_stmt).all():
        grouped.setdefault((pid, plid, d), []).append(float(price))

    rows = [
        {
            "product_id": pid,
            "platform_id": plid,
            "trade_date": d,
            "min_price": min(prices),
            "p25_price": _p25(prices),
            "max_price": max(prices),
            "avg_price": sum(prices) / len(prices),
            "sample_count": len(prices),
        }
        for (pid, plid, d), prices in grouped.items()
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
                "p25_price": stmt.excluded.p25_price,
                "max_price": stmt.excluded.max_price,
                "avg_price": stmt.excluded.avg_price,
                "sample_count": stmt.excluded.sample_count,
            },
        )
        session.execute(stmt)
        session.flush()

    return len(rows)
