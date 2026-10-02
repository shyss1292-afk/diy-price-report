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
  · `detect_page_state()`       — 四态判定：正常 / 风控 / 登录失效 / 明确无结果
  · `assert_not_rate_limited()` — 抛 `RateLimitError` 或 `AuthExpiredError`
  · `raise_if_empty()`          — 取数 0 条后抛 `EmptyResult`（"真无货"）
  · `warmup()`                  — 新会话第一跳前的首页预热

⚠️ 三种异常**处置方式完全不同**，这是本项目最容易搞错的地方：

    RateLimitError   风控惩罚   不可恢复 → 等冷却（推进退避阶梯）
    AuthExpiredError 登录失效   可恢复   → 通知人重新扫码（**不**推进阶梯）
    EmptyResult      明确无结果 正常业务 → 什么都不做，只是别算成"可疑"

混在一起的代价（真实事故）：`login.html` 既是限流特征、又是登录失效的证据，
于是"PDD 跳登录页 → 熔断 1 天 → 到期重试 → 还是登录页 → 冷更久"无限循环。

⚠️ 边界要说清楚：拟人化与差异化节奏能**推迟**触发限流、提高单轮可采数量，
   但**突破不了平台的总量限制**。账号已被标记时，该等的冷却期还是要等
   （冷却由 services/breaker.py 负责）。
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field

logger = logging.getLogger("diyprice.policy")


# 闲鱼 mtop `ret` 字段的纯错误码（取 `::` 前的串）
# 依据 fancyboi999/goofish-cli core/mtop.py:141-177（已读代码确认）
AUTH_EXPIRED_CODES = frozenset({
    "FAIL_SYS_SESSION_EXPIRED",   # session 过期（刷 cookie 可救）
    "FAIL_SYS_TOKEN_EXOIRED",     # token 过期（闲鱼后端拼写：EXOIRED 不是 EXPIRED）
    "FAIL_SYS_TOKEN_EMPTY",       # token 为空
})
# 风控类 —— 刷 cookie 救不了，必须抛 RateLimitError
RISK_CODES = frozenset({
    "FAIL_SYS_ILLEGAL_ACCESS",     # 非法访问（风控层）
    "FAIL_SYS_RATE_LIMIT",
    "RGV587_ERROR",
    "FAIL_SYS_USER_VALIDATE",
})


