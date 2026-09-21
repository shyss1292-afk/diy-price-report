"""采集熔断器（Circuit Breaker）—— 撞墙之后**记住**，而不是下一轮接着撞。

为什么需要
----------
三个平台都会在探测到异常频率后返回限流页：京东"访问频繁/无法搜索"、
闲鱼 punish 页、拼多多"系统繁忙"。改造前的反应是"空结果累积到阈值就
结束本轮"，但**没有任何冷却记录** —— 下一轮（甚至手动立刻重跑）又会
原样去撞一遍。京东今天下午就是这样被连续撞进限流的。

本模块提供跨进程的冷却登记：

    trip()                命中限流时登记冷却截止时间（时长按退避阶梯）
    fast_fail()           Fast-Fail 门禁：冷却期内直接跳过，**绝不等待**
    record_success()      该轮正常采到数据且无限流 → 连续熔断计数清零
    cooldown_remaining()  还剩多久
    wait_until_ready()    需要等的时候才等（剩余 ≤ 上限就睡，超过就跳过）
    clear()               手动解除

为什么要写盘
------------
采集是**每轮一个独立进程**（launchd 每小时拉起一次），内存状态活不过一轮。
落盘后无论下一轮是 1 分钟后（手动重跑）还是 1 小时后（定时任务），
都能看到上一轮的限流记录 —— 前者会真的睡够冷却，后者会发现已自然过期。

跨轮次退避阶梯
--------------
**"连着被拦"和"偶尔被拦"是两件不同的事。** 固定的 3~5 分钟冷却只够应付
偶发限流；如果同一平台连续几轮都被拦，说明它已经把我们这段会话/指纹标记了，
短冷却结束后再去撞，只会把标记越焊越死 —— 京东 2026-09-21 下午就是这样
被连续撞进长时间限流的。

所以冷却时长按**连续熔断次数**逐级放大（默认 30min → 2h → 6h → 24h 封顶）：

    连续第 1 次  30 分钟
    连续第 2 次  2 小时
    连续第 3 次  6 小时
    连续第 4 次  24 小时（封顶，不再增长）

计数是**连续**的：只要某轮该平台正常采到数据且没有触发限流，
`record_success()` 就把它清零，下次熔断从 30 分钟重新开始。

配合这个阶梯，门禁必须是 **Fast-Fail** —— 等 6 小时没有任何意义，
所以 `fast_fail()` 不睡，直接让调度层跳过该平台（不分配任务、不拉起浏览器），
把这一轮的时间留给正常平台。

⚠️ 这个模块只管"什么时候不该碰某个平台"。真正决定"换个新会话再来"的是
   `browser_worker`（销毁实例）；两者配合见 `base.run_browser_batch`。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from ..config import DATA_DIR

logger = logging.getLogger("diyprice.breaker")

BREAKER_FILE: Path = DATA_DIR / "breaker.json"

# 单次最多睡多久。超过这个值就**跳过该源**而不是干等 ——
# 否则一个 5 分钟冷却会把整轮采集拖垮（每轮本来就 9~10 分钟）。
#
# 注意：退避阶梯生效后冷却动辄 30 分钟起，**生产路径根本不会走到"睡"这一步**
# （见 `fast_fail`）。这个上限现在只服务于 `wait_until_ready` ——
# 它保留给人工排障 / CLI 场景：那种场合你确实想"等它几分钟再试一次"。
_DEFAULT_MAX_INLINE_WAIT = 300.0
# 长睡眠切成小段，便于在日志里看到进度、也便于 Ctrl-C 打断
_SLEEP_CHUNK = 30.0

# 跨轮次退避阶梯：连续熔断第 N 次对应的冷却秒数，末级封顶。
# 30min / 2h / 6h / 24h —— 可用 DIYPRICE_BREAKER_LADDER 覆盖（逗号分隔秒数）。
_DEFAULT_BACKOFF_LADDER: tuple[float, ...] = (1800.0, 7200.0, 21600.0, 86400.0)

# ------------------------------------------------------------------ 错误分级
#
# **"平台把我们拦了"和"平台自己忙"是两件事，代价完全不同。**
#
# 硬拦截 = 平台已经把我们判定为异常：验证码、风控惩罚页、被踢到登录页、403。
#          连着被拦说明这段会话/指纹已被标记 —— 短冷却后再去撞只会把标记
#          焊死，所以必须走完整阶梯（30min → 24h）。
#
# 软风控 = 平台侧临时繁忙 / 限流（"系统繁忙"、"请稍后再试"、429）。
#          这未必是冲我们来的，可能是平台自己在抖动。用同一个阶梯把它放大到
#          "几小时不采"，就是**长时间踏空** —— 明明恢复了却还在等。
#          所以软风控套一个上限（默认 15 分钟）。
#
# ⚠️ 匹配顺序：**先判硬、再判软**。
# 京东的拦截页文案是「抱歉由于访问频繁导致无法搜索，请稍后再试！」——
# 同时含"访问频繁"（硬）和"请稍后再试"（软）。若先判软，这条真·硬拦截
# 会被误降级成 15 分钟，等于放它反复撞。顺序反了就是 bug。
_HARD_MARKERS: tuple[str, ...] = (
    "验证",        # 验证码 / 滑动验证 / 安全验证
    "captcha",
    "punish",      # 闲鱼风控惩罚页
    "非法访问",
    "访问频繁",
    "无法搜索",
    "防刷",
    "passport",    # 被踢到登录页 = 登录态已被风控处置
    "login",
    "verify",
    "risk.",       # risk.jd.com
    "anti.",       # anti.jd.com
    "blackhole",   # 京东反爬黑洞
    "busy",        # busy.html
    "403",
)

_SOFT_MARKERS: tuple[str, ...] = (
    "系统繁忙",
    "繁忙",
    "请稍后再试",
    "稍后重试",
    "访问异常",
    "429",
    "40001",       # 拼多多的签名/限流错误码
)

# 软风控的退避上限（秒）。默认 15 分钟。
#
# 为什么是 15 分钟：调度是**每小时一轮**，15 分钟短于轮次间隔 ——
# 于是"软风控不会让任何一个定时轮次被跳过"，同时又不至于无限等。
# 真要更保守可调大（见 DIYPRICE_BREAKER_SOFT_CAP）。
_DEFAULT_SOFT_CAP = 900.0

# 给日志/CLI 用的中文标签
SEVERITY_LABEL: dict[str, str] = {"hard": "硬拦截", "soft": "软风控"}

_LOCK = threading.Lock()


def disabled() -> bool:
    """`DIYPRICE_BREAKER_DISABLE=1` 可整体关掉熔断（排障用）。

    留这个开关的理由：熔断会"少采"，万一指标写得太宽导致误判，
    需要一个不改代码就能立刻恢复采集的出口。
    """
    return os.getenv("DIYPRICE_BREAKER_DISABLE", "0") == "1"


def _max_inline_wait() -> float:
    try:
        return float(os.getenv("DIYPRICE_BREAKER_MAX_INLINE_WAIT", "") or _DEFAULT_MAX_INLINE_WAIT)
    except ValueError:
        return _DEFAULT_MAX_INLINE_WAIT


# ------------------------------------------------------------------ 退避阶梯

def backoff_ladder() -> tuple[float, ...]:
    """当前生效的退避阶梯（`DIYPRICE_BREAKER_LADDER=1800,7200,...` 可覆盖）。

    留环境变量覆盖是为了排障：阶梯默认以"小时"计，压到秒级才能做端到端演练。
    """
    raw = os.getenv("DIYPRICE_BREAKER_LADDER", "").strip()
    if raw:
        try:
            vals = tuple(float(x) for x in raw.split(",") if x.strip())
            if vals:
                return vals
        except ValueError:
            logger.warning("DIYPRICE_BREAKER_LADDER 解析失败，改用默认阶梯：%s", raw[:80])
    return _DEFAULT_BACKOFF_LADDER


def backoff_for(trips: int, severity: str = "hard") -> float:
    """连续第 `trips` 次熔断对应的冷却秒数（超出阶梯长度取末级封顶）。

    `severity="soft"` 时再套一层 `soft_cap()`（默认 15 分钟）——
    平台自己"繁忙"不该让我们几小时不采；只有硬拦截才走完整阶梯。
    """
    ladder = backoff_ladder()
    index = max(1, int(trips)) - 1
    rung = ladder[min(index, len(ladder) - 1)]
    if severity == "soft":
        return min(rung, soft_cap())
    return rung


def soft_cap() -> float:
    """软风控的退避上限（秒），`DIYPRICE_BREAKER_SOFT_CAP` 可覆盖。"""
    try:
        return max(0.0, float(os.getenv("DIYPRICE_BREAKER_SOFT_CAP", "") or _DEFAULT_SOFT_CAP))
    except ValueError:
        return _DEFAULT_SOFT_CAP


def classify(reason: str) -> str:
    """把限流原因分成 `"hard"`（硬拦截）或 `"soft"`（软风控）。

    **先判硬、再判软** —— 京东的「访问频繁…请稍后再试！」同时含两类关键词，
    先判软会把它误降级成 15 分钟，等于放它反复撞（见上方 _HARD_MARKERS 注释）。

    未知原因一律按 `"hard"` 处理：宁可多等，也不要因为"不认识这个词"
    就放宽退避、把平台撞进更严的风控。
    """
    text = (reason or "").lower()
    for marker in _HARD_MARKERS:
        if marker.lower() in text:
            return "hard"
    for marker in _SOFT_MARKERS:
        if marker.lower() in text:
            return "soft"
    return "hard"


def human_duration(seconds: float) -> str:
    """把秒数说成人话（日志里"还剩 1783s"没法一眼看懂）。"""
    total = int(max(0.0, float(seconds)))
    if total < 60:
        return f"{total}秒"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}分{sec:02d}秒" if sec else f"{minutes}分"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}小时{minutes:02d}分" if minutes else f"{hours}小时"
    days, hours = divmod(hours, 24)
    return f"{days}天{hours}小时" if hours else f"{days}天"


# ------------------------------------------------------------------ 读写

def _load() -> dict:
    if not BREAKER_FILE.exists():
        return {"version": 1, "sources": {}}
    try:
        raw = json.loads(BREAKER_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # 文件损坏时**当成没有冷却**，而不是让采集全部停摆
        logger.warning("熔断状态文件损坏，忽略：%s", BREAKER_FILE)
        return {"version": 1, "sources": {}}
    if not isinstance(raw, dict):
        return {"version": 1, "sources": {}}
    raw.setdefault("sources", {})
    return raw


def _save(data: dict) -> None:
    """原子写：先写临时文件再 replace，避免读到半截 JSON。"""
    try:
        BREAKER_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = BREAKER_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(BREAKER_FILE)
    except OSError as exc:
        # 写不进去只影响"下次进程能否看到冷却"，不该让本轮采集崩掉
        logger.warning("熔断状态写入失败（不影响本轮）：%s", str(exc)[:120])


# ------------------------------------------------------------------ 对外

def trip(
    source: str,
    seconds: float | None = None,
    reason: str = "",
    severity: str | None = None,
) -> float:
    """登记一次熔断，返回冷却截止时间戳（epoch 秒）。

    冷却时长默认由**跨轮次退避阶梯**决定（见模块头）：连续第 N 次熔断取阶梯
    第 N 级；若 `severity` 判定为软风控（或未传时由 `classify(reason)` 判出），
    再套 `soft_cap()` 上限（默认 15 分钟）。

    `seconds` 显式传入时按传入值处理 —— 这条路径留给单测与人工排障，
    生产路径不传（见 `base.run_browser_batch`），否则阶梯会被架空。

    同源再次 trip 时**取较晚的截止时间** —— 冷却不该因为重复触发而变短。
    连续计数 `trips` 在这里自增，由 `record_success()` 清零。
    """
    with _LOCK:
        data = _load()
        prev = data["sources"].get(source) or {}
        consecutive = int(prev.get("trips", 0)) + 1
        sev = severity or classify(reason)
        cooldown = float(seconds) if seconds is not None else backoff_for(consecutive, sev)
        cooldown = max(0.0, cooldown)

        until = time.time() + cooldown
        if prev.get("until", 0) > until:
            until = prev["until"]
        data["sources"][source] = {
            "until": until,
            "until_text": datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M:%S"),
            "seconds": cooldown,
            "cooldown_text": human_duration(cooldown),
            "reason": (reason or "")[:200],
            "severity": sev,
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "trips": consecutive,
        }
        _save(data)
    return until


def record_success(source: str, verified: bool = False) -> bool:
    """某轮该平台**正常采到数据且没有触发限流** → 连续熔断计数清零。

    返回是否真的清掉了记录。三条护栏：

      · 记录不存在（本来就没熔断过）→ 返回 False，不写盘
      · 仍在冷却期内 → 返回 False。这种情况本来就不该出现"成功"，
        真出现了更可能是侥幸拿到一两条，不足以说明风控已解除 ——
        宁可让计数留着，也不要因为一次侥幸把退避阶梯重置回 30 分钟。
      · `verified=True` 时**豁免上一条**（探针专用）。

    关于 `verified`
    ---------------
    探针（`services/probe.py`）是**限频的、主动发起的、单型号的**真实请求，
    它采到数据就是"渠道此刻可用"的直接证据 —— 那是证据，不是侥幸。
    所以允许它提前结束冷却、立即恢复调度。

    ⚠️ 调度路径（`collectors/base.py`）**永远不要传 True** ——
    否则一次侥幸的少量结果就能把退避阶梯清空，阶梯也就白设了。
    """
    with _LOCK:
        data = _load()
        entry = data["sources"].get(source)
        if not entry:
            return False
        if not verified and float(entry.get("until", 0)) > time.time():
            return False
        data["sources"].pop(source, None)
        _save(data)
    return True


def consecutive_trips(source: str) -> int:
    """该平台当前连续熔断次数（0 = 没有未清零的记录）。"""
    with _LOCK:
        entry = _load()["sources"].get(source) or {}
    return int(entry.get("trips", 0))


def cooldown_remaining(source: str) -> float:
    """还剩多少秒冷却（已过期返回 0）。"""
    if disabled():
        return 0.0
    with _LOCK:
        entry = (_load()["sources"].get(source) or {})
    until = entry.get("until") or 0
    return max(0.0, float(until) - time.time())


def is_cooling(source: str) -> bool:
    return cooldown_remaining(source) > 0


def entry_of(source: str) -> dict:
    """某个源的冷却记录（用于日志/CLI 展示）。"""
    with _LOCK:
        return dict(_load()["sources"].get(source) or {})


def clear(source: str | None = None) -> int:
    """解除冷却。`source=None` 时清空全部，返回清掉的条数。

    顺带把**连续熔断计数**一起清掉 —— 记录整条被移除，下次熔断从阶梯第 1 级
    （默认 30 分钟）重新开始。人工判断"风控已经解除"时用它。
    """
    with _LOCK:
        data = _load()
        if source is None:
            n = len(data["sources"])
            data["sources"] = {}
        else:
            n = 1 if data["sources"].pop(source, None) else 0
        _save(data)
    return n


def snapshot() -> dict:
    """当前所有冷却状态（只含未过期的）。"""
    with _LOCK:
        sources = dict(_load()["sources"])
    now = time.time()
    alive = {
        code: {
            "remaining": round(max(0.0, float(v.get("until", 0)) - now), 1),
            "remaining_text": human_duration(max(0.0, float(v.get("until", 0)) - now)),
            "until_text": v.get("until_text"),
            "reason": v.get("reason"),
            "severity": v.get("severity") or classify(v.get("reason", "")),
            "trips": v.get("trips", 1),
            "cooldown_text": v.get("cooldown_text") or human_duration(v.get("seconds", 0)),
        }
        for code, v in sources.items()
        if float(v.get("until", 0)) > now
    }
    return alive


def active_summary() -> str:
    """一行式冷却摘要（给日志用），没有冷却返回空串。"""
    snap = snapshot()
    if not snap:
        return ""
    return " · ".join(
        f"{code} 连续第 {v['trips']} 次"
        f"（{SEVERITY_LABEL.get(v['severity'], v['severity'])}），"
        f"还剩 {v['remaining_text']}（{v['reason'] or '限流'}）"
        for code, v in snap.items()
    )


def fast_fail(source: str, label: str = "") -> tuple[bool, float]:
    """Fast-Fail 门禁：冷却期内**直接跳过，绝不等待**。

    Returns:
        (是否可以继续, 剩余冷却秒数)

    与 `wait_until_ready` 的分工：

      · `fast_fail`     —— 调度路径。退避阶梯最长 24 小时，等它毫无意义；
                           调用方拿到 False 就该**不分配任务、不拉起浏览器**，
                           把这一轮的时间留给正常平台（如闲鱼）。
      · `wait_until_ready` —— 人工排障路径。冷却很短时值得等几分钟再试。

    日志里会写清"连续第几次、退避多久"，否则"某个源今天一直没数据"
    看起来像 bug，实际是它在退避期内。
    """
    if disabled():
        return True, 0.0

    remaining = cooldown_remaining(source)
    if remaining <= 0:
        return True, 0.0

    entry = entry_of(source)
    severity = entry.get("severity") or classify(entry.get("reason", ""))
    logger.warning(
        "%s 处于熔断退避中（连续第 %s 次 · %s · 退避 %s，原因：%s），剩余 %s"
        " —— 本轮 Fast-Fail 跳过：不分配任务、不启动浏览器",
        label or source,
        entry.get("trips", 1),
        SEVERITY_LABEL.get(severity, severity),
        entry.get("cooldown_text") or human_duration(entry.get("seconds", 0)),
        entry.get("reason") or "限流",
        human_duration(remaining),
    )
    return False, remaining


def wait_until_ready(
    source: str,
    label: str = "",
    max_wait: float | None = None,
) -> tuple[bool, float]:
    """在开始采集某个源**之前**调用 —— 人工排障路径（调度路径用 `fast_fail`）。

    Returns:
        (是否可以继续, 剩余冷却秒数)

    行为分三种：

      · 没在冷却              → 立刻返回 (True, 0)
      · 冷却剩余 ≤ max_wait   → 睡够剩余时间再返回 (True, 0)
      · 冷却剩余 > max_wait   → **不睡**，返回 (False, 剩余) 让调用方跳过

    为什么第三种要跳过而不是硬等：一轮采集总共才 9~10 分钟，
    干等 5 分钟会把后面健康的源一起拖住，而限流本身也不会因为
    "我们等着"而提前解除。跳过更划算，下一轮再来。
    """
    if disabled():
        return True, 0.0

    remaining = cooldown_remaining(source)
    if remaining <= 0:
        return True, 0.0

    cap = _max_inline_wait() if max_wait is None else float(max_wait)
    name = label or source

    if remaining > cap:
        logger.warning(
            "%s 处于熔断冷却中，剩余 %.0fs 超过本次可等待上限 %.0fs，本轮跳过",
            name, remaining, cap,
        )
        return False, remaining

    logger.warning("%s 熔断冷却中，等待 %.0fs 后再开始（避免继续撞墙）", name, remaining)
    slept = 0.0
    while slept < remaining:
        chunk = min(_SLEEP_CHUNK, remaining - slept)
        time.sleep(chunk)
        slept += chunk
        left = remaining - slept
        if left > 1:
            logger.info("  %s 冷却还剩 %.0fs", name, left)
    logger.info("%s 冷却结束，重新开始采集", name)
    return True, 0.0
