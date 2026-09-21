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
