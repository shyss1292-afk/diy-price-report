"""探活探针：退避期内用**一次真实请求**确认渠道是否已恢复。

为什么要它
----------
退避阶梯是"宁可错杀"的策略：一旦熔断，就按 30 分钟 ~ 24 小时不再碰。
但平台侧的限流往往**几分钟就解除了** —— 我们却还在盲等，这段时间是纯损失
（"踏空"）。京东实测冷却只有 20~25 分钟，而阶梯第 3 级已经是 6 小时。

探针把"盲等"换成"验证"：

    调度层（pipeline）   退避期内继续 Fast-Fail 跳过        ← 完全不动
    探针（本模块）       限频地发**一个型号**的真实搜索请求
      ├─ 成功  → breaker.record_success(source, verified=True) → 立即恢复调度
      └─ 失败  → **什么都不改**（不放大阶梯）

三条安全边界，缺一不可
----------------------
1. **限频**（`DIYPRICE_PROBE_MIN_INTERVAL`，默认 600s）
   不加限频，探针自己就变成"高频撞墙" —— 那正是把拼多多打进风控的原因
   （见 2026-09-20 记忆：每 8 分钟一轮的密集采集把 PDD 搜索打进风控）。
2. **单型号**：只查一个型号，不是一批。请求量比一轮正常采集（6~15 个）
   低一个量级。
3. **失败不放大**：探针失败**不调用 `trip()`**。阶梯统计的是"调度轮次被拦"，
   探针是我们主动多打的一枪，不该把退避推得更高。

⚠️ 本模块**不是** `scripts/reverse_check.py`。那个是"把源码改坏、验证自检
能拦住"的变异测试脚本，不联网；本模块才真的会发请求。两者职责不能混。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..config import DATA_DIR

logger = logging.getLogger("diyprice.probe")

PROBE_STATE_FILE: Path = DATA_DIR / "probe_state.json"

# 同一源两次探测的最小间隔（秒）。600s = 10 分钟。
_DEFAULT_MIN_INTERVAL = 600.0

_LOCK = threading.Lock()


def min_interval() -> float:
    """探测最小间隔，`DIYPRICE_PROBE_MIN_INTERVAL`（秒）可覆盖。"""
    try:
        return max(
            0.0, float(os.getenv("DIYPRICE_PROBE_MIN_INTERVAL", "") or _DEFAULT_MIN_INTERVAL)
        )
    except ValueError:
        return _DEFAULT_MIN_INTERVAL


# ------------------------------------------------------------------ 限频状态

def _load_state() -> dict:
    if not PROBE_STATE_FILE.exists():
        return {"version": 1, "sources": {}}
    try:
        raw = json.loads(PROBE_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("探针状态文件损坏，忽略：%s", PROBE_STATE_FILE)
        return {"version": 1, "sources": {}}
    if not isinstance(raw, dict):
        return {"version": 1, "sources": {}}
    raw.setdefault("sources", {})
    return raw


def _save_state(data: dict) -> None:
    try:
        PROBE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PROBE_STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(PROBE_STATE_FILE)
    except OSError as exc:
        logger.warning("探针状态写入失败（不影响本次探测）：%s", str(exc)[:120])


def _mark_probed(source: str) -> None:
    """记一次探测时间。

    ⚠️ 在**发请求之前**记，不是之后 —— 否则探测过程中崩溃/被 kill，
    限频记录丢失，下一次又会立刻探测。
    """
    with _LOCK:
        data = _load_state()
        data["sources"][source] = {"at": time.time(),
                                   "at_text": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        _save_state(data)


def seconds_since_probe(source: str) -> float | None:
    """距上次探测过了多少秒（从未探测过返回 None）。"""
    with _LOCK:
        entry = _load_state()["sources"].get(source) or {}
    at = entry.get("at")
    if not at:
        return None
    return max(0.0, time.time() - float(at))


def reset_rate_limit(source: str | None = None) -> int:
    """清掉限频记录（排障用：想立刻再探一次）。返回清掉的条数。"""
    with _LOCK:
        data = _load_state()
        if source is None:
            n = len(data["sources"])
            data["sources"] = {}
        else:
            n = 1 if data["sources"].pop(source, None) else 0
        _save_state(data)
    return n


# ------------------------------------------------------------------ 探针型号

def _pick_canary(session, source: str, model_hint: str | None = None):
    """挑一个型号当探针。

    优先用**该平台上一次真实采到过**的型号，而不是写死一个名字。理由：
    型号库一直在变（今天加 40 个显卡、明天改别名），写死的名字迟早找不到；
    而"上次真实采到过"的型号天然满足两个条件 —— 它存在，且这个平台上确实
    有它的商品。

    第二条很关键：**"没货导致的 0 条"和"被限流导致的 0 条"必须能分开**。
    拿一个平台上根本没有的型号去探，永远探不通，会误判成"渠道仍不可用"。
    """
    from sqlalchemy import select

    from ..models import Listing, Platform, Product

    if model_hint:
        hit = session.execute(
            select(Product).where(Product.model == model_hint, Product.is_active.is_(True))
        ).scalars().first()
        if hit is not None:
            return hit

    platform = session.execute(
        select(Platform).where(Platform.code == source)
    ).scalars().first()
    if platform is not None:
        last = session.execute(
            select(Listing.product_id)
            .where(Listing.platform_id == platform.id, Listing.is_synthetic.is_(False))
            .order_by(Listing.id.desc())
            .limit(1)
        ).scalars().first()
        if last is not None:
            product = session.get(Product, last)
            if product is not None and product.is_active:
                return product

    # 兜底：取聚焦品类的第一个型号（至少保证是"该采的"）
    from ..collectors.base import focus_categories

    wanted = focus_categories()
    stmt = select(Product).where(Product.is_active.is_(True))
    if wanted:
        stmt = stmt.where(Product.category.in_(sorted(wanted)))
    return session.execute(stmt.order_by(Product.id).limit(1)).scalars().first()


# ------------------------------------------------------------------ 结果

@dataclass
class ProbeOutcome:
    """一次探测的结果。`skipped` 非空表示**根本没发请求**。"""

    source: str
    ok: bool = False              # 渠道是否可用（真的采到数据）
    recovered: bool = False       # 是否因此清掉退避、恢复调度
    quotes: int = 0
    model: str = ""
    reason: str = ""              # 失败原因
    skipped: str = ""             # 跳过原因（未发请求）
    elapsed: float = 0.0
    detail: dict = field(default_factory=dict)

    def line(self) -> str:
        if self.skipped:
            return f"⏭ {self.source}: 跳过（{self.skipped}）"
        if self.ok:
            tail = "已恢复调度" if self.recovered else "渠道可用（本轮无退避可清）"
            return (f"✅ {self.source}: 探活成功，{self.model} → {self.quotes} 条，{tail}"
                    f"（{self.elapsed:.1f}s）")
        return f"❌ {self.source}: 仍不可用 —— {self.reason}（{self.elapsed:.1f}s）"


# ------------------------------------------------------------------ 探测

def probe(source: str, *, force: bool = False, model: str | None = None) -> ProbeOutcome:
    """对单个源做一次探活。

    Args:
        source: 采集源 code（jd / pdd / xianyu）
        force: True 时忽略"是否在退避期内"与"最小间隔"两道闸
               （人工排障用；**不要放进自动化流程**）
        model: 指定探针型号（默认自动挑，见 `_pick_canary`）

    Returns:
        `ProbeOutcome`。本函数**不抛异常** —— 探测失败也是一种结果。
    """
    from ..collectors import get_collectors
    from ..collectors import policy
    from ..db import session_scope
    from . import breaker

    started = time.monotonic()

    collectors = get_collectors([source])
    if not collectors:
        return ProbeOutcome(source, skipped=f"未知采集源：{source}")
    collector = collectors[0]

    # ---- 闸 1：只有退避中的源才需要探活 ----
    if not force and not breaker.is_cooling(source):
        return ProbeOutcome(source, skipped="未处于熔断退避期，无需探活")

    # ---- 闸 2：限频 ----
    if not force:
        since = seconds_since_probe(source)
        gap = min_interval()
        if since is not None and since < gap:
            return ProbeOutcome(
                source,
                skipped=f"距上次探测 {breaker.human_duration(since)}，"
                        f"未到最小间隔 {breaker.human_duration(gap)}",
            )

    # ---- 挑探针型号 ----
    with session_scope() as session:
        product = _pick_canary(session, source, model)
    if product is None:
        return ProbeOutcome(source, skipped="找不到可用作探针的型号")

    # ---- 记限频（发请求**之前**）----
    _mark_probed(source)

    # ---- 一次真实请求 ----
    from .browser_worker import get_worker

    worker = get_worker()
    quotes: list = []
    try:
        with worker.page(site=collector.browser_site) as page:
            quotes = collector._search(page, product)
    except policy.RateLimitError as exc:
        # 仍然被拦。**不 trip()** —— 探针是主动多打的一枪，不该把阶梯推高。
        outcome = ProbeOutcome(
            source,
            reason=f"仍被限流（{exc.indicator}）",
            model=product.model,
        )
        outcome.elapsed = time.monotonic() - started
        logger.warning("探针 %s：%s", source, outcome.reason)
        return outcome
    except Exception as exc:  # noqa: BLE001 —— 探测失败不该把调用方炸掉
        outcome = ProbeOutcome(source, reason=f"探测异常：{str(exc)[:120]}", model=product.model)
        outcome.elapsed = time.monotonic() - started
        logger.warning("探针 %s：%s", source, outcome.reason)
        return outcome
    finally:
        worker.stop()      # 短生命周期：探完就还内存

    if not quotes:
        outcome = ProbeOutcome(
            source, reason="渠道返回 0 条结果（可能仍被软拦，或该型号暂时无货）",
            model=product.model,
        )
        outcome.elapsed = time.monotonic() - started
        logger.warning("探针 %s：%s", source, outcome.reason)
        return outcome

    # ---- 探活成功：提前结束退避，立即恢复调度 ----
    recovered = breaker.record_success(source, verified=True)
    outcome = ProbeOutcome(
        source, ok=True, recovered=recovered, quotes=len(quotes), model=product.model
    )
    outcome.elapsed = time.monotonic() - started
    logger.warning(
        "探针 %s：探活成功（%s → %d 条）%s",
        source, product.model, len(quotes),
        "，已清除退避、立即恢复调度" if recovered else "，本轮无退避可清",
    )
    return outcome


def probe_cooling(*, force: bool = False) -> list[ProbeOutcome]:
    """对所有**当前处于退避期**的源各探一次（限频闸仍生效）。"""
    from . import breaker

    if breaker.disabled():
        return []
    return [probe(code, force=force) for code in breaker.snapshot()]
