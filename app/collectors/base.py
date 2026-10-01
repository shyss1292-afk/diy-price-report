"""采集适配器基类与统一数据结构。

真实平台适配器只需实现 `collect()`，
把页面解析结果转成 Quote 列表即可，其余交给流水线。
"""
from __future__ import annotations

import abc
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date


@dataclass
class Quote:
    """一条标准化后的原始报价（尚未归属到具体型号）。"""

    platform_code: str
    title_raw: str
    price: float
    condition: str = "全新"
    url: str = ""
    seller: str = ""
    matched_product_id: int | None = None  # 由归一化阶段回填
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.price = round(float(self.price), 2)


class BaseCollector(abc.ABC):
    """数据源适配器基类。"""

    code: str = ""
    name: str = ""

    @property
    def supported_platforms(self) -> list[str] | None:
        """该适配器负责的平台 code 列表；None 表示全部平台。"""
        return None

    @property
    def browser_site(self) -> str:
        """浏览器会话用的站点标识（读登录态 / 借页时用）。

        默认与 `code` 相同；**闲鱼是例外**（`code="xianyu"`，但会话文件与
        浏览器站点叫 `goofish`）。这个对应关系以前散在各采集器里硬编码成
        `site="..."`，探针要用时只能靠猜 —— 收敛成一个属性。
        """
        return self.code

    @abc.abstractmethod
    def collect(self, platform, products: list, day: date) -> list[Quote]:
        """采集指定平台下这批型号的报价。

        Args:
            platform: Platform ORM 实例（提供 code / kind / factor）
            products: 需要采集的 Product ORM 实例列表
            day: 目标采集日期

        Returns:
            Quote 列表。采集失败请抛出异常，由流水线记录日志。
        """
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} {self.code}>"


def focus_categories() -> set[str] | None:
    """轮转范围收窄开关：环境变量 DIYPRICE_FOCUS_CATEGORY=gpu,cpu。

    为什么需要：反爬源（京东/拼多多/闲鱼）单轮只能采几个型号，游标是绕
    **整个型号库**（309 个）转的。如果日报只看显卡和 CPU，让轮转把时间花在
    机箱、电源上就很不划算 —— 收窄后同样的轮数能把目标品类覆盖得更密。
    留空则维持全品类轮转。
    """
    raw = os.getenv("DIYPRICE_FOCUS_CATEGORY", "").strip()
    if not raw:
        return None
    return {c.strip() for c in raw.split(",") if c.strip()}


def build_rotation(products: list, source: str | None = None) -> list:
    """把型号按品类轮流展开成一个线性序列。

    这样游标每前进 N 位，取到的就是跨品类均匀分布的一批型号，
    而不是被显卡和 CPU 占满（机箱、电源等品类才不会饿死）。

    Args:
        source: 采集源 code。传入时按**生命周期路由**过滤：

          停产老硬件（GTX 10/16 系、RTX 20/30 系、RX 5000/6000 系、
          12 代及更早的 Intel、5000 系及更早的 AMD）在京东/拼多多
          **已无正品新货** —— 发搜索要么 0 条，要么把「显卡支架」
          「拆机风扇」这类配件当结果混进来。既占配额又抬风控概率。
          所以 legacy 型号**只走闲鱼**。

        ⚠️ 源不在 `SOURCE_LIFECYCLES` 里时**不过滤**（宽松兜底）——
           新增数据源时忘了登记，只是少省一点配额，不会把采集整个断掉。
    """
    wanted = focus_categories()
    if wanted:
        products = [p for p in products if p.category in wanted]

    if source:
        from ..seed_data import SOURCE_LIFECYCLES, lifecycle_of

        allowed = SOURCE_LIFECYCLES.get(source)
        if allowed is not None:
            products = [
                p
                for p in products
                if lifecycle_of(p.model, p.category, p.brand) in allowed
            ]

    buckets: dict[str, list] = {}
    for product in products:
        buckets.setdefault(product.category, []).append(product)

    ordered: list = []
    index = 0
    while True:
        added = False
        for category in sorted(buckets):
            if index < len(buckets[category]):
                ordered.append(buckets[category][index])
                added = True
        if not added:
            break
        index += 1
    return ordered


