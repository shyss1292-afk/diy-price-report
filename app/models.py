"""ORM 数据模型。

分层思路：
  products / platforms  —— 主数据（型号字典、平台字典）
  listings              —— 采集明细快照，逐条留痕，保留全量历史
  price_daily           —— 按 型号 × 平台 × 日期 聚合，趋势查询走这层
  crawl_log             —— 采集任务执行日志
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class Product(Base):
    """配件型号主数据（归一化后的标准 SKU）。"""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category: Mapped[str] = mapped_column(String(16), index=True)  # gpu/cpu/ram/mb/ssd/psu/cooler/case
    brand: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(128), unique=True)
    spec: Mapped[str] = mapped_column(String(160), default="")
    base_price: Mapped[float] = mapped_column(Float, default=0.0)  # 基准参考价（元）
    aliases: Mapped[str] = mapped_column(Text, default="")  # 逗号分隔，用于跨平台标题匹配
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    listings: Mapped[list["Listing"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    dailies: Mapped[list["PriceDaily"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Product {self.category} {self.model}>"


class Platform(Base):
    """平台字典。kind 区分全新/二手，factor 为相对基准价的价格系数。"""

    __tablename__ = "platforms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True)
    name: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(8), default="new")  # new | used
    factor: Mapped[float] = mapped_column(Float, default=1.0)
    color: Mapped[str] = mapped_column(String(16), default="#378ADD")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    listings: Mapped[list["Listing"]] = relationship(
        back_populates="platform", cascade="all, delete-orphan"
    )
    dailies: Mapped[list["PriceDaily"]] = relationship(
        back_populates="platform", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Platform {self.code}>"


class Listing(Base):
    """一条采集到的商品报价明细。"""

    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    platform_id: Mapped[int] = mapped_column(
        ForeignKey("platforms.id", ondelete="CASCADE"), index=True
    )
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    # 采集批次标识（如 "20260916T1841"）。
    # 为什么需要：幂等重采原本按「日期 × 平台」删除旧明细，导致**同一天第二次采集
    # 会覆盖第一次** —— 于是日内价格变化无法计算，日报的「涨跌幅」永远为空。
    # 加上批次后，同一天多次采集可以共存，涨跌幅才有比较基准。
    batch: Mapped[str] = mapped_column(String(16), default="", index=True)
    price: Mapped[float] = mapped_column(Float)
    title_raw: Mapped[str] = mapped_column(Text, default="")
    condition: Mapped[str] = mapped_column(String(16), default="全新")
    url: Mapped[str] = mapped_column(Text, default="")
    seller: Mapped[str] = mapped_column(String(64), default="")
    # 报价质量标记（逗号分隔）：bundle / defective —— 见 collectors/quality.py。
    # 「价格不能代表这个型号行情」的条目（多商品捆绑列表、坏卡）在此留痕，
    # **不静默丢弃**：统计侧可据此排除，排障时可原样查回来。
    quality_flags: Mapped[str] = mapped_column(String(32), default="")
    # 数据血缘：True = 模拟生成，False = 真实采集。
    # 为什么必须有：真实平台（jd/pdd/xianyu）在接入真实适配器之前，
    # 历史数据是 mock 写进同名平台的 —— 只按平台名过滤无法区分真假，
    # 会导致「史低价」「最低平台」被模拟值污染。
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    product: Mapped[Product] = relationship(back_populates="listings")
    platform: Mapped[Platform] = relationship(back_populates="listings")

    __table_args__ = (
        Index("ix_listings_product_date", "product_id", "trade_date"),
        Index("ix_listings_platform_date", "platform_id", "trade_date"),
    )


class PriceDaily(Base):
    """型号 × 平台 × 日期 的价格聚合结果。"""

    __tablename__ = "price_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    platform_id: Mapped[int] = mapped_column(
        ForeignKey("platforms.id", ondelete="CASCADE"), index=True
    )
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    min_price: Mapped[float] = mapped_column(Float)
    # 稳健底价（25 分位）—— 见 aggregate.py 的「为什么要 p25」。
    # 保留 min_price 是原始量（"最低多少钱能买到"），p25 专供**涨跌幅/分位**等
    # 对离群值敏感的统计量。默认 0 仅为兼容旧行，聚合会立刻覆盖。
    p25_price: Mapped[float] = mapped_column(Float, default=0.0)
    max_price: Mapped[float] = mapped_column(Float)
    avg_price: Mapped[float] = mapped_column(Float)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)

    product: Mapped[Product] = relationship(back_populates="dailies")
    platform: Mapped[Platform] = relationship(back_populates="dailies")

    __table_args__ = (
        UniqueConstraint(
            "product_id", "platform_id", "trade_date", name="uq_daily_product_platform_date"
        ),
    )


class ListingSnapshot(Base):
    """一条**具体挂牌**在某一天的价格快照。

    ⚠️ 与 `Listing` 的粒度区别（这是本表存在的全部理由）：

        Listing          行 = (型号, 平台, 采集批次)  —— "某型号今天有哪些报价"
        ListingSnapshot  行 = (平台, 挂牌 item_id, 日期) —— "这条商品我第几次见到"

    有了它才算得出：
      · 这条挂牌**第一次见到**是哪天（= 新上架，捡漏的黄金窗口）
      · 它**降过价没有**（相对自己上一次被看到的价格）
      · 我见过它几次（挂了很久还卖不掉 = 可能可以砍价）

    为什么不能用 `listings` 表代替：那里每次采集都会插入新行、且按批次清理，
    没有"同一商品跨天追踪"的身份（`url` 是商品页链接，但商品身份需要
    `item_id`/`ware_id` 这个**平台原生 id**，而 listings 不存它）。

    目前只有闲鱼（`item_id`）与京东（`ware_id`）能提供商品级身份；
    拼多多还没有，所以那张表暂时只覆盖两个平台 —— 这不是缺陷，是如实反映
    能力边界（拼多多的搜索响应是加密的，拿不到 goods_id 级身份）。
    """

    __tablename__ = "listing_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform_id: Mapped[int] = mapped_column(
        ForeignKey("platforms.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    # 平台原生的商品 id（闲鱼 item_id / 京东 wareId）。**去重与跨天追踪的身份**。
    item_id: Mapped[str] = mapped_column(String(64), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    batch: Mapped[str] = mapped_column(String(16), default="")
    price: Mapped[float] = mapped_column(Float)
    # 原价（划掉的那个）—— 有它才算得出"挂牌自己标称降了多少"。
    # 注意与 `price` 的区别：这是**卖家自己标**的原价，不是我们的历史价。
    ori_price: Mapped[float] = mapped_column(Float, nullable=True)
    title_raw: Mapped[str] = mapped_column(Text, default="")
    seller: Mapped[str] = mapped_column(String(128), default="")
    area: Mapped[str] = mapped_column(String(64), default="")
    condition: Mapped[str] = mapped_column(String(16), default="")
    url: Mapped[str] = mapped_column(Text, default="")
    # 接口给的发布时间（已归一成 ISO）。它是"新上架"判定的**平台口径**，
    # 与"我们第一次见到它"（first_date，我们的口径）是两件事，都要留。
    publish_time: Mapped[str] = mapped_column(String(32), default="")
    want_num: Mapped[int] = mapped_column(Integer, default=0)
    tags: Mapped[str] = mapped_column(String(128), default="")
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    __table_args__ = (
        # 同一天同一挂牌只留一条 —— 重跑/多批次走 upsert 覆盖，而不是堆行
        UniqueConstraint(
            "platform_id", "item_id", "trade_date",
            name="uq_snapshot_platform_item_date",
        ),
        Index("ix_snapshot_item_date", "platform_id", "item_id", "trade_date"),
        Index("ix_snapshot_product_date", "product_id", "trade_date"),
    )


class CrawlLog(Base):
    """采集任务执行日志。"""

    __tablename__ = "crawl_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    trigger: Mapped[str] = mapped_column(String(16), default="manual")  # manual|schedule|backfill
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|success|failed|partial
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    items: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")

    @property
    def duration_sec(self) -> float | None:
        if self.finished_at is None:
            return None
        return round((self.finished_at - self.started_at).total_seconds(), 2)


class Build(Base):
    """一套装机配置单。

    这是**用户数据**，不是行情数据 —— 所以和 listings/price_daily 分开存：
    配置单跟着用户走（他存了几套），价格每小时变。混在一起会让
    "清行情缓存" 和 "改配置" 互相牵连。

    总价不落库。价格是算出来的（每次现算三平台最低价），
    落库会立刻过期 —— 存一个"昨天的总价"比不存更糟。
    """

    __tablename__ = "builds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    items: Mapped[list["BuildItem"]] = relationship(
        back_populates="build",
        cascade="all, delete-orphan",
        order_by="BuildItem.id",
    )


class BuildItem(Base):
    """配置单里的一件配件。"""

    __tablename__ = "build_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    build_id: Mapped[int] = mapped_column(
        ForeignKey("builds.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    # 备注：自用场景很有用 —— "二手也行" / "等 618" / "已有，不用买"
    note: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    build: Mapped[Build] = relationship(back_populates="items")
    product: Mapped[Product] = relationship()
