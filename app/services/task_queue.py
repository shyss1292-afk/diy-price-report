"""采集任务队列：持久化 + 崩溃恢复 + 断点续爬。

为什么需要它
-----------
改造前，采集进度只活在两个地方：

    游标文件   data/collect_cursor.json   —— 下一轮从哪开始（已落盘）
    本轮结果   内存里的 quotes 列表        —— 已经采到的东西（**不落盘**）

浏览器崩溃、进程被 kill、或者超时被看门狗掐掉时，内存里的 quotes 全部
消失，而游标**已经前进过了** —— 那批型号就被静默跳过，要等游标转一整圈
（88 个型号 ÷ 每轮 2~15 个）才轮到，代价是几小时到半天。

队列解决的正是这件事：

    · 任务粒度  一个 (源, 型号, 日期) 三元组
    · 状态落盘  data/collect_queue.json
    · 结果落盘  data/queue_results/<source>-<day>.jsonl（**增量追加**）
    · 崩溃恢复  进程启动时把 running 回滚为 pending，接着做完剩下的

配合 browser_worker 的短生命周期模型：浏览器被回收重启时，队列里已完成
的任务不会重采，未完成的会被新实例接着做。

关于「采不到就算完」
------------------
有些型号在某平台确实无货（京东尤其多）。这类型号不会永远卡在 pending：
`mark_empty()` 会记录连续空结果次数，超过阈值即判定为「该平台无此型号」
并结案 —— 否则队列会被永远采不到的型号堵死。

关于「路由排除」
--------------
另一类不该留在 pending 的是**按采集源生命周期路由根本不该采**的任务：
停产老硬件（GTX 10/16 系、RTX 20/30 系、RX 5000/6000 系、12 代及更早的
Intel、5000 系及更早的 AMD）在京东/拼多多已无正品新货，只走闲鱼。
路由生效**之前**入队的这类任务由 `retire_routed()` 标成 `routed` 终态结案。
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

from ..config import DATA_DIR

logger = logging.getLogger("diyprice.queue")

QUEUE_FILE = DATA_DIR / "collect_queue.json"
RESULTS_DIR = DATA_DIR / "queue_results"

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_EMPTY = "empty"      # 反复采不到，判定该平台无此型号
STATUS_FAILED = "failed"
# 按采集源生命周期路由**根本不该采**此源（legacy 型号不走 jd/pdd），终态。
# 与 `empty` 的区别：`empty` 是"采了但平台没货"（平台侧事实），
# `routed` 是"按我们的规则不该采"（策略侧决定）—— 分开记，
# 看队列时才能分辨到底是平台没货还是路由配错。
STATUS_ROUTED = "routed"

# 连续这么多次空结果就结案（与京东采集器的 max_empty_streak 同量级）
MAX_EMPTY_ATTEMPTS = 4

VERSION = 1


@dataclass
class Task:
    """一个采集任务 = 采某型号在某平台的当日价格。"""

    task_id: str
    source: str
    product_id: int
    model: str
    day: str
    status: str = STATUS_PENDING
    attempts: int = 0
    empty_streak: int = 0
    quotes: int = 0
    updated_at: str = ""

    def touch(self) -> None:
        self.updated_at = datetime.now().isoformat(timespec="seconds")


def make_task_id(source: str, product_id: int, day: str) -> str:
    return f"{source}:{product_id}:{day}"


# ------------------------------------------------------------------ 底层读写

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _atomic_write(path: Path, text: str) -> None:
    """原子写：先写临时文件再 rename。

    队列文件是崩溃恢复的唯一依据 —— 如果写一半被 kill，文件会损坏，
    整个待办列表就丢了。rename 在同一文件系统上是原子的。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _load_raw() -> dict:
    if not QUEUE_FILE.exists():
        return {"version": VERSION, "tasks": {}}
    try:
        data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("队列文件损坏（%s），已重置", str(exc)[:80])
        return {"version": VERSION, "tasks": {}}
    if not isinstance(data, dict) or "tasks" not in data:
        return {"version": VERSION, "tasks": {}}
    return data


def _save_raw(data: dict) -> None:
    data["version"] = VERSION
    data["saved_at"] = _now()
    _atomic_write(QUEUE_FILE, json.dumps(data, ensure_ascii=False, indent=1))