def pick_targets(products: list, limit: int, source: str | None = None) -> list:
    """取一批待采型号，支持带游标的轮转。

    Args:
        products: 全部型号
        limit: 本轮最多取几个
        source: 数据源 code。传入时会读写游标 —— 每轮接着上次的位置继续，
                多轮下来覆盖整个型号库；不传则每次都从序列开头取。

    Returns:
        本轮要采集的型号（数量 <= limit）。序列长度不足时自动回绕。

    这是「反爬限流源」的关键设计：单轮只能采少量，靠游标在多轮之间轮转，
    一天跑若干轮就能覆盖全库，而不是每次都重复采同一批型号。
    """
    ordered = build_rotation(products, source=source)
    if not ordered:
        return []

    total = len(ordered)
    start = 0
    if source:
        from ..services.cursor import load_offset

        start = load_offset(source) % total

    picked = [ordered[(start + i) % total] for i in range(min(limit, total))]

    if source:
        from ..services.cursor import save_offset

        save_offset(source, (start + len(picked)) % total)
    return picked


def pick_round_robin(products: list, limit: int) -> list:
    """不带游标的按品类轮流取样（保留给一次性调用场景）。"""
    return pick_targets(products, limit, source=None)


# ------------------------------------------------------------------ 浏览器批量采集

logger = logging.getLogger("diyprice.collector")


def route_allows(source: str, product) -> bool:
    """该采集源是否允许采这个型号（生命周期路由）。

    源不在 `SOURCE_LIFECYCLES` 里时**放行**（宽松兜底）—— 与 `build_rotation`
    同口径：新增数据源时忘了登记，只是少省一点配额，不会把采集整个断掉。
    """
    from ..seed_data import SOURCE_LIFECYCLES, lifecycle_of

    allowed = SOURCE_LIFECYCLES.get(source)
    if allowed is None:
        return True
    return lifecycle_of(product.model, product.category, product.brand) in allowed


def split_routed(source: str, tasks: list, by_id: dict) -> tuple[list, list]:
    """把队列任务按生命周期路由切成 (可派发, 该退役)。

    ⚠️ 为什么必须在**队列出口**再筛一次：`pick_targets` 只过滤**新取**的型号。
       路由生效**之前**入队、当天仍是 pending 的 legacy 任务会绕过它被重做 ——
       实测 9/24 当天 jd 队列里就躺着 `Arc A380 6G` / `GTX 1050 2G` 两个。
       （那天恰好两个都已 done 才没出事，属于运气，不是设计。）

    ⚠️ 型号不在 `by_id` 里时**放行**，交给下游按「型号已不存在」处理 ——
       这里不该替下游做那个判断。
    """
    kept: list = []
    retired: list = []
    for task in tasks:
        product = by_id.get(task.product_id)
        if product is not None and not route_allows(source, product):
            retired.append(task)
        else:
            kept.append(task)
    return kept, retired


def _next_batch(source: str, products: list, day: date, limit: int) -> list:
    """取本轮要做的任务：**优先重做队列里积压的**，不够再从游标补新的。

    这个顺序是断点续爬的关键 —— 崩在半路的任务会被优先捡起来，
    而不是等游标转一整圈（几十轮）才回头。同时队列也不会无限积压。

    ⚠️ 队列出口必须再过一遍生命周期路由（见 `split_routed`），
       且被排除的要**落盘退役** —— 只跳过不落盘的话它们会永远停在 pending。
    """
    from ..services import task_queue as tq

    by_id = {p.id: p for p in products}

    def _pending_after_route() -> list:
        kept, retired = split_routed(source, tq.pending(source, day), by_id)
        for task in retired:
            tq.retire_routed(task.task_id, f"生命周期路由：{task.model} 不走 {source}")
        if retired:
            logger.warning(
                "%s 队列里 %d 个型号按生命周期路由不该采（legacy 只走闲鱼），已退役：%s",
                source, len(retired), "、".join(t.model for t in retired[:5]),
            )
        return kept

    queued = _pending_after_route()
    if len(queued) < limit:
        need = limit - len(queued)
        fresh = pick_targets(products, need, source=source)
        if fresh:
            tq.enqueue(source, [(p.id, p.model) for p in fresh], day)
        # 重读队列时**仍走同一条过滤**，不能直接 `tq.pending()` ——
        # 否则这里会把刚补进来的和历史遗留的 legacy 任务一起放回去。
        queued = _pending_after_route()
    return queued[:limit]


