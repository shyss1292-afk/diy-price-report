"""拟人化采集节奏。

⚠️ 已被 `collectors/policy.py` 取代（2026-09-21）。
   现在三个采集器的节奏、行为模拟与就绪等待全部由 `PLATFORM_THROTTLE_CONFIG`
   统一提供 —— 那里能按平台差异化（京东/闲鱼/拼多多的节奏要求并不相同），
   而本模块只有一套全局参数。保留此文件仅为历史参考，**不要再新增调用**。


真实用户不会以精确 3 秒的节奏连续搜索 —— 那种规律性本身就是最显眼的
机器人特征，平台风控看的正是「频率 + 节奏规律」，而不是总次数。

这里做两件事：
  · 把间隔随机化，并偶发一次"看久了"的长停顿
  · 页面内加入滚动与鼠标移动，让行为序列不像脚本

⚠️ 边界要说清楚：拟人化能**推迟**触发限流、提高单轮可采数量，
但**不能突破平台的总量限制**。一旦账号被标记，该等的冷却期还是要等。
"""
from __future__ import annotations

import random


def human_delay(min_seconds: float = 6.0, max_seconds: float = 18.0) -> float:
    """返回一个随机化的搜索间隔（秒）。

    基础间隔在区间内均匀随机，另有 15% 概率追加 8~25 秒的"看久了"停顿 ——
    真人的浏览时长本来就长短不一，长短混合比固定值更接近真实分布。
    """
    delay = random.uniform(min_seconds, max_seconds)
    if random.random() < 0.15:
        delay += random.uniform(8.0, 25.0)
    return delay


def simulate_reading(page, rounds: int = 2) -> None:
    """在页面内做无害的浏览动作（滚动 + 鼠标移动）。

    纯粹是为了让行为更像人；失败也不影响采集，因此整体兜住异常。
    """
    try:
        for _ in range(random.randint(1, max(1, rounds))):
            page.mouse.wheel(0, random.randint(280, 900))
            page.wait_for_timeout(random.randint(300, 1000))
        page.mouse.move(random.randint(150, 900), random.randint(120, 600))
        page.wait_for_timeout(random.randint(200, 700))
    except Exception:
        pass