def _to_task(tid: str, raw: dict) -> Task:
    return Task(
        task_id=tid,
        source=raw.get("source", ""),
        product_id=int(raw.get("product_id", 0)),
        model=raw.get("model", ""),
        day=raw.get("day", ""),
        status=raw.get("status", STATUS_PENDING),
        attempts=int(raw.get("attempts", 0)),
        empty_streak=int(raw.get("empty_streak", 0)),
        quotes=int(raw.get("quotes", 0)),
        updated_at=raw.get("updated_at", ""),
    )


# ------------------------------------------------------------------ 入队

def enqueue(source: str, pairs: list[tuple[int, str]], day: date) -> int:
    """把一批 (product_id, model) 加入队列。

    已存在的任务不会重置状态 —— 已经 done 的不重采，未完成的保留原状态
    （这就是断点续爬能生效的原因）。

    Returns:
        新建的任务数（已存在的不计）。
    """
    day_s = day.isoformat() if isinstance(day, date) else str(day)
    data = _load_raw()
    tasks: dict = data["tasks"]

    created = 0
    for pid, model in pairs:
        tid = make_task_id(source, pid, day_s)
        if tid in tasks:
            # 只补齐可能缺失的展示字段，不动 status
            if model and not tasks[tid].get("model"):
                tasks[tid]["model"] = model
            continue
        task = Task(
            task_id=tid, source=source, product_id=pid, model=model, day=day_s,
        )
        task.touch()
        tasks[tid] = asdict(task)
        created += 1

    if created:
        _save_raw(data)
    return created


def pending(source: str | None = None, day: date | None = None) -> list[Task]:
    """取待办任务（按入队顺序）。"""
    day_s = day.isoformat() if isinstance(day, date) else day
    data = _load_raw()
    out: list[Task] = []
    for tid, raw in data["tasks"].items():
        if raw.get("status") != STATUS_PENDING:
            continue
        task = _to_task(tid, raw)
        if source and task.source != source:
            continue
        if day_s and task.day != day_s:
            continue
        out.append(task)
    return out


# ------------------------------------------------------------------ 状态流转

def _update(task_id: str, **fields) -> None:
    data = _load_raw()
    raw = data["tasks"].get(task_id)
    if raw is None:
        return
    raw.update(fields)
    raw["updated_at"] = _now()
    _save_raw(data)


def mark_running(task_id: str) -> None:
    data = _load_raw()
    raw = data["tasks"].get(task_id)
    if raw is None:
        return
    raw["status"] = STATUS_RUNNING
    raw["attempts"] = int(raw.get("attempts", 0)) + 1
    raw["updated_at"] = _now()
    _save_raw(data)


def mark_done(task_id: str, quotes: int) -> None:
    _update(task_id, status=STATUS_DONE, quotes=int(quotes), empty_streak=0)


def mark_failed(task_id: str, reason: str = "") -> None:
    data = _load_raw()
    raw = data["tasks"].get(task_id)
    if raw is None:
        return
    streak = int(raw.get("empty_streak", 0)) + 1
    # 连续空结果到阈值 → 判定该平台确实没有这个型号，结案，别堵住队列
    status = STATUS_EMPTY if streak >= MAX_EMPTY_ATTEMPTS else STATUS_PENDING
    raw.update({"status": status, "empty_streak": streak, "updated_at": _now()})
    if reason:
        raw["last_reason"] = reason[:120]
    _save_raw(data)


def retire_routed(task_id: str, reason: str = "") -> None:
    """把任务标记为「按采集源生命周期路由不该采」，终态，不再回到 pending。

    为什么需要（2026-09-24）
    ----------------------
    `pick_targets` 只过滤**新取**的型号。路由生效**之前**入队、当天仍是
    pending 的 legacy 任务会绕过它被重做 —— 实测 9/24 当天 jd 队列里就躺着
    `Arc A380 6G` / `GTX 1050 2G` 两个（那天恰好都已 done 才没出事）。

    为什么必须落盘而不是"本轮跳过"：不落盘的话这些任务会永远停在 pending，
    每轮被捡起一次又被丢掉，队列里积一堆"永远不会完成"的待办，
    看队列的人无从分辨。落盘成终态后 `clear_finished()` 会正常回收它。
    """
    _update(task_id, status=STATUS_ROUTED, last_reason=(reason or "生命周期路由排除")[:120])