def _heartbeat():
    """延迟导入心跳模块 —— services 与 collectors 互相依赖，
    模块级 import 会循环。"""
    from ..services import heartbeat
    return heartbeat


def page_dead(exc: Exception) -> bool:
    """异常是否表示「页面 / 浏览器已经不可用」。

    这类错误**重试同一个页面没有意义**，必须换实例：页面崩溃后
    context 可能已经失效，继续在它上面操作只会连环报错。
    """
    text = str(exc).lower()
    return any(
        key in text
        for key in (
            "page crashed",
            "has been closed",
            "target closed",
            "browser has been closed",
            "context or browser has been closed",
        )
    )


# 连续这么多次网络错误就判定「网络不可用」并提前结束本轮。
# 与 empty_streak_limit 是两套独立阈值 —— 混用会互相污染归因。
NET_STREAK_LIMIT = 3


def network_dead(exc: Exception) -> bool:
    """异常是否表示「网络不可用」（而不是平台在拦你）。

    ⚠️ 必须和「搜索无结果」严格分开，这是**归因正确性**问题：
       网络错误若混进 `empty_streak`，连续 3 次就会打出
       「判定已被限流，提前结束本轮」—— 实测 2026-10-01 有多轮就是这么报的，
       而日志里明明是 `ERR_INTERNET_DISCONNECTED`。
       方向指反的代价：去查平台风控、调退避阶梯，全是在治错的病。

    统计（2026-10-01）：`ERR_INTERNET_DISCONNECTED` 131 次，是最大头，
    远超 `ERR_PROXY_CONNECTION_FAILED`（51，全在 09-29 那一天）。

    ⚠️ 故意**不含** `ERR_CONNECTION_CLOSED` —— 那个既可能是本地网络问题，
       也可能是平台主动掐断（风控），归到哪边都会误导。让它走普通失败路径。
    """
    text = str(exc).lower()
    return any(
        key in text
        for key in (
            "err_internet_disconnected",
            "err_proxy_connection_failed",
            "err_name_not_resolved",
            "err_network_changed",
            "err_address_invalid",
            "err_socket_not_connected",
        )
    )


def should_reset_backoff(quote_count: int, aborted: bool, suspect_throttle: bool) -> bool:
    """本轮够不够格让退避阶梯**减一级**（见 `breaker.record_success`）。
    三个条件缺一不可，放宽任何一条都会让退避阶梯白设：

      · quote_count > 0     —— 真的采到数据了
      · not aborted         —— 没有命中限流特征被熔断中止
      · not suspect_throttle—— 没有因"连续 N 个型号 0 条"被**推断**为限流

    抽成纯函数是为了可断言：这条规则一旦被改宽（比如有人图省事写成
    `if quotes:`），自检必须能立刻发现。

    ⚠️ 用 `> 0` 而不是 `bool(...)` —— 负数是"没有数据"的另一种写法，
    而 `bool(-1)` 是 True，会把负数当成功（写这条断言时真的踩到了）。
    """
    return int(quote_count or 0) > 0 and not aborted and not suspect_throttle


# ----------------------------------------------------------------------
# 轮次分段计数
# ----------------------------------------------------------------------
#
# 为什么需要它：出问题时我们只能看到一句"某源今天数据少"，然后回去翻日志猜
# 是卡在哪一段 —— 网络没到？页面没就绪？接口没命中？解析失败？还是入库被过滤？
# 这几段的失败**表现完全一样**（都表现为 0 条），但处置方式完全不同。
#
# 借鉴自参考项目 superboyyy/xianyu_spider：它用 ws_frames / sync_pushes / parsed
# **三个分段计数**区分"服务端没推"和"推到了但没解开" —— 而不是笼统一句"连接失败"。
#
# 实现刻意做成"模块级累加 + 取走即清"：采集是单进程单线程的，
# 不需要锁；取走即清保证每一轮的数字不会串到下一轮。
_STAGE_COUNTS: dict[str, int] = {}


