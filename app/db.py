"""数据库引擎与会话管理（SQLite + WAL + 忙等待）。

并发写入的坑（2026-09-21 真实故障）
-----------------------------------
定时采集轮次报过 `sqlite3.OperationalError: database is locked` 并**以退出码 1
中断整轮**（写入 `crawl_log` 时）。SQLite 只允许一个写者，而默认
`busy_timeout = 0` —— 拿不到写锁就**立刻抛异常**，不重试。

所以这里做了两件事，缺一不可：

  1. **忙等待**（本文件）：`busy_timeout=5000` + pysqlite 的 `timeout`，
     锁被占用时最多等 5 秒重试，而不是立即失败。
  2. **收窄写事务**（`services/pipeline.py`）：不要把写事务横跨整个浏览器
     采集过程（9~10 分钟）。忙等待只能扛住"短促争用"，扛不住"对方握着
     写锁十分钟"。

只做第 1 件是不够的 —— 5 秒等不来一个 10 分钟的长事务。
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import DATABASE_URL, DB_PATH

DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# 拿不到写锁时最多等多久（毫秒）。默认 5000ms —— 比一次常规写入长得多，
# 又远短于一轮采集，所以"短暂争用能等到、长事务等不到"。
DEFAULT_BUSY_TIMEOUT_MS = 5000


def busy_timeout_ms() -> int:
    """忙等待上限，可用 `DIYPRICE_SQLITE_BUSY_TIMEOUT`（毫秒）覆盖。"""
    try:
        return int(os.getenv("DIYPRICE_SQLITE_BUSY_TIMEOUT", "") or DEFAULT_BUSY_TIMEOUT_MS)
    except ValueError:
        return DEFAULT_BUSY_TIMEOUT_MS


engine = create_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    connect_args={
        "check_same_thread": False,
        # pysqlite 自己的忙等待（秒）。必须设：下面第一条 PRAGMA
        # （journal_mode=WAL）本身就要抢锁，没有它连"开启 WAL"都可能直接失败。
        "timeout": busy_timeout_ms() / 1000.0,
    },
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _record) -> None:
    """每条新连接都要设的 PRAGMA。

    ⚠️ 必须挂在 `connect` 事件上而不是只做一次 —— PRAGMA 是**连接级**的
    （`journal_mode` 会写进库文件，但 `busy_timeout` / `foreign_keys` 不会）。
    池里每开一条新连接都得重设，否则新连接又退回"立即抛异常"。
    """
    cur = dbapi_conn.cursor()
    # 预写日志：读写互不阻塞（读不再被写事务挡住）
    cur.execute("PRAGMA journal_mode=WAL")
    # 拿不到写锁就等，而不是立刻抛 database is locked
    cur.execute(f"PRAGMA busy_timeout={busy_timeout_ms()}")
    # 配合 WAL 提升写吞吐；WAL 下 NORMAL 不会丢库（最多丢最后一个事务）
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


SessionLocal = sessionmaker(
    bind=engine, autoflush=False, expire_on_commit=False, future=True
)


def connection_pragmas() -> dict:
    """当前连接实际生效的 PRAGMA（自检/排障用，别只看代码以为设上了）。"""
    with engine.connect() as conn:
        return {
            "journal_mode": conn.exec_driver_sql("PRAGMA journal_mode").scalar(),
            "busy_timeout": conn.exec_driver_sql("PRAGMA busy_timeout").scalar(),
            "synchronous": conn.exec_driver_sql("PRAGMA synchronous").scalar(),
            "foreign_keys": conn.exec_driver_sql("PRAGMA foreign_keys").scalar(),
        }


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务性会话上下文，异常自动回滚。

    ⚠️ **不要用它包住慢操作**（浏览器采集、网络请求、sleep）。

    SQLite 的写锁从第一次写一直持有到 COMMIT —— 一旦把它横跨一轮采集
    （9~10 分钟），任何其它写者都只能干等，`busy_timeout` 也救不了
    （5 秒等不来 10 分钟）。这就是 2026-09-21 那次 `database is locked`
    崩溃的成因。

    正确用法：**快进快出**。要采集，就先把需要的数据读出来、关掉会话，
    采完再开一个新会话说写入（见 `services/pipeline.run_pipeline`）。
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖注入用的会话生成器。"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def init_db() -> None:
    """建表（幂等）+ 轻量迁移。"""
    from . import models  # noqa: F401  确保模型已注册到 metadata

    Base.metadata.create_all(engine)
    _migrate()


def _migrate() -> None:
    """为已有库补上后新增的列。

    SQLite 支持 ADD COLUMN（带默认值），不需要重建表。
    每次加字段时在这里登记一次即可，重复执行是安全的。
    """
    additions = [
        ("listings", "is_synthetic", "BOOLEAN NOT NULL DEFAULT 0"),
        ("listings", "batch", "VARCHAR(16) NOT NULL DEFAULT ''"),
        ("listings", "quality_flags", "VARCHAR(32) NOT NULL DEFAULT ''"),
    ]
    with engine.begin() as conn:
        for table, column, ddl in additions:
            existing = {
                row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            }
            if existing and column not in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
