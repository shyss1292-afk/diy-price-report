#!/usr/bin/env python
"""并发写入验证：确认 `database is locked` 不再复现。

背景
----
2026-09-21 15:00 的定时轮次在写 `crawl_log` 时抛
`sqlite3.OperationalError: database is locked`，**以退出码 1 中断整轮**。
根因有两条，缺一不可：

  1. `busy_timeout = 0`（SQLite 默认）—— 拿不到写锁就立刻抛异常，不重试。
  2. 写事务横跨整个浏览器采集过程（9~10 分钟）—— 期间写锁被独占。

修复后这里做三层验证：

  A. **PRAGMA 生效性**：每条连接都必须真的带上 WAL / busy_timeout / NORMAL。
     （只改代码不验证 = 不知道设上没设上。）
  B. **DB 层争用**：一个连接握着写事务，另一个连接并发写 ——
     必须**等待并成功**，而不是立刻抛错。
     附**反向验证**：把 busy_timeout 归零，同样的场景必须复现失败 ——
     否则说明这个用例根本测不出问题（永远绿的摆设）。
  C. **真实采集期间并发写**（`--live`）：复现 15:00 那个场景 ——
     采集进程在跑的同时，另一个连接持续写入。要求 **0 次** locked。

用法
----
    python -m scripts.concurrency_check            # 只跑 A + B（秒级，不联网）
    python -m scripts.concurrency_check --live     # 加上 C（会真跑一轮采集）
"""
from __future__ import annotations

import argparse
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

PROJ = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from app.config import DB_PATH  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ✅ {name}" + (f" — {detail}" if detail else ""))
    else:
        _FAIL += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))


# ------------------------------------------------------------------ A

def test_pragmas() -> None:
    print("\n[A] PRAGMA 生效性（每条新连接都要带）")
    from app.db import busy_timeout_ms, connection_pragmas

    p = connection_pragmas()
    check("journal_mode = wal（读写互不阻塞）", str(p["journal_mode"]).lower() == "wal", str(p))
    check("busy_timeout = 配置值（拿不到锁会等，不会立刻抛）",
          p["busy_timeout"] == busy_timeout_ms(), f"实际 {p['busy_timeout']}ms")
    check("busy_timeout ≥ 1000ms", p["busy_timeout"] >= 1000, f"{p['busy_timeout']}ms")
    check("synchronous = NORMAL(1)（配合 WAL 提写吞吐）", p["synchronous"] == 1)
    check("foreign_keys 仍然开启（没被新配置挤掉）", p["foreign_keys"] == 1)

    # 池里**新开**的连接也必须带上 —— PRAGMA 是连接级的，漏挂事件就会退化
    from app.db import engine

    with engine.connect() as c1, engine.connect() as c2:
        v1 = c1.exec_driver_sql("PRAGMA busy_timeout").scalar()
        v2 = c2.exec_driver_sql("PRAGMA busy_timeout").scalar()
    check("同一池里多条连接都带 busy_timeout", v1 == v2 == busy_timeout_ms(), f"{v1} / {v2}")


# ------------------------------------------------------------------ B

def _hold_write_lock(db: pathlib.Path, seconds: float, ready: threading.Event) -> None:
    """在一个连接上握写锁 hold 指定秒数。"""
    conn = sqlite3.connect(db, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("CREATE TABLE IF NOT EXISTS t(id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("BEGIN IMMEDIATE")          # 立刻抢写锁
    conn.execute("INSERT INTO t(v) VALUES ('holder')")
    ready.set()                              # 通知对方：锁已在我手里
    time.sleep(seconds)
    conn.commit()
    conn.close()


def _try_write(db: pathlib.Path, busy_timeout_ms: int) -> tuple[bool, float]:
    """另一个连接尝试写入，返回 (是否成功, 耗时)。"""
    conn = sqlite3.connect(db, timeout=0.001)   # pysqlite 侧也几乎不等
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    conn.execute("CREATE TABLE IF NOT EXISTS t(id INTEGER PRIMARY KEY, v TEXT)")
    started = time.monotonic()
    try:
        conn.execute("INSERT INTO t(v) VALUES ('contender')")
        conn.commit()
        return True, time.monotonic() - started
    except sqlite3.OperationalError:
        return False, time.monotonic() - started
    finally:
        conn.close()


def test_contention() -> None:
    print("\n[B] DB 层争用：一方握写锁，另一方并发写")
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="diyprice_lock_"))
    db = tmp / "t.db"

    # --- B1 反向验证：busy_timeout=0 必须复现失败 ---
    ready = threading.Event()
    holder = threading.Thread(target=_hold_write_lock, args=(db, 1.5, ready), daemon=True)
    holder.start()
    ready.wait(timeout=3)
    ok0, el0 = _try_write(db, busy_timeout_ms=0)
    check("反向验证：busy_timeout=0 时**确实会**立刻抛 locked（用例有效）",
          ok0 is False and el0 < 0.5, f"成功={ok0} 耗时={el0:.3f}s")
    holder.join()

    # --- B2 正向：配了 busy_timeout 就必须等到并成功 ---
    ready = threading.Event()
    holder = threading.Thread(target=_hold_write_lock, args=(db, 1.5, ready), daemon=True)
    holder.start()
    ready.wait(timeout=3)
    ok1, el1 = _try_write(db, busy_timeout_ms=5000)
    check("配了 busy_timeout=5000 后，并发写**等待并成功**（不再抛 locked）",
          ok1 is True, f"成功={ok1} 耗时={el1:.2f}s")
    check("等待时长合理（真的等了，不是碰巧）", el1 >= 1.0, f"{el1:.2f}s")
    holder.join()

    # --- B3 超过 busy_timeout 仍应失败（不是无限等） ---
    ready = threading.Event()
    holder = threading.Thread(target=_hold_write_lock, args=(db, 4.0, ready), daemon=True)
    holder.start()
    ready.wait(timeout=3)
    ok2, el2 = _try_write(db, busy_timeout_ms=500)
    check("握锁时长 > busy_timeout 时仍然失败（不会无限阻塞）",
          ok2 is False and el2 < 2.0, f"成功={ok2} 耗时={el2:.2f}s")
    holder.join()

    # --- B4 读操作不被写事务挡住（WAL 的核心收益）---
    ready = threading.Event()
    holder = threading.Thread(target=_hold_write_lock, args=(db, 1.2, ready), daemon=True)
    holder.start()
    ready.wait(timeout=3)
    conn = sqlite3.connect(db, timeout=0.001)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=0")     # 读连接**故意不设**忙等待
    started = time.monotonic()
    try:
        n = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        read_ok, read_err = True, ""
    except sqlite3.OperationalError as exc:
        n, read_ok, read_err = -1, False, str(exc)
    read_el = time.monotonic() - started
    conn.close()
    holder.join()
    check("WAL 下读操作不被写事务阻塞（busy_timeout=0 也秒回）",
          read_ok and read_el < 0.5, f"读到 {n} 行，耗时 {read_el:.3f}s {read_err}")