def recover_stale() -> int:
    """把 running 状态回滚为 pending。

    `running` 只可能由**当前进程**设置。所以进程启动时若还看到 running，
    说明上一个进程死在半路了 —— 那些任务的成果要么已落盘（结果文件），
    要么需要重做。
    """
    data = _load_raw()
    n = 0
    for raw in data["tasks"].values():
        if raw.get("status") == STATUS_RUNNING:
            raw["status"] = STATUS_PENDING
            raw["updated_at"] = _now()
            n += 1
    if n:
        _save_raw(data)
        logger.info("队列恢复：%d 个中断任务已回滚为待办", n)
    return n


def reset_running_and_pending(source: str | None = None) -> int:
    """把某源所有未完成任务清掉（重置轮转用）。"""
    data = _load_raw()
    drop = [
        tid for tid, raw in data["tasks"].items()
        if raw.get("status") in (STATUS_PENDING, STATUS_RUNNING, STATUS_FAILED)
        and (source is None or raw.get("source") == source)
    ]
    for tid in drop:
        data["tasks"].pop(tid, None)
    if drop:
        _save_raw(data)
    return len(drop)


# ------------------------------------------------------------------ 结果落盘

def _result_file(source: str, day: str) -> Path:
    return RESULTS_DIR / f"{source}-{day}.jsonl"


def append_quotes(task_id: str, source: str, day: str, quotes: list) -> int:
    """把本次采到的报价**立即追加落盘**。

    这是"浏览器崩溃也不丢数据"的关键：每个型号采完就写，而不是攒到
    一轮结束再统一写内存。崩了以后 `load_quotes()` 能把这些捡回来。

    Returns:
        写入条数。
    """
    if not quotes:
        return 0
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _result_file(source, day)
    lines = []
    for q in quotes:
        payload = asdict(q) if hasattr(q, "__dataclass_fields__") else dict(q)
        lines.append(json.dumps({"task_id": task_id, "quote": payload}, ensure_ascii=False))
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
        fh.flush()
    return len(lines)


def load_quotes(source: str, day: str, task_ids: set[str] | None = None) -> list:
    """读回已落盘的报价（供崩溃恢复时合并）。

    Args:
        task_ids: 只取这些任务的（None 表示全部）
    """
    from ..collectors.base import Quote, quote_identity

    path = _result_file(source, day)
    if not path.exists():
        return []

    out: list[Quote] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if task_ids is not None and row.get("task_id") not in task_ids:
            continue
        payload = row.get("quote") or {}
        # 去重：同一型号可能因重试被写了两次。
        # ⚠️ 必须与 `base.dedupe_quotes` 用**同一个**身份键 —— 否则
        #    "落盘时按链接去重、读回时按标题去重"会得出不同结果。
        key = quote_identity(
            payload.get("platform_code"),
            payload.get("title_raw"),
            payload.get("price"),
            payload.get("url", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        try:
            out.append(Quote(**payload))
        except TypeError:
            continue
    return out


def clear_results(source: str, day: str) -> None:
    """清掉结果文件（流水线已写库后调用）。"""
    path = _result_file(source, day)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def clear_finished(day: str | None = None, keep_results: bool = False) -> int:
    """清掉已完成/已结案的任务，避免队列无限增长。"""
    data = _load_raw()
    drop = [
        tid for tid, raw in data["tasks"].items()
        if raw.get("status") in (STATUS_DONE, STATUS_EMPTY, STATUS_ROUTED)
        and (day is None or raw.get("day") == day)
    ]
    for tid in drop:
        data["tasks"].pop(tid, None)
    if drop:
        _save_raw(data)
    if not keep_results and day:
        for f in RESULTS_DIR.glob(f"*-{day}.jsonl"):
            try:
                f.unlink()
            except OSError:
                pass
    return len(drop)


# ------------------------------------------------------------------ 观测

def summary() -> dict:
    """队列状态摘要（给 CLI / 网页用）。"""
    data = _load_raw()
    counts: dict[str, int] = {}
    by_source: dict[str, dict[str, int]] = {}
    for raw in data["tasks"].values():
        status = raw.get("status", "?")
        counts[status] = counts.get(status, 0) + 1
        src = raw.get("source", "?")
        by_source.setdefault(src, {})
        by_source[src][status] = by_source[src].get(status, 0) + 1

    return {
        "total": len(data["tasks"]),
        "by_status": counts,
        "by_source": by_source,
        "saved_at": data.get("saved_at"),
        "queue_file": str(QUEUE_FILE),
        "results_dir": str(RESULTS_DIR),
    }
