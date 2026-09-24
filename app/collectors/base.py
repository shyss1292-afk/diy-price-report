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


def _next_batch(source: str, products: list, day: date, limit: int) -> list:
    """取本轮要做的任务：**优先重做队列里积压的**，不够再从游标补新的。

    这个顺序是断点续爬的关键 —— 崩在半路的任务会被优先捡起来，
    而不是等游标转一整圈（几十轮）才回头。同时队列也不会无限积压。
    """
    from ..services import task_queue as tq

    queued = tq.pending(source, day)
    if len(queued) < limit:
        need = limit - len(queued)
        fresh = pick_targets(products, need, source=source)
        if fresh:
            tq.enqueue(source, [(p.id, p.model) for p in fresh], day)
        queued = tq.pending(source, day)
    return queued[:limit]


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


def should_reset_backoff(quote_count: int, aborted: bool, suspect_throttle: bool) -> bool:
    """本轮够不够格把连续熔断计数清零（见 `breaker.record_success`）。

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


def dedupe_quotes(quotes: list[Quote]) -> list[Quote]:
    """按 (平台, 标题, 价格) 去重 —— 重试与崩溃恢复都可能带来重复。"""
    out: list[Quote] = []
    seen: set[tuple] = set()
    for q in quotes:
        key = (q.platform_code, q.title_raw, q.price)
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
    3. **成功重置**：本轮正常采到数据且没触发限流 → 连续熔断计数清零，
       下次熔断从阶梯第 1 级重新开始（见 `breaker.record_success`）。
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
    """
    from ..services import breaker
    from ..services import task_queue as tq
    from ..services.browser_worker import get_worker
    from . import policy

    label = label or getattr(collector, "name", "") or collector.code
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
    index = 0
    total = len(batch)
    session_tasks = 0
    aborted = False                       # 是否因限流熔断中止
    hit: Exception | None = None          # 命中的限流特征
    suspect_throttle = False              # 是否因"连续空结果"被判定限流
    recycle_for_session = False           # 是否因单会话上限需要换实例

    while index < total:
        session_tasks = 0                 # 每次借页 = 一个新会话，计数重置
        with worker.page(site=site, viewport=viewport, user_agent=user_agent) as page:
            while index < total:
                task = batch[index]
                index += 1
                product = by_id.get(task.product_id)
                if product is None:
                    tq.mark_failed(task.task_id, "型号已不存在")
                    continue

                tq.mark_running(task.task_id)
                try:
                    found = search_fn(page, product)
                except policy.RateLimitError as exc:
                    # ---- 熔断：不再"记一笔失败继续下一个" ----
                    tq.mark_failed(task.task_id, f"限流：{exc.indicator}"[:80])
                    aborted, hit = True, exc
                    break
                except Exception as exc:  # noqa: BLE001 —— 单个型号失败不该中断整轮
                    tq.mark_failed(task.task_id, str(exc)[:80])
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
                    continue

                if found:
                    # 先落盘再记账：顺序反了会在"落盘成功但记账前崩溃"时
                    # 丢掉这部分数据（下次恢复看不到 task 状态，但文件里有）
                    tq.append_quotes(task.task_id, code, day.isoformat(), found)
                    tq.mark_done(task.task_id, len(found))
                    fresh.extend(found)
                    empty_streak = 0
                    logger.info(
                        "%s [%d/%d] %s → %d 条", label, index, total, product.model, len(found)
                    )
                else:
                    tq.mark_failed(task.task_id, "搜索无结果")
                    empty_streak += 1
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
        if aborted:
            break
        if recycle_for_session:
            recycle_for_session = False
            if index < total:
                worker.recycle("达到单会话任务上限")

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

    if not merged and not aborted:
        logger.warning("%s 本轮 0 条报价；任务已留在队列，下一轮优先重做", label)

    # ---- 成功重置：连续熔断计数清零 ----
    # 判定规则见 `should_reset_backoff` —— 抽成纯函数是为了可断言，
    # 免得哪天被图省事改成 `if merged:`，退避阶梯就白设了。
    if should_reset_backoff(len(merged), aborted, suspect_throttle):
        if breaker.record_success(code):
            logger.info(
                "%s 本轮正常采到 %d 条且未触发限流，连续熔断计数已清零（退避阶梯重置）",
                label, len(merged),
            )

    logger.info(
        "%s 采集结束：%d 个型号 → %d 条报价，耗时 %.1fs%s",
        label, len(batch), len(merged), time.monotonic() - started,
        "（限流中止）" if aborted else "",
    )
    return merged
