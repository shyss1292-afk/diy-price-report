"""采集流水线：采集 → 归一化 → 落库 → 日聚合。

幂等保证：重跑同一天会先清空该日期 × 目标平台的明细再写入，
因此回填多少天、跑多少次，结果都一致。

事务边界（**重要，别改回一个大事务**）
--------------------------------------
一轮采集被刻意切成四段，每段各自开合事务：

    阶段 1  读主数据        session_scope 短事务，读完即关
    阶段 2  浏览器采集      **无事务**（可能 9~10 分钟）
    阶段 3  每源写入        各自 session_scope 短事务
    阶段 4  日聚合          session_scope 短事务

为什么不能合成一个：SQLite 的写锁从第一次写持有到 COMMIT。原来的写法
把采集也包在同一个事务里，等于**独占写锁十分钟**，其它写者（Web 服务、
定时脚本的汇总查询）全部 `database is locked`，`busy_timeout` 也救不了。
2026-09-21 15:00 那轮就是以退出码 1 崩在这个问题上。

拆开之后，写锁只在"真的在写"的那几百毫秒内被持有。
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time as _time
from datetime import date, datetime, time, timedelta

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from ..collectors import get_collectors
from ..config import BACKFILL_DAYS
from ..db import init_db, session_scope
from ..models import CrawlLog, Listing, Platform, PriceDaily, Product
from .aggregate import refresh_daily
from .bootstrap import bootstrap
from .clean import check as clean_check
from . import healthcheck
from .normalize import ModelMatcher

logger = logging.getLogger("diyprice.pipeline")


def _day_list(day: date, backfill_days: int) -> list[date]:
    if backfill_days and backfill_days > 1:
        start = day - timedelta(days=backfill_days - 1)
        return [start + timedelta(days=i) for i in range(backfill_days)]
    return [day]


# 采集的**硬性墙钟上限**（秒）。正常一轮 9~10 分钟（15 个闲鱼型号约 8 分钟），
# 35 分钟足够宽裕，同时低于单实例锁的陈旧阈值（40 分钟）。
_DEFAULT_WALL_CLOCK_LIMIT = 2100.0


def wall_clock_limit() -> float:
    """墙钟上限，`DIYPRICE_COLLECT_MAX_SECONDS` 可覆盖（设 0 关闭）。"""
    try:
        return max(0.0, float(os.getenv("DIYPRICE_COLLECT_MAX_SECONDS", "")
                              or _DEFAULT_WALL_CLOCK_LIMIT))
    except ValueError:
        return _DEFAULT_WALL_CLOCK_LIMIT


def _install_wall_clock_guard(limit: float) -> None:
    """硬性墙钟兜底：到点**强制结束进程**。

    为什么需要它（2026-09-21 实测）
    -------------------------------
    17:00 那轮卡在 Playwright/CDP 等待里 **3 小时 27 分**，而 shell 层的
    看门狗（`collect_scheduled.sh` 的 `kill -TERM` → 15s → `kill -KILL`）
    **没有杀掉它** —— 原因尚未定位，看门狗子 shell 执行完就退出了，
    子进程却还活着。那轮一直占着单实例锁，导致 18:00/19:00/20:00 三轮全废。

    这一层与 shell 看门狗是**双保险，不是替代**：
      · shell 看门狗负责收尾 shell 层（写汇总、释放锁）
      · 这一层保证**进程绝不会无限挂着** —— 用独立线程调 `os._exit()`，
        **不经过信号机制**，所以即使主线程卡在不可中断的系统调用里、
        或进程对 SIGTERM 无响应（实测这两个卡死进程都无响应），也照样终止。

    ⚠️ 为什么是「绝对时间戳 + 短轮询」而不是「一次长 sleep」
    -------------------------------------------------------
    2026-09-23 复盘发现：机器休眠时 **`time.sleep()` 的计时会被冻结**
    （macOS 上它基于 `mach_absolute_time`，不计入休眠时长）。
    于是 `time.sleep(35*60)` 在整夜休眠后**几乎没走**，一轮采集的墙钟耗时
    被拉到 **12~15 小时**，一直霸占单实例锁 —— 9/23 整天只跑成了 1 轮。

    改法：记下启动时刻的**绝对 Epoch 时间**（`time.time()`，它含休眠时间），
    守护线程每 5 秒醒一次比对一次。这样即使机器睡了 6 小时，
    **唤醒后第一个轮询（≤5 秒）就会发现已严重超期并立即自杀**。

    卡死时顺手 SIGKILL 掉浏览器进程：否则它们会变成孤儿一直吃内存
    （下一次启动的 `_purge_stale` 虽然也会清，但那是下一轮的事了）。
    """
    if limit <= 0:
        return

    # ⚠️ 必须用 time.time()（Epoch 墙钟，含休眠），**不能用 time.monotonic()**
    #    —— 后者在 macOS 上基于 mach_absolute_time，休眠期间同样冻结。
    started_at = _time.time()
    # 轮询周期：默认 5 秒（唤醒后最迟 5 秒内发现超期）。
    # 上限阈值很小时（如测试用 10 秒）自动收紧到 1 秒，保证"精准秒级触发"。
    poll = max(0.5, min(5.0, limit / 10.0))

    def _guard() -> None:
        while True:
            _time.sleep(poll)
            elapsed = _time.time() - started_at
            if elapsed < limit:
                continue
            human = (f"{limit / 60:.0f} 分钟" if limit >= 60 else f"{limit:.0f} 秒")
            logger.error("=" * 68)
            logger.error("采集已超过 %s 仍未结束（墙钟 %.0f 分钟，疑似卡死或期间休眠），强制终止进程",
                         human, elapsed / 60)
            logger.error("未完成的任务留在队列里，下一轮会优先重做")
            logger.error("=" * 68)
            try:
                from .session import BROWSER_PROFILE, profile_pids

                for pid in profile_pids(BROWSER_PROFILE):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
            except Exception:  # noqa: BLE001 —— 清理失败不影响强制退出
                pass
            os._exit(3)      # 硬退出：不走 atexit、不等线程

    threading.Thread(target=_guard, daemon=True, name="wall-clock-guard").start()


def _platforms_for(collector, platforms: list[Platform]) -> list[Platform]:
    supported = getattr(collector, "supported_platforms", None)
    if not supported:
        return list(platforms)
    return [p for p in platforms if p.code in supported]


# 单条 INSERT 语句最多带多少行。
# 为什么不用"一行一个"或"全部一次"：SQLite 对单语句的绑定参数个数有上限
# （旧的 SQLITE_MAX_VARIABLE_NUMBER=999，新的是 32766）。闲鱼一轮 15 个型号
# × 约 30 条 = 450 行 × 11 列 ≈ 5000 个参数，现在的新版库能过、旧版库会炸。
# 分块是廉价保险，同时让单条语句的耗时可控。
_INSERT_CHUNK = 500


def _log_skipped(source: str, trigger: str, wait_text: str) -> None:
    """记录一次"因熔断退避被跳过"（独立短事务，写完即提交）。"""
    with session_scope() as session:
        now = datetime.now()
        session.add(
            CrawlLog(
                source=source,
                trigger=trigger,
                status="skipped",
                started_at=now,
                finished_at=now,
                items=0,
                message=f"熔断退避中，剩余 {wait_text}",
            )
        )


def _open_crawl_log(source: str, trigger: str) -> int:
    """开一条 `running` 日志并**立刻提交**，返回其 id。

    为什么单独一个事务：管理页靠这行显示"进行中"。若等采集结束再写，
    一轮 9~10 分钟里管理页什么都看不到。而把它与采集塞进同一个事务，
    正是"写锁被持 10 分钟、别人写不进来"的成因 —— 所以必须快进快出。
    """
    with session_scope() as session:
        log = CrawlLog(
            source=source, trigger=trigger, status="running", started_at=datetime.now()
        )
        session.add(log)
        session.flush()
        return log.id


def _finish_crawl_log(log_id: int, items: int, errors: list[str]) -> str:
    """收尾日志（独立短事务），返回最终状态。"""
    if errors and items == 0:
        status = "failed"
    elif errors:
        status = "partial"
    else:
        status = "success"
    with session_scope() as session:
        log = session.get(CrawlLog, log_id)
        if log is not None:
            log.finished_at = datetime.now()
            log.items = items
            log.status = status
            log.message = "; ".join(errors[:5]) if errors else "采集正常"
    return status


# 超过这个时长仍是 running 的日志，判定为"开了头没收尾"。
# 必须大于采集看门狗（scripts/collect_scheduled.sh 里的 30 分钟），
# 否则会把正在跑的正常轮次误收尾（正常一轮 9~10 分钟）。
_STALE_RUNNING_MINUTES = 45


def reap_stale_crawl_logs() -> int:
    """把"开了头没收尾"的采集日志收尾成 failed，返回处理条数。

    **这是拆分事务引入的代价，必须自己收尾。**

    `_open_crawl_log()` 会**立刻提交**一条 running（不这么做管理页就看不到
    "进行中"），代价是：进程被 `kill -9` / 断电时来不及收尾，那条记录会
    **永远停在"进行中"**。而改造前整轮是一个大事务 —— 崩了整轮回滚，
    等于根本没这条记录，所以不会留下这种残迹。

    阈值取 45 分钟：远大于正常一轮（9~10 分钟）与看门狗（30 分钟），
    正常轮次绝不会被误判。
    """
    cutoff = datetime.now() - timedelta(minutes=_STALE_RUNNING_MINUTES)
    with session_scope() as session:
        rows = list(
            session.execute(
                select(CrawlLog).where(
                    CrawlLog.status == "running", CrawlLog.started_at < cutoff
                )
            ).scalars()
        )
        for row in rows:
            row.status = "failed"
            row.finished_at = datetime.now()
            row.message = "进程异常终止（开了头未收尾）—— 由后续轮次自动收尾"
    return len(rows)


def zero_yield_reason(matched: int, unmatched: int, filtered: int) -> str:
    """本轮 0 条入库时，给出**指向正确排查方向**的原因文案。

    这里区分两种"0 条"，混为一谈会把排查带偏：

      · 解析出 0 条        → 页面结构变了，或真被限流（去查风控/选择器）
      · 解析出了但全被剔除  → **数据质量问题**：型号没匹配上，或价格被判为
        离群值。平台并没有拦我们 —— 写成"疑似被限流"会让人去查风控，
        而真正该改的是匹配规则或型号基准价。

    实测踩过：搜「RTX 5090 D 32G」返回 30 条 5090 报价，全部因离群被剔除，
    旧文案却记成"疑似被限流"，把一次数据质量问题误报成了风控问题。
    """
    parsed = matched + unmatched + filtered
    if parsed:
        return (
            f"本次采集 0 条可用报价（解析出 {parsed} 条："
            f"未匹配 {unmatched} 条、被过滤 {filtered} 条）"
            f"—— 属数据质量问题，非限流"
        )
    return "本次采集 0 条（疑似被限流或页面结构变化）"


def run_pipeline(
    day: date | None = None,
    sources: list[str] | None = None,
    trigger: str = "manual",
    backfill_days: int = 0,
    on_progress=None,
) -> dict:
    """执行一次完整采集流水线。

    Args:
        day: 目标日期（默认今天）
        sources: 采集源 code 列表（默认全部启用源）
        trigger: manual | schedule | backfill
        backfill_days: 回填天数，>1 时从 day-backfill_days+1 连续采到 day
        on_progress: 可选进度回调 fn(stage:str, payload:dict)
    """
    day = day or date.today()
    days = _day_list(day, backfill_days)
    collectors = get_collectors(sources)
    if not collectors:
        raise RuntimeError("没有可用的采集源")

    def notify(stage: str, **payload) -> None:
        if on_progress:
            try:
                on_progress(stage, payload)
            except Exception:  # 进度回调异常不应中断采集
                pass

    started = _time.monotonic()
    init_db()

    # ---- 前置检查：系统代理 / 网络 ----
    # 2026-09-29 的教训：macOS 系统代理开着但代理客户端没跑，Chromium 继承了这个
    # 死代理，15 轮全部 0 条跑了一整天。在这里花几秒探一次，不通就**立刻结束本轮**
    # 并告警 —— 比启动浏览器、撞 15 次墙、浪费 2 分钟强得多。
    # ⚠️ 探的是「Chromium 实际走的路径」（读 scutil --proxy 再探那个端口），
    #    不是「curl 能不能通」—— 后者不读系统代理，会给出错误结论。
    from . import breaker as _breaker_for_preflight

    pre = healthcheck.check_preflight()
    if not pre["ok"]:
        logger.error("前置检查不通过，本轮跳过：%s", "；".join(pre["problems"]))
        return {
            "listings": 0,
            "unmatched": 0,
            "filtered": 0,
            "aggregated": 0,
            "sources": [],
            "skipped": [],
            "cooldowns": _breaker_for_preflight.snapshot(),
            "elapsed_sec": round(_time.monotonic() - started, 2),
            "preflight": pre,
            "alerts": [],
        }
    logger.info("前置检查通过（系统代理：%s）", pre["proxy"])

    # ---- 浏览器生命周期治理（短生命周期模型，见 services/browser_worker.py）----
    #   · 浏览器**按需**启动：第一个调用 worker.page() 的源负责把它拉起来，
    #     纯 mock 采集不会白启一个 Chrome
    #   · 累计任务数 / 存活时长任一超阈值 → worker 在**两次借用之间**回收重启，
    #     不会打断正在采的型号
    #   · 整轮结束在函数末尾显式 stop()，把内存还给系统
    #   · 异常与中断路径由信号钩子 + atexit 兜底，不留孤儿进程
    from .browser_worker import get_worker, install_shutdown_handlers

    install_shutdown_handlers()
    # 墙钟兜底（与 shell 看门狗双保险）—— 见 _install_wall_clock_guard
    _install_wall_clock_guard(wall_clock_limit())
    worker = get_worker()

    # 熔断状态可见性：把上一轮留下的冷却记录在轮次开头打出来 ——
    # 否则"某个源今天一直没数据"看起来像 bug，实际是它在冷却期内。
    from . import breaker

    cooling = breaker.active_summary()
    if cooling:
        logger.warning("熔断冷却中（本轮对应源会被跳过或等待）：%s", cooling)

    # 崩溃恢复：上一个进程若死在半路，队列里会留着 running 状态的任务。
    # 它们要么结果已落盘（下面 load_quotes 会捡回），要么需要重做 ——
    # 两种情况都要求先把状态退回 pending，否则会被永远卡住。
    from .task_queue import recover_stale

    rolled = recover_stale()
    if rolled:
        logger.warning("检测到 %d 个中断的采集任务，已退回队列优先重做", rolled)

    # 收尾"开了头没收尾"的日志：拆分事务后，进程被强杀会留下永远"进行中"
    # 的记录（改造前整轮回滚，不会有这种残迹）—— 见 reap_stale_crawl_logs。
    reaped = reap_stale_crawl_logs()
    if reaped:
        logger.warning("收尾了 %d 条异常中断的采集日志（原先一直显示「进行中」）", reaped)

    summary: dict = {
        "days": [d.isoformat() for d in days],
        "day_count": len(days),
        "trigger": trigger,
        "sources": [],
        "skipped": [],          # 因熔断退避被 Fast-Fail 跳过的源（本轮未采）
        "listings": 0,
        "aggregated": 0,
        "unmatched": 0,
        "filtered": 0,
    }

    # ================================================================
    # 阶段 1：读主数据（**短事务，读完立即关闭**）
    # ================================================================
    #
    # ⚠️ 这里刻意不把整个采集过程包在一个 `session_scope()` 里。
    #
    # 原来的写法是 `with session_scope() as session:` 从 bootstrap 一直包到
    # 聚合，中间夹着 9~10 分钟的浏览器采集 —— 于是 SQLite 的**写锁从第一次
    # 写（写 crawl_log）一直持有到全部结束**，任何其它写者（Web 服务、定时
    # 脚本的汇总查询）都只能干等，`busy_timeout` 也救不了（5 秒等不来 10 分钟）。
    # 2026-09-21 15:00 那轮就是以 `database is locked` + 退出码 1 崩掉的。
    #
    # 现在的边界：
    #   读主数据（短） → 采集（无事务） → 每源写入（短） → 聚合（短）
    #
    # 用 detached ORM 对象是安全的：`SessionLocal` 设了
    # `expire_on_commit=False`，会话关闭后已加载的**标量列**仍然可读
    # （下面只用到 id / model / category / aliases / base_price / code 等，
    #  不触碰 listings、dailies 这类关系属性，不会触发懒加载）。
    with session_scope() as session:
        bootstrap(session)
        session.flush()
        products = list(
            session.execute(select(Product).where(Product.is_active.is_(True))).scalars()
        )
        platforms = list(
            session.execute(
                select(Platform).where(Platform.is_active.is_(True)).order_by(Platform.sort_order)
            ).scalars()
        )
    # ← 事务在此结束，写锁已释放

    if not products or not platforms:
        raise RuntimeError("主数据为空，请先执行 init-db")

    matcher = ModelMatcher(products)
    logger.info("主数据就绪：%d 个型号 / %d 个平台 / %d 条匹配规则",
                len(products), len(platforms), matcher.rule_count)
    notify("start", products=len(products), platforms=len(platforms), days=len(days))

    for collector in collectors:
        plats = _platforms_for(collector, platforms)
        if not plats:
            continue
        plat_ids = [p.id for p in plats]

        # ---------------------------------------------------- Fast-Fail
        # 熔断退避期内**在调度层直接跳过**这个源。
        #
        # 为什么必须放在这里，而不是只靠采集器内部的门禁：
        #   · 采集器门禁在 `_next_batch()` 之前，但那时已经进了
        #     `collector.collect()` —— 调度层已经"决定要采"了。
        #     这里跳过 = **不分配任务、不动游标、不拉浏览器进程**，
        #     连一次 CDP 连接都不会发生。
        #   · 退避阶梯动辄 30 分钟到 24 小时，等待毫无意义；
        #     Fast-Fail 把这一轮的时间完整留给正常平台（如闲鱼）。
        #   · 不影响其它源：这里是 per-collector 的 `continue`，
        #     后面的源照常采集。
        ok, remaining = breaker.fast_fail(collector.code, collector.name)
        if not ok:
            wait_text = breaker.human_duration(remaining)
            _log_skipped(collector.code, trigger, wait_text)   # 独立短事务
            summary["sources"].append(
                {
                    "source": collector.code,
                    "name": collector.name,
                    "status": "skipped",
                    "items": 0,
                    "matched": 0,
                    "unmatched": 0,
                    "filtered": 0,
                    "platforms": [p.code for p in plats],
                    "errors": [f"熔断退避中，剩余 {wait_text}"],
                }
            )
            summary["skipped"].append(collector.code)
            logger.warning(
                "[%s] 本轮跳过（熔断退避剩余 %s），继续采集其它源", collector.code, wait_text
            )
            continue

        # 占位日志：独立短事务，写完立刻提交 —— 管理页能看到"进行中"，
        # 又不会把写锁一路拖到采集结束。
        log_id = _open_crawl_log(collector.code, trigger)

        items = matched = unmatched = filtered = 0
        errors: list[str] = []
        # 清洗时要拿型号基准价做离群判断
        base_by_id = {p.id: float(p.base_price or 0.0) for p in products}

        # 本次采集批次（分钟粒度）。
        #
        # 为什么不用小时：同一小时内跑第二轮采集会被当成同一批次，
        # 幂等删除就把第一轮采到的型号数据抹掉了 —— 多轮轮转采集会互相覆盖。
        # 分钟粒度下每轮是独立批次，可以逐轮累积；同一分钟内重跑仍然幂等。
        batch_id = datetime.now().strftime("%Y%m%dT%H%M")

        today = date.today()
        all_rows: list[dict] = []

        # ==============================================================
        # 阶段 2：采集 —— **全程不持有数据库事务**
        # ==============================================================
        # 这一段可能跑 9~10 分钟。原来的写法把它整个包在写事务里，于是
        # SQLite 的写锁被独占十分钟，其它写者（Web 服务、定时脚本的汇总查询）
        # 全部 `database is locked` —— busy_timeout 也救不了（5 秒等不来 10 分钟）。
        try:
            for d in days:
                captured = (
                    datetime.combine(d, time(2, 0)) if d < today else datetime.now()
                )
                rows: list[dict] = []
                for plat in plats:
                    try:
                        quotes = collector.collect(plat, products, d)
                    except Exception as exc:  # 单平台失败不影响其他平台
                        errors.append(f"{plat.code}@{d}: {exc}")
                        logger.warning("采集失败 %s %s: %s", collector.code, plat.code, exc)
                        continue

                    for q in quotes:
                        # 采集器若已知品类（如 ZOL 按品类页抓取），传入可避免跨品类误匹配
                        pid = matcher.match(q.title_raw, category=q.extra.get("category"))
                        if pid is None:
                            unmatched += 1
                            continue
                        # 剔除整机、多型号合并商品与价格离群值 ——
                        # 不起这层过滤，搜"RTX 5090"返回的 ¥817700 整机会直接撑爆价格区间
                        keep, reason = clean_check(q.price, base_by_id.get(pid), q.title_raw)
                        if not keep:
                            filtered += 1
                            logger.debug("剔除报价（%s）：%s", reason, q.title_raw[:48])
                            continue
                        matched += 1
                        rows.append(
                            {
                                "product_id": pid,
                                "platform_id": plat.id,
                                "trade_date": d,
                                "captured_at": captured,
                                "price": q.price,
                                "title_raw": q.title_raw,
                                "condition": q.condition,
                                "url": q.url,
                                "seller": q.seller,
                                # 血缘：mock 采集器写出的都是模拟值，其余为真实抓取。
                                # 报告层据此过滤，避免模拟价污染「史低价/最低平台」。
                                "is_synthetic": collector.code == "mock",
                                "batch": batch_id,
                            }
                        )

                all_rows.extend(rows)
                notify("day_done", day=d.isoformat(), source=collector.code, items=len(all_rows))
        except Exception as exc:
            # 未预期异常：把占位日志收尾成 failed，
            # 否则管理页会留一条永远"进行中"的记录
            errors.append(f"未预期异常：{exc}"[:200])
            _finish_crawl_log(log_id, items, errors)
            raise

        # ==============================================================
        # 阶段 3：写入（**独立短事务**，写完立刻提交、释放写锁）
        # ==============================================================
        #
        # ⚠️ 删除必须放在**采集成功之后**。
        # 原来是「先删后采」，结果京东被限流返回 0 条时，把当天早些时候
        # 已经采到的真实数据一起删掉了 —— 白干一场还丢数据。
        # 现在：本次颗粒无收就完全不动库里的已有数据。
        if all_rows:
            with session_scope() as session:
                # 幂等：只清空**同批次**的明细。按「日期 × 平台」全删会把同一天
                # 更早批次的价格一起删掉，日内涨跌幅就永远算不出来。
                session.execute(
                    delete(Listing).where(
                        Listing.trade_date.in_(days),
                        Listing.platform_id.in_(plat_ids),
                        Listing.batch == batch_id,
                    )
                )
                # 聚合表仍需同步清空 —— 否则明细删掉后，price_daily 会残留上次
                # 采集（可能是另一个数据源）留下的孤儿行，导致同一个格子下
                # 明细 23 条、聚合 68 行这种对不上的情况。
                session.execute(
                    delete(PriceDaily).where(
                        PriceDaily.trade_date.in_(days), PriceDaily.platform_id.in_(plat_ids)
                    )
                )
                # 分块插入：单条 INSERT 的绑定参数个数有上限（见 _INSERT_CHUNK）
                for i in range(0, len(all_rows), _INSERT_CHUNK):
                    session.execute(insert(Listing), all_rows[i : i + _INSERT_CHUNK])
            items += len(all_rows)
        else:
            reason = zero_yield_reason(matched, unmatched, filtered)
            logger.warning("%s %s，跳过写入（不清空已有数据）", collector.code, reason)
            if not errors:
                errors.append(reason)

        status = _finish_crawl_log(log_id, items, errors)

        summary["sources"].append(
            {
                "source": collector.code,
                "name": collector.name,
                "status": status,
                "items": items,
                "matched": matched,
                "unmatched": unmatched,
                "filtered": filtered,
                "platforms": [p.code for p in plats],
                "errors": errors[:5],
            }
        )
        summary["listings"] += items
        summary["unmatched"] += unmatched
        summary["filtered"] += filtered
        logger.info("[%s] %s 写入 %d 条明细（未匹配 %d 条）",
                    collector.code, status, items, unmatched)

    # ================================================================
    # 阶段 4：聚合（独立短事务）
    # ================================================================
    notify("aggregating")
    with session_scope() as session:
        summary["aggregated"] = refresh_daily(session, since=min(days))

    # ---- 采集结束：彻底关闭浏览器，等内存归还系统 ----
    # 放在所有 `session_scope()` **之外**：数据库事务此时已全部提交，
    # 关浏览器的等待（可能十几秒）不会让 SQLite 写锁多持有一秒。
    # （踩过 "database is locked"，见 2026-09-18 / 2026-09-21 记忆。）
    browser_stats = {
        "recycles": worker.recycles,
        "tasks_done": worker.tasks_done,   # 必须先取 —— stop() 会把计数归零
    }
    released = worker.stop()
    summary["browser"] = {
        "processes_before_close": released.get("before", {}).get("processes", 0),
        "rss_before_mb": released.get("before", {}).get("rss_mb", 0.0),
        "rss_after_mb": released.get("after", {}).get("rss_mb", 0.0),
        "freed_mb": released.get("freed_mb", 0.0),
        "recycles": browser_stats["recycles"],
        "tasks_done": browser_stats["tasks_done"],
    }
    if released.get("freed_mb"):
        logger.info(
            "浏览器已回收：%d 进程 / %.0f MB → %.0f MB（释放 %.0f MB）",
            released["before"]["processes"], released["before"]["rss_mb"],
            released["after"]["rss_mb"], released["freed_mb"],
        )

    # 把冷却状态带进返回值：调用方（CLI / HTTP / 定时脚本）能一眼看到
    # "这轮为什么某个源没数据"
    summary["cooldowns"] = breaker.snapshot()
    summary["elapsed_sec"] = round(_time.monotonic() - started, 2)

    # ---- 健康检查：出问题就报警 ----
    # 放在这里而不是 CLI 里：定时脚本、HTTP 触发、backfill 都会经过 run_pipeline，
    # 挂在收尾能保证**所有入口**都有告警，不会因为走的是另一个入口就静默失败。
    # （2026-09-29 全天 15 轮 0 条没人知道，就是因为告警只挂在某一个入口上。）
    # ⚠️ 它内部吞掉所有异常，绝不能因为告警把采集带崩。
    summary["alerts"] = healthcheck.check_round(summary)

    notify("done", **{"listings": summary["listings"]})
    return summary


def run_backfill(days: int = BACKFILL_DAYS, sources: list[str] | None = None, on_progress=None) -> dict:
    """回填最近 N 天历史数据（首次初始化用）。"""
    return run_pipeline(
        day=date.today(),
        sources=sources,
        trigger="backfill",
        backfill_days=days,
        on_progress=on_progress,
    )


def run_aggregate_only(since: date | None = None) -> int:
    """只重算聚合表（数据修复用）。"""
    with session_scope() as session:
        return refresh_daily(session, since=since)
