"""进程内定时任务（APScheduler）—— 只作为兜底，日常调度由 launchd 承担。

为什么把采集搬到 launchd
------------------------
轮转采集原本跑在这个进程内的 APScheduler 上，实测 Mac 睡眠后会**彻底停摆**：
2026-09-16 夜里 23:30 之后所有任务都没再触发，凌晨 02:00 的全量采集、
早上 8/9/10 点的轮转全部丢失，且 next_run 卡在过去的时间上不再推进。
launchd 是系统级调度，睡醒后会把错过的任务补跑（合并成一次），
这是进程内定时器做不到的。

现在的分工
----------
  launchd（com.diyprice.collect）  每小时一轮：浏览器健康检查 + 当日全量打底
                                   + 反爬源轮转采集（jd/pdd/xianyu，显卡+CPU）
  本模块                            仅在 DIYPRICE_SCHEDULER=1 时启用，用于
                                   兜住聚合校准这类轻量、容错的任务

服务默认以 DIYPRICE_SCHEDULER=0 启动（见 scripts/service.sh 生成的 plist）。
"""
from __future__ import annotations

import logging
from datetime import date

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .db import session_scope
from .services.aggregate import refresh_daily
from .services.pipeline import run_pipeline

logger = logging.getLogger("diyprice.scheduler")

_scheduler: BackgroundScheduler | None = None

TIMEZONE = "Asia/Shanghai"


def _job_daily_collect() -> None:
    logger.info("定时采集开始（全源全品类）")
    try:
        result = run_pipeline(day=date.today(), trigger="schedule")
        logger.info(
            "定时采集完成：%s 条明细，聚合 %s 行，耗时 %ss",
            result["listings"],
            result["aggregated"],
            result["elapsed_sec"],
        )
    except Exception:
        logger.exception("定时采集失败")

    finally:
        if prev is None:
            os.environ.pop("DIYPRICE_FOCUS_CATEGORY", None)
        else:
            os.environ["DIYPRICE_FOCUS_CATEGORY"] = prev


def _job_refresh_aggregate() -> None:
    """采集后二次校准聚合表，防止中途异常导致聚合缺失。"""
    try:
        with session_scope() as session:
            n = refresh_daily(session)
        logger.info("聚合校准完成：%s 行", n)
    except Exception:
        logger.exception("聚合校准失败")


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler

    scheduler = BackgroundScheduler(timezone=TIMEZONE)
    scheduler.add_job(
        _job_daily_collect,
        CronTrigger(hour=2, minute=0, timezone=TIMEZONE),
        id="daily_collect",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600 * 6,
    )
    # 轮转采集已移到 launchd（scripts/collect_scheduled.sh）。
    #
    # 为什么搬走：原来的轮转任务跑在进程内的 APScheduler 上，实测 Mac 睡眠后
    # 会**彻底停摆** —— 2026-09-16 夜里 23:30 之后所有任务都没再触发，
    # 凌晨 02:00 的全量采集、早上 8/9/10 点的轮转全部丢失，next_run 还卡在
    # 过去的时间上。launchd 睡醒后会把错过的任务补跑，进程内定时器做不到。
    #
    # 这里只保留凌晨两个兜底任务；服务侧通过 DIYPRICE_SCHEDULER=0 默认关闭。
    scheduler.add_job(
        _job_refresh_aggregate,
        CronTrigger(hour=3, minute=30, timezone=TIMEZONE),
        id="refresh_aggregate",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600 * 6,
    )
    scheduler.add_job(
        _job_refresh_aggregate,
        CronTrigger(hour=3, minute=30, timezone=TIMEZONE),
        id="refresh_aggregate",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600 * 6,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info(
        "调度器已启动：每日 02:00 全源采集 / 03:30 聚合校准"
        "（轮转采集由 launchd 托管，见 scripts/collect_scheduled.sh）"
    )
    return scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("调度器已停止")
    _scheduler = None


def job_status() -> list[dict]:
    if _scheduler is None or not _scheduler.running:
        return []
    return [
        {
            "id": job.id,
            "next_run": job.next_run_time.isoformat(sep=" ", timespec="seconds")
            if job.next_run_time
            else None,
        }
        for job in _scheduler.get_jobs()
    ]