class RateLimitError(RuntimeError):
    """命中平台限流特征（**风控**）。

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


class AuthExpiredError(RuntimeError):
    """登录态已失效 —— **与风控严格分开，因为处置方式完全不同**。

    为什么必须分开（2026-10-02 加，依据两处独立证据）
    -------------------------------------------------
    `fancyboi999/goofish-cli` 在接口响应层就把失败分层
    （`core/mtop.py:29-41` 两份关键词清单 + `:157-177 _classify_error()`），
    并且**显式**把 `FAIL_SYS_ILLEGAL_ACCESS` 排除在"可恢复"之外 ——
    注释写着"是风控层问题，刷 cookie 也救不了"。

    我们此前把两者混在同一个 `RateLimitError` 里，后果有两层：

      1. **处置错**：登录失效要"人去重新扫码"，风控要"等冷却"。
         拿"等 6 小时"去治登录失效，等到天亮也不会好。
      2. **归因错**：`healthcheck.evaluate_login_expired()` 的注释自己写着
         「熔断器会把它误当成限流」—— 即我们要**事后**从原因字符串里反推。
         现在改成当场判定。

    ⚠️ 它**不进退避阶梯**（`breaker.trip` 不会被调用）。理由：退避是"别再去撞
    平台"的机制，而登录失效时我们撞的根本不是风控 —— 撞的是自己没登录。
    把登录失效记进阶梯，表现就是"某源冷了几小时，其实只是没人去扫码"。
    """

    def __init__(self, source: str, indicator: str, detail: str = "") -> None:
        self.source = source
        self.indicator = indicator
        self.detail = detail
        msg = f"{source} 登录态失效（特征「{indicator}」）"
        if detail:
            msg += f"：{detail}"
        super().__init__(msg)


class EmptyResult(Exception):
    """平台**明确**表示"这个关键词没有结果"。

    为什么要与"未知空页"分开
    ------------------------
    `base.run_browser_batch` 用「连续 N 个型号 0 条」来**推断**限流
    （`empty_streak` → `suspect_throttle`）。如果"真的没货"也被算作证据，
    那无货型号就成了限流推断的燃料 —— 京东每轮只采 2 个型号、其中常有无货
    型号，等于每天都在自己制造"被限流"的证据，然后把退避阶梯越推越高。

    判据取自**平台自己的空态**（京东的 `.empty-box`、闲鱼的"暂无相关宝贝"、
    拼多多的 `41001/41002/41003`），而不是靠"条数 = 0"猜 —— 这正是同类项目
    `CherryPainter/jd-product-crawler` 与 `goofish-cli/search.py:110-113`
    （`empty = /暂无相关宝贝|未找到相关宝贝|没有找到/`）的做法。
    """

    def __init__(self, source: str, indicator: str, detail: str = "") -> None:
        self.source = source
        self.indicator = indicator
        self.detail = detail
        super().__init__(f"{source} 平台明确表示无结果（特征「{indicator}」）")


# 页面状态码（`detect_page_state` 的返回值）
BLOCKED = "blocked"     # 风控惩罚：不可恢复，等冷却
AUTH = "auth"           # 登录态失效：可恢复，重新登录
EMPTY = "empty"         # 平台明确无结果：正常业务状态


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

    # ---------------------------------------------------------- 登录态失效
    # **与风控分开**：登录失效可恢复（重新扫码），风控不可恢复（要等冷却）。
    # 混在一起的后果是"某源冷了几小时，其实只是没人去扫码"。
    #
    # ⚠️ 只放**无歧义**的信号。像"请登录"这种词在页脚/顶栏都可能出现，
    #    放进来会把正常页判成登录失效 —— 那比不判更糟（会让我们忽略真风控）。
    #    可靠的运行时判据走 `services/session.probe_login_valid()`（主动探针）。
    auth_url_patterns: tuple[str, ...] = ()
    auth_indicators: tuple[str, ...] = ()

    # ---------------------------------------------------------- 空态
    # 平台**明确**表示"没有相关商品"的文案/错误码。
    # 命中它 → 抛 `EmptyResult` → 不计入 `empty_streak` 的限流推断。
    # 判错的代价是单向的（最多"少算一次可疑"），不会误熔断。
    empty_indicators: tuple[str, ...] = ()

    # ---------------------------------------------------------- 预热
    # 新会话的**第一跳之前**先访问这里养热会话上下文。
    # 依据：同类项目 `CherryPainter/jd-product-crawler`
    # `middlewares.py:189-205` 在拿到浏览器后、任何搜索之前先
    # `tab.get("https://www.jd.com")` 并停 3~5s；`Goodnameisfordoggy` 的
    # `LoginManager.BASE_URL` 也是首页。我们自己在拼多多侧早已验证过这条
    # （`pdd_source` 的注释：直接深链搜索页会被判安全验证）。
    warmup_url: str | None = None
    warmup_dwell_ms: tuple[int, int] = (1500, 3200)

    # ---------------------------------------------------------- 正向 URL 判据
    # 「最终 URL 必须落在我预期的域名上」—— 比黑名单更可靠：新出现的风控域名
    # 不用改代码就能被发现。依据 `jdcrawler/middlewares.py:228-246`
    # （`while "search.jd.com" not in tab.url and "list.jd.com" not in tab.url`）。
    #
    # ⚠️ 默认**只记日志不熔断**（`DIYPRICE_ENFORCE_URL_PREFIX=1` 才升级为熔断）：
    #    导航途中可能经过中间域名，设成硬判据前必须先跑一天看误报率。
    expected_url_prefixes: tuple[str, ...] = ()

    # ---------------------------------------------------------- 滚动完成判据
    # True 时 `behave()` 改为"滚到卡片数不再增长为止"，而不是固定像素。
    # 依据 `muxue-yqy/core/lazy_loader.py:27-72`（连续 N 次 (高度, 商品数) 都不变）
    # 与 `jdcrawler/middlewares.py:381-434`（按 `data-sku` **去重**计数）。
    #
    # ⚠️ **默认全关**：京东改走响应截获后，一次 XHR 就给全部数据，滚动已无必要；
    #    而它会让单型号停留时间变长，对"节奏规律"敏感的平台反而更危险。
    #    这条是"响应截获失效时的 B 计划"，要用再开。
    scroll_until_stable: bool = False
    stable_rounds: int = 3
    card_selector: str | None = None
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
        for indicator in (*self.rate_limit_indicators, *self.empty_indicators,
                          *self.auth_indicators):
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
            + (f" · 预热 {self.warmup_url}" if self.warmup_url else "")
            + (f" · 空态 {len(self.empty_indicators)} 条" if self.empty_indicators else "")
            + (f" · 登录失效率 {len(self.auth_indicators)} 条"
               if self.auth_indicators else "")
        )


# ======================================================================
# 三平台策略
# ======================================================================

JD = ThrottlePolicy(
    code="jd",
    label="京东",
    # ⏱ 2026-10-02 按实测上调（原为 gauss(5, 1.2) 夹到 [3, 8]）。
    #
    # 依据：三个 2026 年在维护的同类实现，节奏**都比我们慢**：
    #   · `jdcrawler/middlewares.py:73-75`  页内 8~15s、**翻页间隔 30~60s**
    #   · `CherryPainter` 每次滚动前 `interruptible_sleep(random.uniform(3,5))`
    #   · `muxue-yqy/lazy_loader` 滚动之间固定 sleep，无固定像素跳跃
    # 而我们原来是"2 个型号之间只隔 3~8 秒"—— 一个真人搜索两次通常是十几秒。
    #
    # 绝对成本可控：京东每轮只有 2 个型号，一个间隔从 ~5s 变成 ~8s，
    # 整轮只多几秒；而"节奏过于规律且快"正是风控的判据之一。
    delay_mu=8.0,
    delay_sigma=2.0,
    delay_floor=5.0,
    delay_ceil=16.0,
    long_pause_prob=0.20,
    long_pause_range=(15.0, 40.0),
    # ⚠️ 旧注释写"京东搜索结果是服务端渲染"—— **2026-10-02 实测推翻**：
    #    服务端 HTML 只有 47KB、零商品，数据在 `api.m.jd.com` 的明文 JSON 里。
    #    `ready_selector` 现在只在**接口失效、回落 DOM** 时起作用。
    wait_networkidle=False,
    ready_selector='[class*="goodsCardWrapper"]',
    # 新会话第一跳先落首页养热（我们 PDD 早就有这条，JD 一直缺）
    warmup_url="https://www.jd.com/",
    # 正向判据：最终必须在京东域内。默认只记日志（见字段说明）。
    expected_url_prefixes=("search.jd.com", "list.jd.com", "www.jd.com", "jd.com"),
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
        # 京东风控处置页。子 Agent 读同类项目时发现我们漏了它 ——
        # 它和 busy.html 同类，但 URL 里没有 busy 字样，旧特征抓不到。
        "risk_handler",
        "risk.jd.com",
        "anti.jd.com",
    ),
    # 「被踢到登录页」是**登录态失效**，不是风控 —— 2026-10-02 从上面那组里挪出来。
    # 处置方式不同：风控要等冷却，登录失效要人去重新扫码。
    auth_url_patterns=("passport.jd.com", "passport.jd.local"),
    # 空态：京东在"真的没有"时会渲染明确的空容器/文案。
    # 实测候选来自 `Goodnameisfordoggy/src/Exporter.py:78-88` 的 `.empty-box` 思路。
    empty_indicators=("没有找到相关的商品", "抱歉，没有找到", "暂无相关商品",
                      "无搜索结果", "empty-box"),
    cooldown_seconds=180,
    # 响应截获优先，DOM 兜底；`data-sku` 是京东商品卡上**不带构建哈希**的稳定属性
    card_selector='div[data-sku]',
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
        "rgv587",          # 大小写不敏感，已覆盖 RGV587_ERROR
        "系统繁忙",
        "非法访问",
        "请使用正常浏览器",
        # 以下三条来自同类项目的**风控特征清单**（2026-10-02 补）：
        #   `XianyuAutoAgent/XianyuApis.py:204`  if 'RGV587_ERROR' in msg or '被挤爆啦' in msg
        #   `goofish-cli/mtop.py:29-34`  _RISK_KEYWORDS = (..., "FAIL_SYS_USER_VALIDATE", "哎哟喂", "/punish")
        # 注意 `FAIL_SYS_ILLEGAL_ACCESS` **不在这里** —— 那是登录层的问题，
        # 归到 auth_indicators（同名项目 `mtop.py:146` 专门注释说明）
        "FAIL_SYS_USER_VALIDATE",
        "哎哟喂",
        "被挤爆啦",
    ),
    rate_limit_url_patterns=(
        "punish",
        "rgv587",
        "captcha",
        "sec.taobao.com",
        # 惩罚页的真实形态是 `punish?x5secdata=...`
        # （`zhinianboke/xianyu-auto-reply/routes/internal.py:117`）
        "x5secdata",
    ),
    auth_url_patterns=("login.taobao.com", "login.goofish.com", "havana"),
    auth_indicators=(
        # 淘系 mtop 的登录层错误码。`11273/goofish-client/token.manager.ts:69-76`
        # 的做法是 `ret[0].split('::')[0]` 取**纯错误码**再比对白名单 ——
        # 比子串匹配干净，但我们的判据入口是页面文本，只能先按子串来。
        "FAIL_SYS_SESSION_EXPIRED",
        "FAIL_SYS_TOKEN_EXPIRED",
        "FAIL_SYS_TOKEN_EXOIRED",   # ← 平台自己的拼写错误，不是笔误
        "FAIL_SYS_TOKEN_EMPTY",
        "令牌过期",
    ),
    empty_indicators=("暂无相关宝贝", "没有找到相关宝贝", "未找到相关宝贝",
                      "暂无搜索结果", "没有找到"),
    # 首页预热（闲鱼是 SPA，先落首页能让前端的登录态探针先跑一轮）
    warmup_url="https://www.goofish.com/",
    expected_url_prefixes=("goofish.com",),
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
           # 🚫 这里曾有裸数字 "40001"，2026-10-01 已删除 —— 它会命中正文里显卡型号
           #    "RTXA400016G"（RTX A4000 16G），当天误熔断 2 次。
           #    `ThrottlePolicy.__post_init__` 现在会**拒绝**任何纯数字特征。
           #
           # ⚠️ 而 40001 的真实语义是「登录已过期，请重新登录」（拼多多前端错误码表
           #    实测：`jynvch/pdd_spider/源文件/commons_源.js:2037-2059`），
           #    所以它已**挪到 auth**，不再当成风控 —— 这是本轮的一处语义纠正。
           "系统繁忙",
           "服务器繁忙",
           "服务器忙碌中",
           "请稍后再试",
           "安全验证",
           "滑动验证",
           "访问异常",
           # 54001 = isRisk（风控）。带键名，不会被商品型号撞上。
           #   依据 `GoodsList_源.js:6572-6579`：
           #     var r = 40001, n = 54001;
           #     isRisk: e.errorCode === n || e.error_code === n
           "error_code=54001",
       ),
    rate_limit_url_patterns=(
        "verify",
        "error_code=54001",
        "captcha",
    ),
    # 登录层：40001 / needLogin。「登录已过期，请重新登录」——
    # 同一个错误码表，`isRisk` 用 54001、`needLogin` 用 40001。
    auth_url_patterns=("login.html", "needLogin", "error_code=40001"),
    auth_indicators=("error_code=40001", "needLogin", "登录已过期"),
    # 空态：41001 商品不存在 / 41002 已下架 / 41003 已售罄
    # （`commons_源.js:2037-2059` 的完整错误码表）—— 这是"真无货"，不是风控。
    empty_indicators=("error_code=41001", "error_code=41002", "error_code=41003",
                      "暂无相关商品", "没有找到相关商品",
                      "商品已下架", "商品已售罄", "商品不存在"),
    # ⚠️ PDD 的预热在 `pdd_source._search()` 里（它带"被劫持后自愈"的判据
    #    `"search_result" not in page.url`，比通用预热更聪明），所以这里留空，
    #    否则一个会话会访问两次首页。
    warmup_url=None,
    expected_url_prefixes=("yangkeduo.com", "pinduoduo.com"),
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


def _page_url(page) -> str:
    try:
        return (page.url or "")
    except Exception:  # noqa: BLE001
        return ""


def _scan(patterns, haystack_lower: str, where: str) -> tuple[str, str] | None:
    """在**已 lower** 的 haystack 里找特征（特征本身也 lower 后再比）。

    ⚠️ 两边都必须 lower。历史 bug：URL 分支早就 lower 了，正文分支一直是裸 `in`
    —— 于是任何含大写 ASCII 的特征（`RGV587_ERROR`、`FAIL_SYS_USER_VALIDATE`）
    **静默永不命中**：不报错、不熔断，只是安静地失效。这类"沉默的守卫"
    比崩溃更难发现，所以现在只有这一个匹配入口。
    """
    for pattern in patterns:
        key = pattern.lower()
        if key in haystack_lower:
            return (pattern, where)
    return None


def detect_page_state(
    page, code: str | None, status: int | None = None
) -> tuple[str, str, str] | None:
    """判定页面处于哪种状态，返回 `(状态, 特征, 上下文)` 或 None（正常）。

    三种状态 + 正常，**处置方式各不相同**，这是"归因正确性"的核心：

        BLOCKED  风控惩罚  → 不可恢复，登记冷却（推退避阶梯）
        AUTH     登录失效  → 可恢复，通知人重新扫码（**不**推退避阶梯）
        EMPTY    明确无结果 → 正常业务状态，**不算**异常证据
        None     正常

    检查顺序（**顺序即语义，改动前先想清楚**）：

        HTTP 403/429
          → 登录 URL        ← 排在风控 URL 之前：`passport.jd.com` / `login.html`
                              这种**最终落地 URL** 无歧义，就是登录态没了
          → 风控 URL
          → 风控正文        ← 先风控后登录：京东拦截页文案可能同时提到登录
          → 登录正文
          → 空态

    历史教训：早期版本把 `passport.jd.com` / `login.html` 塞在"风控 URL"里，
    于是**登录失效被记成被限流** —— 冷却 1 天，到期重试还是登录失效，冷得更久，
    无限循环（`pdd_source` 的注释记录过这个形态）。
    """
    pol = policy_for(code)

    if status in (403, 429):
        return (BLOCKED, f"HTTP {status}", f"响应状态码 {status}")

    url_lower = _page_url(page).lower()

    hit = _scan(pol.auth_url_patterns, url_lower, "URL")
    if hit:
        return (AUTH, hit[0], f"URL 命中 {hit[0]}")

    hit = _scan(pol.rate_limit_url_patterns, url_lower, "URL")
    if hit:
        return (BLOCKED, hit[0], f"URL 命中 {hit[0]}")

    title, text = _text_probe(page)
    probe = f"{title}\n{text}"
    probe_lower = probe.lower()

    for patterns, kind in (
        (pol.rate_limit_indicators, BLOCKED),
        (pol.auth_indicators, AUTH),
        (pol.empty_indicators, EMPTY),
    ):
        for indicator in patterns:
            key = indicator.lower()
            if key in probe_lower:
                idx = probe_lower.find(key)
                # 片段取自**原文**，保持日志可读（不能被 lower 改写）
                snippet = probe[max(0, idx - 40): idx + 80].replace("\n", " ").strip()
                return (kind, indicator, snippet)

    return None


def detect_rate_limit(page, code: str | None, status: int | None = None):
    """检测是否命中限流，返回 `(特征, 上下文摘要)` 或 None。

    这是 `detect_page_state` 的**向后兼容包装**：只把 `BLOCKED` 与 `AUTH`
    视为"命中"，`EMPTY` 返回 None（无结果是正常业务状态，不是异常）。

    保留它是因为既有调用方与断言把它当成"页面是否异常"的判据；
    需要区分三种状态的地方请直接用 `detect_page_state`。
    """
    state = detect_page_state(page, code, status)
    if state is None or state[0] == EMPTY:
        return None
    return (state[1], state[2])


def assert_not_rate_limited(page, code: str | None, status: int | None = None) -> None:
    """页面异常就抛对应异常：风控 → `RateLimitError`，登录失效 → `AuthExpiredError`。

    `EMPTY`（平台明确无结果）**不抛** —— 那是正常业务状态。空态由
    `raise_if_empty()` 在**取数之后**处理（取数前页面可能还在加载，
    这时看到空态文案容易误判）。
    """
    state = detect_page_state(page, code, status)
    if state is None:
        return
    kind, indicator, detail = state
    if kind == EMPTY:
        return
    if kind == AUTH:
        raise AuthExpiredError(code or "?", indicator, detail)
    _snapshot_blocked(page, code, indicator)
    raise RateLimitError(code or "?", indicator, detail)


def raise_if_empty(page, code: str | None, status: int | None = None) -> None:
    """取数得到 0 条之后调用：若平台**明确**说无结果，抛 `EmptyResult`。

    为什么要单独一步：`base.run_browser_batch` 用"连续 N 个型号 0 条"推断限流。
    若不把"真无货"摘出去，无货型号就成了限流推断的燃料 —— 京东每轮只采 2 个
    型号、其中常有无货型号，等于每天自己制造"被限流"的证据。
    """
    state = detect_page_state(page, code, status)
    if state is not None and state[0] == EMPTY:
        raise EmptyResult(code or "?", state[1], state[2])


# ---------------------------------------------------------------- 现场留存

_BLOCKED_DIR_NAME = "blocked"
_last_snapshot: dict[str, float] = {}
_SNAPSHOT_WINDOW = 60.0     # 同一平台 60 秒内只存一份，避免限流页反复 dump
_SNAPSHOT_MAX_FILES = 40    # 上限；超出按时间删最旧


def _snapshot_blocked(page, code: str | None, indicator: str) -> str:
    """把命中限流的**现场**存下来（URL + 前 12KB HTML）。

    为什么值得：我们现在的日志只有特征串 + 40 字上下文，出问题时没有现场 ——
    "到底是不是风控页？是哪个风控页？"只能靠猜。同类项目
    `jdcrawler/middlewares.py:270-274` 在等不到商品时 `f.write(tab.html[:10000])`，
    思路直接可用。

    只存**前 12KB**：风控提示都在首屏；整页可能是几十 KB 的压缩 HTML。
    任何异常都被吞掉 —— 排障辅助绝不能影响采集。
    """
    import time as _time
    from pathlib import Path as _Path

    stamp = _time.time()
    if stamp - _last_snapshot.get(code or "?", 0.0) < _SNAPSHOT_WINDOW:
        return ""
    _last_snapshot[code or "?"] = stamp

    try:
        from ..config import DATA_DIR

        outdir = _Path(DATA_DIR) / _BLOCKED_DIR_NAME
        outdir.mkdir(parents=True, exist_ok=True)
        # ⚠️ 文件名带毫秒：60 秒节流窗口内不会重复，但**测试**会清空节流表 ——
        # 秒级精度下同一秒的两次调用会同名互相覆盖。
        # ⚠️ 文件名带毫秒：60 秒节流窗口内不会重复，但**测试**会清空节流表 ——
        # 秒级精度下同一秒的两次调用会同名互相覆盖。
        name = f"{code or 'unknown'}-{_time.strftime('%Y%m%d-%H%M%S')}-{__import__('uuid').uuid4().hex[:6]}"
        html = ""
        # ⚠️ 2026-10-02 修：`page.content()` 在限流页常抛异常（页面已关闭/导航
        #    中断），旧实现 `except: pass` 让 html 留空但**照样写 0 字节 .html** ——
        #    实测 80 个留存里 35 个是空文件，污染现场、误导排障。
        #    改：① content() 失败时用 `evaluate("document.documentElement.outerHTML")`
        #    兜底（content 走 CDP、evaluate 走 page，一个通另一个往往通）；
        #    ② 两个都失败 → 只写 .json 标注「无现场」，不写空 .html。
        html = ""
        try:
            html = page.content()[:12000]
        except Exception:  # noqa: BLE001
            try:
                html = page.evaluate(
                    "() => document.documentElement ? document.documentElement.outerHTML.slice(0, 12000) : ''"
                ) or ""
            except Exception:  # noqa: BLE001 —— 排障辅助绝不能影响采集
                html = ""
        if html:
            (outdir / f"{name}.html").write_text(html, encoding="utf-8", errors="ignore")
        (outdir / f"{name}.json").write_text(
            __import__("json").dumps(
                {
                    "code": code,
                    "indicator": indicator,
                    "url": _page_url(page)[:400],
                    "at": _time.strftime("%Y-%m-%d %H:%M:%S"),
                    "html_bytes": len(html),
                    "html_source": "content" if html else "none",
                    "note": "" if html else "页面已关闭，无法抓取现场 HTML",
                },
                ensure_ascii=False, indent=1,
            ),
            encoding="utf-8",
        )
        # 只留最近的 N 份（按 mtime）
        items = sorted(outdir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in items[_SNAPSHOT_MAX_FILES * 2:]:
            try:
                old.unlink()
            except OSError:
                pass
        return str(outdir / f"{name}.html")
    except Exception as exc:  # noqa: BLE001 —— 排障辅助，绝不能影响采集
        logger.debug("现场留存失败（不影响采集）：%s", str(exc)[:80])
        return ""


# ---------------------------------------------------------------- 预热 / 正向判据

def warmup(page, code: str | None) -> bool:
    """新会话的第一跳之前，先访问平台首页"养热"会话上下文。

    依据（三处独立证据）：
      · `jdcrawler/middlewares.py:189-205` —— 拿到浏览器后、**任何搜索之前**
        先 `tab.get("https://www.jd.com")` 并停 3~5 秒
      · `Goodnameisfordoggy` 的 `LoginManager.BASE_URL = "https://www.jd.com/"`
      · **我们自己的拼多多实测**：直接深链搜索 URL 会被重定向到
        `psnl_verification.html`（安全验证），先落首页再搜就正常

    失败**不致命**：预热只是降低首搜被拦的概率，不该因为首页慢就整轮失败。
    """
    pol = policy_for(code)
    if not pol.warmup_url:
        return False
    try:
        page.goto(pol.warmup_url, timeout=25000, wait_until="domcontentloaded")
        page.wait_for_timeout(random.randint(*pol.warmup_dwell_ms))
    except Exception as exc:  # noqa: BLE001
        logger.debug("%s 首页预热失败（不致命）：%s", pol.label, str(exc)[:80])
        return False
    logger.debug("%s 已完成首页预热：%s", pol.label, pol.warmup_url)
    return True


def url_prefix_mismatch(page, code: str | None) -> str:
    """最终 URL 是否**不在**预期的平台域名上；是则返回该 URL，否则空串。

    这是**正向判据**：与其列举"风控页长什么样"（黑名单永远追不上新域名），
    不如要求"URL 必须落在我预期的域名上"。

    ⚠️ 默认只记日志，不熔断。原因：导航途中可能经过中间域名
    （京东的 `*.3.cn`、`cfe.m.jd.com`），一上来就硬判会把正常跳转误判成拦截。
    设 `DIYPRICE_ENFORCE_URL_PREFIX=1` 才升级为熔断信号。
    """
    pol = policy_for(code)
    if not pol.expected_url_prefixes:
        return ""
    url = _page_url(page)
    if not url or url.startswith("about:"):
        return ""
    if any(p in url for p in pol.expected_url_prefixes):
        return ""
    return url


def _enforce_url_prefix() -> bool:
    import os

    return os.getenv("DIYPRICE_ENFORCE_URL_PREFIX", "").strip().lower() in ("1", "true", "yes")


# ---------------------------------------------------------------- 就绪与交互


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

    # ---- 正向 URL 判据（默认只观测，不熔断）----
    stray = url_prefix_mismatch(page, code)
    if stray:
        if _enforce_url_prefix():
            _snapshot_blocked(page, code, "URL_CHANGED")
            raise RateLimitError(code or "?", "URL_CHANGED",
                                 f"最终 URL 不在预期域名内：{stray[:160]}")
        logger.warning(
            "%s 最终 URL 不在预期域名内（观测，未熔断）：%s —— "
            "若已设 DIYPRICE_ENFORCE_URL_PREFIX=1 则会被判为被拦截",
            pol.label, stray[:160],
        )
    return status


def behave(page, code: str | None) -> None:
    """提数前的拟人化行为（滚动 → 悬停 → 鼠标抖动）。

    纯装饰性动作：**失败绝不影响采集**，因此整体兜异常。
    不做重试 —— 行为模拟失败就跳过，采集本身不依赖它。
    """
    pol = policy_for(code)
    try:
        if pol.scroll_until_stable and pol.card_selector:
            _scroll_until_stable(page, pol)
        elif pol.scroll_range:
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


def _scroll_until_stable(page, pol: ThrottlePolicy) -> None:
    """滚到「卡片数不再增长」为止（默认不启用，见 `scroll_until_stable` 说明）。

    与固定像素滚动的区别：固定像素会**静默少采** —— 拿到的条数比预期少，
    却不报警。这里以"卡片数连续 `stable_rounds` 次不变"为停止条件。

    计数按选择器去重（同类项目按 `data-sku` 去重，这样重复渲染/占位卡
    不会把计数虚高）；并按 `random.random() < 0.1` 混入一次**向上滚动**，
    因为真人的滚动方向本来就不单调。
    """
    prev = -1
    stable = 0
    for _ in range(12):                       # 硬上限，避免无限滚
        try:
            count = page.locator(pol.card_selector).count()
        except Exception:                     # noqa: BLE001
            return
        if count > prev:
            stable = 0
        else:
            stable += 1
        if stable >= pol.stable_rounds:
            return
        prev = count
        if random.random() < 0.1:
            page.mouse.wheel(0, -random.randint(50, 150))
        else:
            page.mouse.wheel(0, random.randint(400, 900))
        page.wait_for_timeout(random.randint(300, 700))


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