# ------------------------------------------------------------------ C

_LOCK_PROBE_TABLE = "_lock_probe"


def _init_probe_table() -> None:
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(f"DROP TABLE IF EXISTS {_LOCK_PROBE_TABLE}")
    conn.execute(f"CREATE TABLE {_LOCK_PROBE_TABLE}(id INTEGER PRIMARY KEY, at TEXT)")
    conn.commit()
    conn.close()


def _drop_probe_table() -> None:
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(f"DROP TABLE IF EXISTS {_LOCK_PROBE_TABLE}")
    conn.commit()
    conn.close()


def test_live_collection(source: str = "xianyu") -> None:
    """复现 15:00 的场景：采集在跑的同时，另一连接持续写入。

    写的是本脚本自建的 `_lock_probe` 表 —— 不污染业务表，跑完就删。
    """
    print(f"\n[C] 真实采集期间并发写（source={source}）")
    _init_probe_table()

    stop = threading.Event()
    stats = {"ok": 0, "locked": 0, "errors": []}

    def hammer() -> None:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        while not stop.is_set():
            try:
                conn.execute(
                    f"INSERT INTO {_LOCK_PROBE_TABLE}(at) VALUES (?)",
                    (time.strftime("%H:%M:%S"),),
                )
                conn.commit()
                stats["ok"] += 1
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    stats["locked"] += 1
                else:
                    stats["errors"].append(str(exc)[:80])
            time.sleep(1.0)
        conn.close()

    import os

    env = dict(os.environ)
    env[f"DIYPRICE_{source.upper()}_LIMIT"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.cli", "collect", "--sources", source],
        cwd=PROJ, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    t = threading.Thread(target=hammer, daemon=True)
    t.start()
    out, _ = proc.communicate()
    stop.set()
    t.join(timeout=5)

    tail = "\n".join(line for line in out.splitlines() if "locked" in line.lower())
    check(f"采集期间并发写入 {stats['ok']} 次，**0 次 database is locked**",
          stats["locked"] == 0, f"locked={stats['locked']} 其它错误={stats['errors'][:2]}")
    check("采集进程本身也没报 locked", not tail, tail[:160] or "无")
    check("采集进程正常退出（退出码 0）", proc.returncode == 0, f"退出码 {proc.returncode}")

    _drop_probe_table()
    print("  （已清理探针表，业务表未受影响）")


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser(description="SQLite 并发写入验证")
    ap.add_argument("--live", action="store_true", help="额外跑真实采集期间的并发写（会真发请求）")
    ap.add_argument("--source", default="xianyu", help="--live 时用哪个源（默认 xianyu）")
    args = ap.parse_args()

    print("=" * 68)
    print("SQLite 并发写入验证（目标：database is locked 不再复现）")
    print("=" * 68)
    test_pragmas()
    test_contention()
    if args.live:
        test_live_collection(args.source)
    else:
        print("\n[C] 已跳过（加 --live 可跑真实采集期间的并发写）")

    print("\n" + "=" * 68)
    print(f"通过 {_PASS} / 共 {_PASS + _FAIL}")
    if _FAIL:
        print(f"❌ 有 {_FAIL} 项未通过")
    else:
        print("✅ 全部通过 —— 并发写不再抛 database is locked")
    print("=" * 68)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
