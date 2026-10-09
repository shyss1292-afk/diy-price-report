"""装机配置单：自用场景的「配置单 + 实时总价」。

价格从哪来（**最关键的口径决定**）
--------------------------------
**只用 `price_daily`**，不从 listings 现算一遍。

理由：`price_daily` 是行情底座，`aggregate.refresh_daily` 聚合时已经过了四道闸门
（`is_synthetic=0` + `quality_flags=''` + `Platform.is_active` + `Product.is_active`）。
而 `coverage.build_platform_latest()` 虽然也标了 `real`，却是**自己从 listings 聚合**的，
**没有 `quality_flags` 那道闸门** —— 涡轮工包卡、坏卡、捆绑列表照样能进"最低价"。

自用装机助手最怕的正是这个：总价里混进一条 ¥9800 的工业涡轮卡，
整套配置的报价就没有参考意义了。所以这里复用已经洗干净的底座，不另开一条取价路径。

不落库的东西
------------
**总价不存**。价格每小时在变，存一个"昨天的总价"比不存更糟 ——
每次现算，且明确带上"这个价是哪天的"。
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Build, BuildItem, Platform, PriceDaily, Product

DEFAULT_DAYS = 180
MAX_BUILDS = 30
MAX_ITEMS = 40


# ------------------------------------------------------------------ 取价

def _price_cells(
    session: Session, product_ids: list[int], days: int = DEFAULT_DAYS
) -> dict[int, dict]:
    """每个型号在各**启用平台**上的最新一天价格。

    返回 ``{product_id: {platform_code: {price, date, kind, name, samples}}}``。

    「最新一天」是**逐平台**取的，不是"全表最新那天" —— 整点分批轮巡下，
    京东和闲鱼当天可能在不同的日期有数据（京东 2 个/轮，进度落后很多）。
    按全表最新日期取会让京东的格子大面积空掉。
    """
    if not product_ids:
        return {}
    since = date.today() - timedelta(days=days)

    rn = (
        func.row_number()
        .over(
            partition_by=(PriceDaily.product_id, PriceDaily.platform_id),
            order_by=PriceDaily.trade_date.desc(),
        )
        .label("rn")
    )
    ranked = (
        select(
            PriceDaily.product_id.label("pid"),
            PriceDaily.platform_id.label("plid"),
            PriceDaily.trade_date.label("d"),
            PriceDaily.min_price.label("low"),
            PriceDaily.p25_price.label("p25"),
            PriceDaily.sample_count.label("n"),
            rn,
        )
        .where(PriceDaily.product_id.in_(product_ids), PriceDaily.trade_date >= since)
        .subquery()
    )
    rows = session.execute(
        select(
            ranked.c.pid,
            Platform.code,
            Platform.name,
            Platform.kind,
            ranked.c.d,
            ranked.c.low,
            ranked.c.p25,
            ranked.c.n,
        )
        .join(Platform, Platform.id == ranked.c.plid)
        .where(ranked.c.rn == 1, Platform.is_active.is_(True))
    ).all()

    out: dict[int, dict] = {}
    for pid, code, name, kind, d, low, p25, n in rows:
        if low is None and p25 is None:
            continue
        low_f = float(low) if low is not None else None
        # `p25_price` 是后加的列：迁移刚补上、聚合还没重算时它是 0。
        # 0 会让"稳健价"变成 ¥0（比坏数据更糟），所以只认正数，否则退回 min。
        p25_f = float(p25) if p25 and float(p25) > 0 else None
        out.setdefault(pid, {})[code] = {
            # ⚠️ **`price` 用稳健价（25 分位），不是最低挂牌**。
            #
            # 为什么必须这样（2026-10-04 实测）：RTX 5060 Ti 16G 在闲鱼
            # 10-03 的最低价是 ¥2300，而当天真实行情约 ¥4400 —— 因为那条
            # 标题是「技嘉 **RX6800** 超级雕 16G …**换了 5060Ti 故出**」，
            # 卖的其实是 RX 6800，只因为句子里提到"5060Ti"就被归到这个型号名下。
            #
            # 这类"提到但不是在卖"的错配**词表抓不到**（它既不是坏卡也不是捆绑），
            # 是型号归属的固有难题。用单条最低价算总价，整套配置就会凭空便宜
            # 两千块 —— 对装机助手来说这是致命的（总价没了参考意义）。
            # 25 分位天然把这种"最低的一小撮"排除在外。
            "price": round(p25_f if p25_f is not None else low_f, 2),
            # 真实最低挂牌价，**仅作参考展示** —— 它可能真能捡到，
            # 也可能是错配/引流。界面用「另有更低」小字提示，不参与总价。
            "low": round(low_f, 2) if low_f is not None else None,
            "date": d.isoformat() if d else None,
            "kind": kind,
            "name": name,
            "samples": int(n or 0),
        }
    return out


def _best_of(cells: dict) -> dict | None:
    """一组平台格里最便宜的那个。"""
    if not cells:
        return None
    code, cell = min(cells.items(), key=lambda kv: kv[1]["price"])
    return {"code": code, **cell}


# ------------------------------------------------------------------ 序列化

def serialize(session: Session, build: Build, days: int = DEFAULT_DAYS) -> dict:
    """把一套配置单渲染成"带实时价"的结构。

    含三层信息，缺一层自用场景就不够用：
      · 每件配件的三平台价（知道**去哪买**）
      · 每件的最低价（知道**大概多少钱**）
      · 总价 + 「几件缺价」（知道**这个总价可不可信**）
    """
    items = list(build.items)
    pids = [i.product_id for i in items]
    cells = _price_cells(session, pids, days=days)
    products = {
        p.id: p
        for p in session.execute(select(Product).where(Product.id.in_(pids or [0]))).scalars()
    }

    out_items: list[dict] = []
    total = 0.0
    priced = 0
    for it in items:
        product = products.get(it.product_id)
        row_cells = cells.get(it.product_id, {})
        best = _best_of(row_cells)
        qty = max(1, int(it.quantity or 1))
        line_total = round(best["price"] * qty, 2) if best else None
        if best is not None:
            total += line_total
            priced += 1
        out_items.append(
            {
                "item_id": it.id,
                "product_id": it.product_id,
                "model": product.model if product else f"（型号 {it.product_id} 已移除）",
                "brand": product.brand if product else "",
                "spec": product.spec if product else "",
                "category": product.category if product else "",
                "quantity": qty,
                "note": it.note or "",
                "platforms": row_cells,
                "best": best,
                "line_total": line_total,
            }
        )

    # 按品类分组排序：CPU → 主板 → 显卡 …（和装机思路一致，不是按添加顺序）
    order = {"cpu": 0, "mb": 1, "gpu": 2, "ram": 3, "ssd": 4, "psu": 5, "cooler": 6, "case": 7}
    out_items.sort(key=lambda r: (order.get(r["category"], 99), r["item_id"]))

    return {
        "id": build.id,
        "name": build.name,
        "note": build.note or "",
        "created_at": build.created_at.strftime("%Y-%m-%d %H:%M") if build.created_at else "",
        "items": out_items,
        "item_count": len(out_items),
        "priced_count": priced,
        # 「缺价件数」必须单独透出：否则一件没价的配件会让总价悄悄偏低，
        # 用户以为整套只要 X 元，其实还有一件没算进去。
        "missing_count": len(out_items) - priced,
        "total": round(total, 2) if priced else None,
        "total_complete": priced == len(out_items) and bool(out_items),
    }


def list_builds(session: Session, days: int = DEFAULT_DAYS) -> list[dict]:
    builds = session.execute(
        select(Build).order_by(Build.sort_order, Build.id)
    ).scalars().all()
    return [serialize(session, b, days=days) for b in builds]


# ------------------------------------------------------------------ 写操作

def create_build(session: Session, name: str, note: str = "") -> Build:
    count = session.execute(select(func.count()).select_from(Build)).scalar() or 0
    if count >= MAX_BUILDS:
        raise ValueError(f"配置单最多 {MAX_BUILDS} 套")
    build = Build(name=(name or "").strip()[:64] or "未命名配置", note=(note or "").strip()[:200])
    session.add(build)
    session.flush()
    return build


def add_item(
    session: Session, build: Build, product_id: int, quantity: int = 1, note: str = ""
) -> BuildItem:
    if session.get(Product, product_id) is None:
        raise ValueError("型号不存在")
    existing = session.execute(
        select(func.count()).select_from(BuildItem).where(BuildItem.build_id == build.id)
    ).scalar() or 0
    if existing >= MAX_ITEMS:
        raise ValueError(f"单套配置最多 {MAX_ITEMS} 件")
    item = BuildItem(
        build_id=build.id,
        product_id=product_id,
        quantity=max(1, min(99, int(quantity or 1))),
        note=(note or "").strip()[:120],
    )
    session.add(item)
    session.flush()
    return item