def note_stage(name: str, n: int = 1) -> None:
    """记录一次阶段事件。"""
    _STAGE_COUNTS[name] = _STAGE_COUNTS.get(name, 0) + n


def stage_counts() -> dict[str, int]:
    """看一眼当前累计（不清空）—— 用于在日志里打点。"""
    return dict(_STAGE_COUNTS)


def take_stage_counts() -> dict[str, int]:
    """取走并清空累计 —— 轮次结束时调用。"""
    out = dict(_STAGE_COUNTS)
    _STAGE_COUNTS.clear()
    return out


def format_stages(counts: dict[str, int] | None = None) -> str:
    """把阶段计数排成一行，供日志使用。"""
    data = counts if counts is not None else stage_counts()
    if not data:
        return ""
    return " · ".join(f"{k} {v}" for k, v in data.items())


def quote_identity(
    platform_code: str, title_raw: str, price: object, url: str = ""
) -> str:
    """一条报价的**身份键** —— 全项目唯一实现。

    优先用**商品链接**：同一件商品，卖家改个标题（加"包邮"、改空格）它还是它；
    用标题当键的话，每次改标题都会变成"新条目"，同一件商品被重复计入，
    日低价与均价都被拉偏。链接里的 item id 才是身份。

    ⚠️ 必须带**回落**：京东/拼多多的 `Quote.url` 目前存的还是**搜索页 URL**，
    `link_key()` 对它们返回空串。此时若不做回落，同一个型号的 30 条报价会被
    合并成 1 条 —— 那是灾难级的错。所以拿不到链接就退回原来的
    (平台, 标题, 价格) 键，行为与改前**完全一致**。

    拼成字符串（而不是元组）是为了让两处调用方（`base.dedupe_quotes`
    与 `task_queue.load_quotes`）共用同一份逻辑 —— 规则各写一套迟早会漂移。
    """
    from . import normalize

    link = normalize.link_key(url or "")
    if link:
        return f"link:{link}"
    return f"text:{platform_code}|{title_raw}|{price}"


def dedupe_quotes(quotes: list[Quote]) -> list[Quote]:
    """去重 —— 重试与崩溃恢复都可能带来重复。

    键由 `quote_identity()` 给出（优先商品链接，无链接回落文本键）。
    """
    out: list[Quote] = []
    seen: set[str] = set()
    for q in quotes:
        key = quote_identity(q.platform_code, q.title_raw, q.price, q.url)
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
    return out


