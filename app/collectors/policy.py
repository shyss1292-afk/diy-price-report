"""平台差异化反限流策略 —— 单一权威来源。

三个平台的限流机制不一样，用同一套节奏去打是最容易被识别的：

    京东    限流最凶（实测"恢复"后只能再搜几次就再中），且每次搜索都很重，
            必须给足间隔并模拟"看一会儿再走"的行为
    闲鱼    页面是 SPA，加载后有前端探针，靠鼠标位移等行为特征判断是否真人
    拼多多  移动端 H5，请求签名（anti-content）由前端 JS 现算，
            导航后必须等它算完才拿得到数据

所以这里按平台定义各自的一套参数，**而不是在采集器里散落 if 判断** ——
散落的阈值久而久之会互相矛盾，而调参时又找不到全部位置。

本模块提供四类能力：

  · `PLATFORM_THROTTLE_CONFIG`  — 策略字典（调参只改这里）
  · `next_delay()`              — 高斯扰动的请求间隔（杜绝固定间隔）
  · `navigate()` / `behave()` / `settle()`
                                — 就绪等待 → 行为模拟 → 提数前静默
  · `assert_not_rate_limited()` — 限流识别，命中即抛 `RateLimitError`
                                   由 base.run_browser_batch 熔断处理

⚠️ 边界要说清楚：拟人化与差异化节奏能**推迟**触发限流、提高单轮可采数量，
   但**突破不了平台的总量限制**。账号已被标记时，该等的冷却期还是要等
   （冷却由 services/breaker.py 负责）。
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field

logger = logging.getLogger("diyprice.policy")


class RateLimitError(RuntimeError):
    """命中平台限流特征。

    这个异常是**熔断信号**：它会被 `base.run_browser_batch` 专门捕获，
    中止整批采集、销毁浏览器实例、登记冷却 —— 而不是像普通异常那样
    "记一笔失败、继续下一个型号"。后者正是"死循环撞墙"的来源。
    """

    def __init__(self, source: str, indicator: str, detail: str = "") -> None:
        self.source = source
        self.indicator = indicator
        self.detail = detail
        msg = f"{source} 命中限流特征「{indicator}」"
        if detail:
            msg += f"：{detail}"
        super().__init__(msg)


@dataclass(frozen=True)
class ThrottlePolicy:
    """单个平台的采集策略。"""

    code: str
    label: str

    # ---------------------------------------------------------- 请求间隔
    # 高斯分布 + 上下限夹取。用高斯而不是均匀分布：真人的浏览间隔是
    # 围绕某个习惯值波动的，均匀分布反而"太平整"。
    delay_mu: float = 8.0
    delay_sigma: float = 3.0
    delay_floor: float = 4.0
    delay_ceil: float = 20.0
    # 偶发一次"看久了"的长停顿（真人浏览时长本来就长短不一）
    long_pause_prob: float = 0.15
    long_pause_range: tuple[float, float] = (10.0, 25.0)

    # ---------------------------------------------------------- 就绪等待
    # 导航本身一律用 domcontentloaded（不会挂住），需要 networkidle 的
    # 平台再单独等一次 —— 见 wait_networkidle。
    wait_networkidle: bool = False
    networkidle_timeout_ms: int = 8000
    # 核心节点就绪判据。等不到**不算失败**（可能是限流页/无结果页），
    # 由限流检测去判定，避免把"没货"误报成"页面异常"。
    ready_selector: str | None = None
    ready_timeout_ms: int = 6000
    # 就绪后的额外静默时间 —— 给前端 JS 探针/签名运算留出时间
    ready_extra_ms: int = 0

    # ---------------------------------------------------------- 行为模拟
    # 平滑向下滚动的总像素（分若干步走完，不是一次跳到底）
    scroll_range: tuple[int, int] | None = None
    scroll_steps: tuple[int, int] = (2, 4)
    # 悬停时长：鼠标移到内容区后停留（触发 hover 态 + 让埋点看起来像真人）
    hover_ms: tuple[int, int] | None = None
    # 随机鼠标位移（绝对值，方向随机）：避免光标永远锁在 (0, 0)
    mouse_jitter: tuple[int, int] | None = None
    # 提数前的静默等待：等埋点参数（如京东 whwswswws）异步上报完
    settle_ms: tuple[int, int] = (600, 1200)

    # ---------------------------------------------------------- 熔断
    # 命中任一即判定限流。**要尽量具体** —— 这些字符串会被拿去
    # 匹配页面标题与正文，写得太泛（比如单个"验证"）会误伤正常页面。
    rate_limit_indicators: tuple[str, ...] = ()
    # URL 命中即判定（比正文更可靠，且不受渲染影响）
    rate_limit_url_patterns: tuple[str, ...] = ()
    # 冷却秒数：**平台基线**，只在显式指定冷却时长时使用（单测 / 人工排障）。
    #
    # ⚠️ 它不再是生产冷却时长的来源 —— 跨轮冷却由 `services/breaker.py` 的
    # **退避阶梯**决定（连续熔断第 N 次 → 30min / 2h / 6h / 24h 封顶）。
    # 原因是固定值对付不了"连续被拦"：京东 2026-09-21 就是被固定 180s
    # 连着撞进长时间限流的。这个字段保留为基线参考（见 describe 输出）。
    cooldown_seconds: int = 240
    # 单会话任务上限：达到就主动释放进程换新会话。
    # 平台风控看的是"一个会话里的行为序列长度"，会话拖太长比总时长更危险。
    session_task_limit: int = 20
    # 是否允许同一会话内并发多个标签页。
    #
    # 三个平台都设为 False：并发标签页意味着多个请求几乎同时发出，
    # 这在行为模型上极不自然（真人不会同时开三个页面并行搜索），
    # 而且会让"间隔随机化"失效 —— 并发请求的到达时间间隔趋近于 0。
    # 闲鱼对此尤其敏感（阿里系的行为风控会看同会话并发度）。
    #
    # 约束本来就由结构保证（采集器单线程、页面用完即还），这个字段的作用是
    # **让它可被监控**：worker 借页时发现context 里还有别的活动页会告警。
    allow_multi_tab: bool = False

    def __post_init__(self) -> None:
        """在**入口**拒绝会误伤的限流特征 —— 而不是等它在生产里误熔断。

        `detect_rate_limit()` 对「标题 + 整页正文」做的是朴素子串匹配，而
        正文里全是商品信息。所以特征串必须"自带语境"才能用。

        🐞 2026-10-01 的真实事故（此守卫就是为它加的）
        ------------------------------------------------
        PDD 的特征表里曾有一条**裸数字** `"40001"`，本意是匹配错误码。结果：

            23:31:33 拼多多 触发限流熔断：命中「40001」：
                     …i9-14900K/128G/1TSSD /RTXA400016G 仅剩5件…¥ 35000 56人想拼

        `40001` 命中在正文第 40 字符，上下文是 `/RTXA400016G` —— 显卡型号
        **RTX A4000 16G**，"A4000" + "16G" 拼出了 `40001`。那一页其实是
        **正常的搜索结果页**（有价格、有"56人想拼"）。当天因此误熔断 2 次，
        每次都中止整批，并**推进软风控退避阶梯**（15分 → 2小时 → 4小时）。

        所以：**纯数字特征一律禁止**。错误码要匹配就带上它的键名
        （如 `error_code=40001`），或者放进 `rate_limit_url_patterns`
        只在 URL 上匹配 —— URL 上没有商品正文，不存在这种碰撞。
        """
        for indicator in self.rate_limit_indicators:
            if indicator.isdigit():
                raise ValueError(
                    f"[{self.code}] 限流特征 {indicator!r} 是裸数字 —— "
                    "会被正文里的商品型号撞上（实测：'RTXA400016G' 命中 '40001'）。"
                    "请改带键名的形式（error_code=40001），或放进 rate_limit_url_patterns。"
                )

    def describe(self) -> str:
        return (
            f"{self.label}: 间隔 {self.delay_floor:.0f}~{self.delay_ceil:.0f}s"
            f"(μ={self.delay_mu:.1f},σ={self.delay_sigma:.1f})"
            f" · 单会话上限 {self.session_task_limit}"
            f" · 基线冷却 {self.cooldown_seconds}s"
        )


# ======================================================================
# 三平台策略
# ======================================================================

JD = ThrottlePolicy(
    code="jd",
    label="京东",
    # 用户指定：max(3.0, min(8.0, random.gauss(5, 1.2)))
    delay_mu=5.0,
    delay_sigma=1.2,
    delay_floor=3.0,
    delay_ceil=8.0,
    long_pause_prob=0.12,
    long_pause_range=(10.0, 25.0),
    # 京东搜索结果是服务端渲染 + React 水合，等 DOM 就够
    wait_networkidle=False,
    ready_selector='[class*="goodsCardWrapper"]',
    # 平滑滚动 300~600px + 悬停 1~2s，然后静默等埋点上报
    scroll_range=(300, 600),
    scroll_steps=(2, 3),
    hover_ms=(1000, 2000),
    mouse_jitter=(40, 90),
    settle_ms=(1500, 2500),
    rate_limit_indicators=(
        "访问频繁",
        "无法搜索",
        "防刷",
        "请稍后再试",
        "系统繁忙",
    ),
    rate_limit_url_patterns=(
        "busy.html",
        "verify",
        "risk.jd.com",
        "anti.jd.com",
        "passport.jd.com",     # 被踢到登录页 = 登录态已被风控处置
    ),
    cooldown_seconds=180,
    # 京东每轮默认只采 2 个型号（见 jd_source），所以这个上限几乎不会触发；
    # 留着是为了"用户手动把 DIYPRICE_JD_LIMIT 调大"时不至于一个会话跑太久。
    session_task_limit=6,
)

XIANYU = ThrottlePolicy(
    code="xianyu",
    label="闲鱼",
    delay_mu=9.0,
    delay_sigma=3.0,
    delay_floor=5.0,
    delay_ceil=18.0,
    long_pause_prob=0.15,
    long_pause_range=(10.0, 25.0),
    wait_networkidle=False,
    ready_selector=None,
    # 单页应用，首屏数据靠 XHR 拉，滚动触发懒加载
    scroll_range=(400, 800),
    scroll_steps=(2, 4),
    # 闲鱼的行为特征检测对"鼠标是否有真实位移"较敏感，
    # 随机抖动 50~100px，避免光标永远锁在 (0, 0)
    mouse_jitter=(50, 100),
    hover_ms=(600, 1400),
    settle_ms=(800, 1600),
    rate_limit_indicators=(
        "punish",
        "rgv587",
        "系统繁忙",
        "非法访问",
        "请使用正常浏览器",
    ),
    rate_limit_url_patterns=(
        "punish",
        "rgv587",
        "captcha",
        "sec.taobao.com",
        "login.taobao.com",
    ),
    cooldown_seconds=240,
    session_task_limit=20,
)

PDD = ThrottlePolicy(
    code="pdd",
    label="拼多多",
    delay_mu=10.0,
    delay_sigma=3.5,
    delay_floor=6.0,
    delay_ceil=22.0,
    long_pause_prob=0.15,
    long_pause_range=(10.0, 25.0),
    # 移动端 H5 是纯前端渲染：必须等网络静默或核心节点出现，
    # 否则拿到的是空壳 DOM
    wait_networkidle=True,
    networkidle_timeout_ms=9000,
    ready_selector='[class*="goods"], [class*="_3-"], a[href*="goods.html"]',
    # ≥500ms 留给 anti-content 签名计算 —— 早于它取数会拿到未签名数据
    ready_extra_ms=700,
    scroll_range=(500, 1000),
    scroll_steps=(2, 4),
    mouse_jitter=(60, 140),
    hover_ms=(500, 1200),
    settle_ms=(700, 1400),
       rate_limit_indicators=(
           "error_code=40001",
           # 🚫 这里曾有第二条裸数字 "40001"，2026-10-01 已删除 —— 它会命中
           #    正文里显卡型号 "RTXA400016G"（RTX A4000 16G），当天误熔断 2 次。
           #    错误码的 URL 形式已由 rate_limit_url_patterns 覆盖；
           #    ThrottlePolicy.__post_init__ 现在会**拒绝**任何纯数字特征。
           "系统繁忙",
           "请稍后再试",
           "安全验证",
           "滑动验证",
           "访问异常",
       ),
    rate_limit_url_patterns=(
        "verify",
        "error_code=40001",
        "login.html",           # 被踢到登录页
        "captcha",
    ),
    cooldown_seconds=300,
    # 用户指定：单 Session 任务上限严格控制在 15~20 个
    session_task_limit=18,
)


# 用户要求的名字，保持为公开契约
PLATFORM_THROTTLE_CONFIG: dict[str, ThrottlePolicy] = {
    JD.code: JD,
    XIANYU.code: XIANYU,
    PDD.code: PDD,
}

# 未知平台用的兜底策略：保守但可用（不静默失败，也不过度激进）
_FALLBACK = ThrottlePolicy(
    code="_default",
    label="未知平台",
    delay_mu=8.0,
    delay_sigma=3.0,
    delay_floor=4.0,
    delay_ceil=18.0,
    ready_selector=None,
    mouse_jitter=(50, 100),
    settle_ms=(500, 1000),
    cooldown_seconds=240,
    session_task_limit=20,
)


def policy_for(code: str | None) -> ThrottlePolicy:
    """取平台策略（未知平台返回保守兜底，而不是抛异常）。"""
    if not code:
        return _FALLBACK
    pol = PLATFORM_THROTTLE_CONFIG.get(code)
    if pol is None:
        logger.debug("平台 %s 没有专属策略，使用兜底策略", code)
        return _FALLBACK
    return pol


# ======================================================================
# 节奏
# ======================================================================

def next_delay(code: str | None) -> float:
    """下一次请求前该等多久（秒）。

    高斯采样后夹到 [delay_floor, delay_ceil] —— 夹取是必要的：
    高斯分布两端无界，不夹会出现"偶尔 0.2 秒连发"或"偶尔等 3 分钟"，
    前者正是风控要抓的突发性，后者白占时间。
    """
    pol = policy_for(code)
    delay = random.gauss(pol.delay_mu, pol.delay_sigma)
    delay = max(pol.delay_floor, min(pol.delay_ceil, delay))
    if random.random() < pol.long_pause_prob:
        delay += random.uniform(*pol.long_pause_range)
    return delay


# ======================================================================
# 页面交互
# ======================================================================

def _text_probe(page) -> tuple[str, str]:
    """取 (标题, 正文前 4000 字)，任一取不到就返回空串。

    只取前 4000 字有两个原因：限流页的提示都在首屏；以及风控页可能是
    无限增长的滚动容器，读全文既慢又没意义。
    """
    title = ""
    try:
        title = (page.title() or "")[:200]
    except Exception:  # noqa: BLE001 —— 页面崩了就拿不到，不影响判定
        pass

    text = ""
    try:
        text = page.evaluate(
            "() => document.body ? document.body.innerText.slice(0, 4000) : ''"
        ) or ""
    except Exception:  # noqa: BLE001
        pass
    return title, text


def detect_rate_limit(page, code: str | None, status: int | None = None):
    """检测是否命中限流，返回 `(特征, 上下文摘要)` 或 None。

    检查顺序按**可靠性**排：HTTP 状态码 → URL → 标题/正文。
    URL 判定比正文可靠，因为它不受渲染进度与文案改动影响。
    """
    pol = policy_for(code)

    if status in (403, 429):
        return (f"HTTP {status}", f"响应状态码 {status}")

    url = ""
    try:
        url = (page.url or "").lower()
    except Exception:  # noqa: BLE001
        pass
    for pattern in pol.rate_limit_url_patterns:
        if pattern in url:
            return (pattern, f"URL 命中 {pattern}")

    title, text = _text_probe(page)
    probe = f"{title}\n{text}"
    for indicator in pol.rate_limit_indicators:
        if indicator in probe:
            idx = probe.find(indicator)
            snippet = probe[max(0, idx - 40): idx + 80].replace("\n", " ").strip()
            return (indicator, snippet)

    return None


def assert_not_rate_limited(page, code: str | None, status: int | None = None) -> None:
    """命中限流就抛 `RateLimitError`（熔断信号）。"""
    hit = detect_rate_limit(page, code, status)
    if hit:
        raise RateLimitError(code or "?", hit[0], hit[1])


def navigate(page, url: str, code: str | None, timeout: int = 35000) -> int | None:
    """按平台策略导航并等到"可以取数"的状态，返回 HTTP 状态码。

    流程刻意拆开，因为每一步失败的**含义不同**：

        goto(domcontentloaded)   ← 失败 = 网络/浏览器问题，交给调用方处理
        wait networkidle?        ← 失败 = 页面有长连接，不算致命，继续
        wait ready_selector?     ← 失败 = 可能没货、也可能被限流，不算致命
        等 ready_extra_ms        ← 给前端探针/签名运算留时间
        限流检测                 ← 命中就抛 RateLimitError（这里最重要）

    中间两步都用 try 包住且**不重试** —— 重试只会让已经可疑的会话更可疑。
    """
    pol = policy_for(code)

    response = page.goto(url, timeout=timeout, wait_until="domcontentloaded")
    status = getattr(response, "status", None) if response is not None else None

    if pol.wait_networkidle:
        try:
            page.wait_for_load_state("networkidle", timeout=pol.networkidle_timeout_ms)
        except Exception:  # noqa: BLE001 —— 有长连接的页面永远不 idle，不致命
            logger.debug("%s 等到 networkidle 超时，继续", pol.label)

    if pol.ready_selector:
        try:
            page.wait_for_selector(pol.ready_selector, timeout=pol.ready_timeout_ms)
        except Exception:  # noqa: BLE001 —— 等不到不代表失败（可能是限流页）
            logger.debug("%s 核心节点未在 %dms 内出现", pol.label, pol.ready_timeout_ms)

    if pol.ready_extra_ms:
        page.wait_for_timeout(pol.ready_extra_ms)

    # 导航阶段就先查一次：被限流时越早停越好，别白做一轮行为模拟
    assert_not_rate_limited(page, code, status)
    return status


def behave(page, code: str | None) -> None:
    """提数前的拟人化行为（滚动 → 悬停 → 鼠标抖动）。

    纯装饰性动作：**失败绝不影响采集**，因此整体兜异常。
    不做重试 —— 行为模拟失败就跳过，采集本身不依赖它。
    """
    pol = policy_for(code)
    try:
        if pol.scroll_range:
            total = random.randint(*pol.scroll_range)
            steps = max(1, random.randint(*pol.scroll_steps))
            for _ in range(steps):
                page.mouse.wheel(0, max(60, total // steps))
                page.wait_for_timeout(random.randint(180, 520))

        if pol.hover_ms:
            page.mouse.move(random.randint(200, 900), random.randint(150, 600))
            page.wait_for_timeout(random.randint(*pol.hover_ms))

        if pol.mouse_jitter:
            # 方向随机、幅度有界 —— 目的只是让光标**动过**，
            # 光标从头到尾停在 (0,0) 本身就是脚本特征
            dx = random.choice((-1, 1)) * random.randint(*pol.mouse_jitter)
            dy = random.choice((-1, 1)) * random.randint(*pol.mouse_jitter)
            page.mouse.move(400 + dx, 300 + dy)
            page.wait_for_timeout(random.randint(120, 380))
    except Exception as exc:  # noqa: BLE001
        logger.debug("%s 行为模拟跳过：%s", pol.label, str(exc)[:80])


def settle(page, code: str | None) -> None:
    """提数前的静默等待 —— 等埋点参数/签名异步上报完。"""
    pol = policy_for(code)
    try:
        page.wait_for_timeout(random.randint(*pol.settle_ms))
    except Exception:  # noqa: BLE001
        pass


def describe_all() -> str:
    """给 CLI / 日志用的一行式策略摘要。"""
    return "\n".join(p.describe() for p in PLATFORM_THROTTLE_CONFIG.values())
