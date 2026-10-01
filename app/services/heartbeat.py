"""采集心跳：给「停滞看门狗」提供「最后一次有进展是什么时候」。

为什么单独一个模块
------------------
心跳的**写入方**是 `app/collectors/base.py`（每处理完一个型号调一次），
**读取方**是 `app/services/pipeline.py`（停滞看门狗线程）。
而 pipeline 依赖 collectors（`from ..collectors import get_collectors`），
collectors 反过来 import pipeline 就是循环导入。

放这里两边都能用，且这个模块零依赖、不会引入新的循环。

背景（2026-10-01 实测）
----------------------
断网时 `page.goto(timeout=40000)` **不返回**，一轮采集卡到 35 分钟的
墙钟兜底才被杀。192 轮里 77 轮（40%）是这么死的，被杀的轮次中位数
44.6 分钟，而正常轮次 P50 只有 11.4 分钟。

光看「进程活了多久」分不出「慢」和「卡死」—— 需要一个**进展**信号。
"""
from __future__ import annotations

import time

# 最后一次「有进展」的墙钟时刻。
# ⚠️ 用 time.time()（Epoch，含休眠）而不是 monotonic —— 后者在 macOS 上
#    基于 mach_absolute_time，休眠期间会冻结（项目里踩过这个坑）。
_last_progress_at: float = time.time()


def note() -> None:
    """标记「有进展」。每处理完一个型号调一次。"""
    global _last_progress_at
    _last_progress_at = time.time()


def stalled_seconds() -> float:
    """距离上次有进展过了多久。"""
    return time.time() - _last_progress_at


def reset() -> None:
    """重置心跳（新一轮开始时调，避免上一轮的陈旧值立刻触发看门狗）。"""
    note()
