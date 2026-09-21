"""主数据初始化：把平台字典与型号字典写入数据库（幂等）。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Platform, Product
from ..seed_data import platform_rows, product_rows


def ensure_platforms(session: Session) -> int:
    existing = {c for (c,) in session.execute(select(Platform.code)).all()}
    added = 0
    for row in platform_rows():
        if row["code"] in existing:
            continue
        session.add(Platform(**row))
        added += 1
    return added


def ensure_products(session: Session) -> tuple[int, int]:
    """确保型号完整：补齐缺失的，并同步别名规则的变更。

    返回 (新增数, 别名更新数)。别名必须支持刷新 —— 归一化规则升级
    （例如新增"去掉容量后缀"的短名）之后，老型号若不刷新就会一直匹配不上，
    表现为"规则明明存在却命中不了"。
    """
    existing = {p.model: p for p in session.execute(select(Product)).scalars()}
    added = updated = 0
    for row in product_rows():
        product = existing.get(row["model"])
        if product is None:
            session.add(Product(**row))
            added += 1
        elif product.aliases != row["aliases"]:
            product.aliases = row["aliases"]
            updated += 1
    return added, updated


def bootstrap(session: Session) -> dict:
    """确保主数据完整，返回本次变更数量。"""
    products_added, products_updated = ensure_products(session)
    return {
        "platforms_added": ensure_platforms(session),
        "products_added": products_added,
        "products_updated": products_updated,
    }