def run_browser_batch(
    collector,
    products: list,
    day: date,
    *,
    site: str,
    limit: int,
    search_fn,
    label: str = "",
    viewport: dict | None = None,
    user_agent: str | None = None,
    empty_streak_limit: int = 3,
) -> list[Quote]:
    """浏览器型采集源的公共主循环。

    三个真实源（京东 / 拼多多 / 闲鱼）除了搜索参数外，流程完全一致。
    抽到这里是为了让**断点续爬、结果增量落盘、回收记账、熔断退避**这些
    容易漏掉的横切逻辑只写一遍 —— 三份复制粘贴的续爬代码是维护灾难。

    与改造前的差异（按重要性排）：

    1. **熔断退避**：命中平台限流特征（京东"访问频繁"、闲鱼 punish、
       拼多多"系统繁忙"）时**立刻中止整批**，登记冷却、销毁浏览器实例。
       改造前只会把当前型号记为失败、然后继续拿下一个型号去撞，
       同一批里连撞 3 次才停 —— 而"继续撞"本身就是加重风控的行为。
       冷却时长按**跨轮次退避阶梯**放大（连续第 N 次 → 30min/2h/6h/24h）。
    2. **批次前冷却门禁（Fast-Fail）**：上一轮触发过熔断的话，这里直接
       返回空、**不睡**（见 `breaker.fast_fail`）—— 退避动辄几小时，等它
       没有意义，时间要留给正常平台。调度层会先跳过，这里是第二道防线。
    3. **成功递减**：本轮正常采到数据且没触发限流 → 退避阶梯余量**减一级**
       （不是清零 —— 一次侥幸的成功不该抹掉整条阶梯；减到 0 才整条清掉，
       见 `breaker.record_success`）。
    4. **平台差异化节奏**：间隔改由 `policy.next_delay()` 提供
       （高斯扰动 + 上下限夹取），不再是采集器里写死的区间。
    5. **单会话任务上限**：采满 N 个型号主动释放进程换新会话 ——
       平台风控看的是"一个会话里的行为序列长度"，会话拖太长比总耗时更危险。
    6. 浏览器来自 `browser_worker`（短生命周期，用够就回收），
       不再是"连一个常驻 Chrome"。
    7. 每个型号采完**立刻把报价落盘**（`task_queue.append_quotes`），
       浏览器中途崩掉也不丢。
    8. 本轮颗粒无收时**不再回退游标**。原来"0 条就退回起点"会把游标
       钉死在原地（同批型号 → 0 条 → 回退 → 同批型号…），实测京东
       因此原地空转了 3 个多小时。现在任务留在队列里下轮优先重做，
       游标继续前进覆盖更多型号。
    9. **三类失败分开处置**（2026-10-02 加）—— 这是"归因正确性"的核心：

           RateLimitError    风控惩罚   → 中止整批 + 登记冷却（推退避阶梯）
           AuthExpiredError  登录失效   → 中止整批，**不**登记冷却，改发"重新登录"告警
           EmptyResult       明确无结果 → **什么也不做**，只是不计入 empty_streak

       为什么这很重要：三种失败在日志里**长得一模一样**（都是"这一轮 0 条"），
       但处置方式完全不同。把登录失效当成风控 → 冷几小时，而问题只是没人扫码
       （PDD 2026-10 之前就是"跳 login.html → 熔断 1 天 → 到期重试 → 还是
       login.html → 冷更久"的无限循环）；把"真无货"当成被拦的证据 →
       无货型号成了限流推断的燃料，退避阶梯被自己推高。
    10. **新会话第一跳前先预热首页**（`policy.warmup`）。依据：同类项目
        `jdcrawler/middlewares.py:189-205` 在拿到浏览器后、任何搜索之前先落
        一次 `www.jd.com`；我们自己在拼多多侧也实测过"直接深链搜索页会被判
        安全验证"。两件事天然对齐 —— `browser_worker` 每次回收重启正好
        需要重新养热。
    """
    from ..services import breaker
    from ..services import task_queue as tq
    from ..services.browser_worker import get_worker
    from . import policy

    label = label or getattr(collector, "name", "") or collector.code
    take_stage_counts()  # 新一轮：清掉上一轮的残留，数字不串轮
    if not limit or not products:
        return []

    code = collector.code
    pol = policy.policy_for(code)

    # ---- 熔断门禁（Fast-Fail，绝不等待）----
    # 放在 `_next_batch` **之前**：还在退避期就别去动游标，
    # 否则会白白推进一批型号（它们会被塞进队列，下轮才轮到）。
    #
    # 这里不睡。退避阶梯 30 分钟起步、最长 24 小时，等它毫无意义。
    # 调度层（`pipeline.run_pipeline`）在更早的位置就会跳过退避中的源 ——
    # 这一道是**第二防线**，拦住"有人直接调采集器绕过调度"的情况。
    proceed, remaining = breaker.fast_fail(code, label)
    if not proceed:
        return []

    batch = _next_batch(code, products, day, limit)
    if not batch:
        return []

    by_id = {p.id: p for p in products}
    worker = get_worker()
    fresh: list[Quote] = []
    started = time.monotonic()

    # 双层循环：外层"借页"，内层"采一段"。
    #
    # 为什么要分两层：回收阈值如果只在借页时检查，那单源内部连跑 20 分钟
    # 也不会回收（借页动作一次覆盖整个源）。内层达到阈值就 break 出去，
    # 外层重新借页 —— 借页会先完成回收重启，于是"每 N 个任务 / 每 M 分钟
    # 重启一次"在**采集过程中**就能生效。
    empty_streak = 0
    net_streak = 0                        # 连续网络错误（与空结果分开计数）
    network_down = False                  # 是否因网络不可用中止
    index = 0
    total = len(batch)
    session_tasks = 0
    aborted = False                       # 是否因限流熔断中止
    hit: Exception | None = None          # 命中的限流特征
    suspect_throttle = False              # 是否因"连续空结果"被判定限流
    recycle_for_session = False           # 是否因单会话上限需要换实例
    auth_failed: Exception | None = None  # 登录态失效（与限流分开处置）

    while index < total:
        session_tasks = 0                 # 每次借页 = 一个新会话，计数重置
        with worker.page(site=site, viewport=viewport, user_agent=user_agent) as page:
            # ---- 会话预热：每个**新会话**的第一跳之前先落一次首页 ----
            # 放在这里而不是 `_search` 里：`worker.page()` 每次回收重启都是
            # 一个新会话，预热与它天然对齐；放进 `_search` 会变成每个型号都预热
            # （白白多一倍请求）。平台没配 `warmup_url` 时是空操作。
            if index < total and not aborted:
                policy.warmup(page, code)

            while index < total:
                task = batch[index]
                index += 1
                product = by_id.get(task.product_id)
                if product is None:
                    tq.mark_failed(task.task_id, "型号已不存在")
                    continue

                tq.mark_running(task.task_id)
                # 心跳：停滞看门狗靠它区分「慢」和「卡死」。
                # 放在**调用之前** —— 万一 search_fn 卡住，看门狗才知道
                # 是从这一刻起没有进展的。
                _heartbeat().note()
                explicit_empty = False        # 平台**明确**表示无结果
                try:
                    found = search_fn(page, product)
                except policy.EmptyResult:
                    # 平台明确无结果 —— **正常业务状态**，不是异常。
                    # 不能在这里 `continue`：后面还有回收/间隔逻辑，
                    # 统一走下面的 `elif explicit_empty` 分支。
                    found = []
                    explicit_empty = True
                except policy.AuthExpiredError as exc:
                    # ---- 登录态失效：中止整批，但**不**进熔断退避阶梯 ----
                    # 理由见 `policy.AuthExpiredError` 与函数 docstring 第 9 条。
                    # 中止是必须的：没登录时继续搜下去，每个型号都会
                    # "0 条"，最后被 empty_streak 推断成"被限流" —— 归因指反。
                    tq.mark_failed(task.task_id, f"登录失效：{exc.indicator}"[:80])
                    auth_failed = exc
                    note_stage("登录失效中止")
                    break
                except policy.RateLimitError as exc:
                    # ---- 熔断：不再"记一笔失败继续下一个" ----
                    tq.mark_failed(task.task_id, f"限流：{exc.indicator}"[:80])
                    aborted, hit = True, exc
                    note_stage("限流中止")
                    break
                except Exception as exc:  # noqa: BLE001 —— 单个型号失败不该中断整轮
                    tq.mark_failed(task.task_id, str(exc)[:80])
                    note_stage("异常型号")
                    logger.warning("%s 采集异常 %s：%s", label, product.model, str(exc)[:90])
                    worker.note_task()
                    # 页面/浏览器已经崩了 → 当前实例不可信。标记之后，下一个
                    # 型号（或下一个源）会拿到全新实例，而不是继续在一个坏掉
                    # 的 context 上碰运气 —— 那是"京东崩了、三个源全归零"的成因。
                    if page_dead(exc):
                        worker.mark_dirty(str(exc)[:80])
                        logger.warning(
                            "%s 页面已失效，跳出本轮并重建浏览器实例（剩余型号留队列）", label
                        )
                        break     # 在坏页面上跑完剩下型号只会把它们全记成失败

                    # ---- 网络故障：与「搜索无结果」严格分开 ----
                    # ⚠️ 这是**归因正确性**问题，不是省几秒的问题。
                    # 网络错误若混进 empty_streak，连续 3 次就会打出
                    # 「判定已被限流，提前结束本轮」—— 实测 2026-10-01 有多轮
                    # 就是这么报的，而日志里明明是 ERR_INTERNET_DISCONNECTED。
                    # 方向指反的代价：去查平台风控、调退避阶梯，全是在治错的病。
                    if network_dead(exc):
                        net_streak += 1
                        logger.warning(
                            "%s 网络故障（连续第 %d 次）：%s",
                            label, net_streak, str(exc)[:80],
                        )
                        if net_streak >= NET_STREAK_LIMIT:
                            logger.warning(
                                "%s 连续 %d 个型号都是网络错误，判定**网络不可用**"
                                "（不是平台限流），提前结束本轮；"
                                "剩余任务留在队列，下一轮优先重做。"
                                "检查：Wi-Fi/热点、系统代理、代理客户端是否在跑。",
                                label, net_streak,
                            )
                            network_down = True
                            index = total
                            break
                        continue
                    continue

                if found:
                    # 先落盘再记账：顺序反了会在"落盘成功但记账前崩溃"时
                    # 丢掉这部分数据（下次恢复看不到 task 状态，但文件里有）
                    tq.append_quotes(task.task_id, code, day.isoformat(), found)
                    tq.mark_done(task.task_id, len(found))
                    fresh.extend(found)
                    empty_streak = 0
                    note_stage("有结果型号")
                    net_streak = 0        # 采到数据 = 网络是通的
                    logger.info(
                        "%s [%d/%d] %s → %d 条", label, index, total, product.model, len(found)
                    )
                elif explicit_empty:
                    # 平台明确无货 —— **不计入 empty_streak**。
                    # `empty_streak` 是"推断被限流"的证据链，而"真的没货"
                    # 恰恰不是证据。京东每轮只采 2 个型号、其中常有无货型号，
                    # 把无货算进去等于每天都在自己制造"被限流"的证据。
                    tq.mark_failed(task.task_id, "平台明确无结果")
                    note_stage("明确无结果")
                    logger.info(
                        "%s [%d/%d] %s → 0 条（平台明确无货，**不**计入限流推断）",
                        label, index, total, product.model,
                    )
                    net_streak = 0        # 页面正常，说明网络是通的
                else:
                    tq.mark_failed(task.task_id, "搜索无结果")
                    empty_streak += 1
                    note_stage("空结果型号")
                    logger.info(
                        "%s [%d/%d] %s → 0 条（连续第 %d 次空结果）",
                        label, index, total, product.model, empty_streak,
                    )
                    if empty_streak_limit and empty_streak >= empty_streak_limit:
                        logger.warning(
                            "%s 连续 %d 个型号无结果，判定已被限流，提前结束本轮；"
                            "未完成的任务留在队列里，下一轮优先重做。",
                            label, empty_streak,
                        )
                        # 这是**推断**出的限流（没命中明确特征），所以不登记 trip
                        # （证据不足，登记会把退避阶梯越推越高）。但同样不能
                        # 当成本轮成功 —— 否则 record_success() 会把计数清零。
                        suspect_throttle = True
                        index = total      # 结束整批，而不是只跳出内层
                        break

                worker.note_task()
                session_tasks += 1

                # 单会话任务上限：主动释放进程换新会话。
                # 平台风控看的是"一个会话内的行为序列长度"，会话拖太长
                # 比总时长更危险（同一套指纹 + 同一串递增行为）。
                if index < total and session_tasks >= pol.session_task_limit:
                    logger.info(
                        "%s 已达单会话任务上限 %d，主动释放浏览器进程换新会话",
                        label, pol.session_task_limit,
                    )
                    recycle_for_session = True
                    break

                # 定量 / 定时回收：到阈值就跳出内层，外层重新借页
                # （回收在**借页之前**发生，不会打断正在采的型号）
                if index < total:
                    need_recycle, why = worker.should_recycle()
                    if need_recycle:
                        logger.info(
                            "%s 触发浏览器回收：%s（已采 %d/%d）", label, why, index, total
                        )
                        break

                delay = policy.next_delay(code)
                logger.debug("%s 间隔 %.1fs（策略 μ=%.1f σ=%.1f）",
                             label, delay, pol.delay_mu, pol.delay_sigma)
                time.sleep(delay)

        # 出了 `with`：页面已归还，浏览器仍可复用
        if aborted or auth_failed is not None:
            break
        if recycle_for_session:
            recycle_for_session = False
            if index < total:
                worker.recycle("达到单会话任务上限")

    # ---- 登录失效处置（**不**进熔断阶梯）----
    if auth_failed is not None:
        # 为什么不 trip：退避是"别再去撞平台"的机制，而登录失效时我们撞的
        # 根本不是风控，是自己没登录。记进阶梯的表现就是"某源冷了几小时，
        # 其实只是没人去扫码"。改走 auth 通道：写一条「需要重新登录」的标记
        # + 一条醒目告警（这件事需要人，日志必须显眼）。
        #
        # 这里**不**销毁浏览器：会话本身没被污染，只是缺登录 Cookie；
        # 重启解决不了，还白花 3 秒。下一次 `worker.page()` 仍会走
        # `apply_session` 重新注入。
        breaker.note_auth_expired(
            code, getattr(auth_failed, "indicator", str(auth_failed)), label
        )
        logger.warning(
            "已中止本批（采到第 %d/%d 个型号），未完成任务留在队列下轮优先重做；"
            "**该源不会自动恢复**，需人工重新登录",
            index, total,
        )

    # ---- 熔断处置 ----
    if aborted and hit is not None:
        # 不传 seconds：冷却时长交给**跨轮次退避阶梯**决定
        # （连续第 N 次 → 30min / 2h / 6h / 24h 封顶）。
        # 传死值会让阶梯形同虚设 —— 上一版固定 180s，京东就是这么被连撞的。
        until = breaker.trip(code, reason=getattr(hit, "indicator", str(hit)))
        from datetime import datetime as _dt

        consecutive = breaker.consecutive_trips(code)
        entry = breaker.entry_of(code)
        severity = entry.get("severity") or breaker.classify(entry.get("reason", ""))
        cooldown = max(0.0, until - time.time())
        logger.warning("=" * 68)
        logger.warning("%s 触发限流熔断：%s", label, hit)
        logger.warning(
            "已中止本批（采到第 %d/%d 个型号），未完成任务留在队列下轮优先重做", index, total
        )
        logger.warning(
            "连续第 %d 次熔断 · %s → 退避 %s，至 %s 之前不再采集该平台"
            "（后续轮次在调度层 Fast-Fail 跳过，不会拉起浏览器）",
            consecutive,
            breaker.SEVERITY_LABEL.get(severity, severity),
            breaker.human_duration(cooldown),
            _dt.fromtimestamp(until).strftime("%Y-%m-%d %H:%M:%S"),
        )
        # 显式销毁实例，不等下次借页 —— 用户要求"换新 Session 重启"，
        # 而带限流痕迹的会话继续留着毫无价值（指纹已被标记）。
        worker.stop()
        logger.warning("=" * 68)

    # 合并已落盘的结果 —— 崩溃恢复时把上一轮采到的部分捡回来
    batch_ids = {t.task_id for t in batch}
    persisted = tq.load_quotes(code, day.isoformat(), batch_ids)
    merged = dedupe_quotes(persisted + fresh)

    if not merged and not aborted and auth_failed is None:
        logger.warning("%s 本轮 0 条报价；任务已留在队列，下一轮优先重做", label)

    # ---- 成功递减：退避阶梯余量减一级 ----
    # 判定规则见 `should_reset_backoff` —— 抽成纯函数是为了可断言，
    # 免得哪天被图省事改成 `if merged:`，退避阶梯就白设了。
    # 登录失效时同样不算"成功" —— 把 `auth_failed is not None` 并进 aborted，
    # 是为了**不改纯函数签名**（它的三个条件各有一条断言钉着）。
    if should_reset_backoff(
        len(merged), aborted or auth_failed is not None, suspect_throttle
    ):
        before = breaker.consecutive_trips(code)
        if breaker.record_success(code) and before:
            after = breaker.consecutive_trips(code)
            logger.info(
                "%s 本轮正常采到 %d 条且未触发限流，退避阶梯余量 %d → %d%s",
                label, len(merged), before, after,
                "（已归零，下次熔断从第 1 级重新开始）"
                if after == 0 else "（一次成功只减一级，余量留到下次熔断起跳）",
            )

    stages = take_stage_counts()
    logger.info(
        "%s 采集结束：%d 个型号 → %d 条报价，耗时 %.1fs%s",
        label, len(batch), len(merged), time.monotonic() - started,
        "（限流中止）" if aborted
        else ("（登录失效中止）" if auth_failed is not None
              else ("（网络故障中止）" if network_down else "")),
    )
    if stages:
        # 分段计数：出问题时一眼看出卡在哪一段。各段失败**表现都是 0 条**，
        # 但处置方式完全不同（网络没到？没就绪？接口没命中？解析失败？入库被过滤？）
        logger.info("%s 分段计数：%s", label, format_stages(stages))
    return merged
