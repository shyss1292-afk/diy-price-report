"""回填 `listings.quality_flags` —— 对已有明细重跑质量判定。

为什么需要单独的回填
--------------------
`quality_flags` 是在**采集写入时**算的（见 `services/pipeline.py`），
所以规则升级**不会自动作用到历史数据**。2026-10-03 新增 `industrial`
（涡轮 / 工包 / 算力卡）时，库里那 2.5 万条真实明细的 flags 还是旧的二值 ——
不重跑，新规则就只对"以后"的采集生效，而用户看到的
「RTX 4080 Super 16G 一天涨 44%」正是**历史数据**造成的。

用法
----
    python -m scripts.backfill_quality            # 只统计，不写
    python -m scripts.backfill_quality --apply    # 写回

幂等：重复执行结果相同（是"按标题重算并覆盖"，不是"追加标记"）。
"""
from __future__ import annotations

import sys
from collections import Counter

from sqlalchemy import func, select, text

from app.collectors import quality
from app.db import SessionLocal
from app.models import Listing

_CHUNK = 500


def main() -> int:
    apply = "--apply" in sys.argv

    with SessionLocal() as session:
        rows = session.execute(
            select(Listing.id, Listing.quality_flags, Listing.title_raw).where(
                Listing.is_synthetic.is_(False)
            )
        ).all()

        before: Counter = Counter()
        after: Counter = Counter()
        updates: list[dict] = []
        for lid, old, title in rows:
            old_s = (old or "").strip()
            new = ",".join(quality.classify(title))
            before[old_s or "(空)"] += 1
            after[new or "(空)"] += 1
            if new != old_s:
                updates.append({"lid": lid, "flags": new})

        print(f"真实明细 {len(rows)} 条，需更新 {len(updates)} 条")
        print("\n-- 更新前 --")
        for k, v in before.most_common(12):
            print(f"  {k:34} {v}")
        print("-- 更新后 --")
        for k, v in after.most_common(12):
            print(f"  {k:34} {v}")

        if not apply:
            print("\n（未写入。确认无误后加 --apply 落盘）")
            return 0

        # 用**原生 SQL**（`text()`）批改，不用 SQLAlchemy 的 bulk update。
        #
        # 这里连着踩了三个坑，全记下来省得下次再踩：
        #   ① ORM `update().where(额外条件)` 默认同步策略直接抛 InvalidRequestError
        #      → 需 `synchronize_session=None`
        #   ② 加了 None 之后又被判成 "per-row bulk UPDATE by primary key"，
        #      报 "No primary key value supplied for column(s) listings.id"
        #   ③ 把参数名改成 `id` 去迎合它，又撞上
        #      "bindparam() name 'id' is reserved for automatic usage in VALUES/SET"
        # 根子是 ORM 2.0 给 bulk UPDATE 铺了好几条特殊路径，靠**参数名**猜意图。
        # 而这里只是"按主键批改一个文本列"，没有任何会话内对象需要同步 ——
        # 原生 SQL 语义最直白，也没有被猜错的空间。
        #
        # ⚠️ 教训：脚本打印的统计是**内存里的，不代表已落盘**。第一次跑 `--apply`
        # 我在外围加了 `| tail`，异常被截断，只看到"更新后 industrial 256"，
        # 差点当成写成功了 —— 回读查库才发现 0 条。所以下面**强制回读校验**。
        stmt = text("UPDATE listings SET quality_flags = :flags WHERE id = :lid")
        for i in range(0, len(updates), _CHUNK):
            session.execute(stmt, updates[i : i + _CHUNK])
        session.commit()

        # 回读校验 —— 不信 rowcount，只信库里真的变了
        left = session.execute(
            select(func.count())
            .select_from(Listing)
            .where(Listing.is_synthetic.is_(False), Listing.quality_flags == "")
        ).scalar()
        marked = session.execute(
            select(func.count())
            .select_from(Listing)
            .where(Listing.is_synthetic.is_(False), Listing.quality_flags != "")
        ).scalar()
        print(f"\n已写入 {written} 条；回读：有标记 {marked} 条 / 无标记 {left} 条")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
