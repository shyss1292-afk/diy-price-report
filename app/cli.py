"""命令行入口。

用法：
    python -m app.cli init                    # 建表 + 写入主数据
    python -m app.cli backfill --days 180     # 回填历史行情
    python -m app.cli collect                 # 采集当天数据
    python -m app.cli aggregate               # 仅重算聚合表
    python -m app.cli serve                   # 启动 Web 服务
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .config import APP_VERSION, HOST, PORT
from .db import init_db, session_scope
from .services.bootstrap import bootstrap
from .services.pipeline import run_aggregate_only, run_pipeline


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def _own_process_group() -> bool:
    """把采集进程变成**新进程组**的组长，好让看门狗能一次干掉整棵树。

    为什么需要（2026-09-21 实测）
    ----------------------------
    17:00 那轮采集卡在 Playwright/CDP 等待里 3 小时 27 分。shell 看门狗只对
    **主进程 PID** 发信号，结果：主进程对 SIGTERM 无响应、衍生的 Chrome 与
    Playwright 的 node 驱动更是没人管，全部变成孤儿。

    进程组是内核层面的归属关系，**组内任一进程死掉都不会改变其它成员的组号**
    —— 所以 `kill -TERM -- -PGID` 能一次覆盖 python + Chrome + node 驱动全树。
    前提是它们真的在同一个组里：浏览器那边刻意用了
    `start_new_session=False`（见 browser_worker），所以会继承我们的组。

    ⚠️ 只在**非交互**场景启用。一旦脱离终端的前台进程组，Ctrl-C 就送不到
       这里了 —— 手动跑采集时那样很难受。所以交互式终端下默认跳过。

    返回是否真的新建了进程组。
    """
    mode = os.getenv("DIYPRICE_OWN_PROCESS_GROUP", "auto").strip().lower()
    if mode == "0":
        return False
    if mode != "1" and sys.stdin.isatty():
        return False
    try:
        os.setpgrp()          # setpgid(0, 0)
    except OSError:
        return False
    return True


def cmd_init(args) -> int:
    init_db()
    with session_scope() as session:
        stats = bootstrap(session)
    print(
        f"初始化完成：新增平台 {stats['platforms_added']} 个，"
        f"新增型号 {stats['products_added']} 个，"
        f"刷新别名 {stats['products_updated']} 个"
    )
    return 0


def cmd_backfill(args) -> int:
    _own_process_group()      # 回填同样会拉起浏览器，同样需要能整组收掉
    init_db()
    result = run_pipeline(
        day=None,
        sources=args.sources.split(",") if args.sources else None,
        trigger="backfill",
        backfill_days=args.days,
    )
    print(json.dumps({k: v for k, v in result.items() if k != "sources"}, ensure_ascii=False, indent=2))
    for src in result["sources"]:
        print(f"  源 {src['source']}: {src['status']} / {src['items']} 条 / 未匹配 {src['unmatched']}")
    return 0


def cmd_collect(args) -> int:
    # 自建进程组：让看门狗能一次干掉 python + Chrome + Playwright 驱动全树。
    # 必须在**任何子进程产生之前**调用（浏览器、node 驱动都要继承这个组）。
    if _own_process_group():
        logging.getLogger("diyprice.cli").info(
            "已新建进程组（PGID=%d）—— 看门狗可按组广播信号", os.getpgrp()
        )
    init_db()
    result = run_pipeline(
        sources=args.sources.split(",") if args.sources else None,
        trigger="manual",
    )
    print(f"采集完成：{result['listings']} 条明细，聚合 {result['aggregated']} 行，耗时 {result['elapsed_sec']}s")
    # 逐源打印 —— 尤其是 skipped：不打印的话"某个源今天没数据"看起来就像 bug，
    # 实际是它在熔断退避期内（这是设计行为，不是故障）。
    for src in result["sources"]:
        tag = {"skipped": "⏭ 熔断跳过"}.get(src["status"], src["status"])
        print(f"  源 {src['source']}: {tag} / {src['items']} 条")
    if result.get("skipped"):
        print(f"  （{'、'.join(result['skipped'])} 处于熔断退避期，本轮未采集；"
              f"查看：python -m app.cli breaker）")
    return 0


def cmd_probe(args) -> int:
    """探活：退避期内主动确认渠道是否已恢复。

    与 `breaker` 的分工：`breaker` 是"看状态 / 人工解除"，`probe` 是"验证"。
    探针成功会直接清掉退避、让调度立刻恢复 —— 比人工 `--clear` 可靠，
    因为它手里有真实证据（真的采到了数据），而人工判断只能靠感觉。

    ⚠️ 不带 `--source` 时**只探测处于退避期的源**，不会去碰健康的源 ——
    探针本身也是请求，没有理由去打一个本来就能采的渠道。
    """
    from .services import breaker, probe

    if args.reset_interval:
        n = probe.reset_rate_limit(args.source)
        print(f"   已清除 {args.source or '全部'} 的探针限频记录（{n} 条）")

    if args.source:
        targets = [args.source]
    else:
        targets = list(breaker.snapshot())     # 只有退避中的源

    if not targets:
        print("   没有处于熔断退避期的源，无需探活")
        return 0

    print(f"   探活目标：{'、'.join(targets)}"
          f"（最小间隔 {breaker.human_duration(probe.min_interval())}）")
    outcomes = [probe.probe(code, force=args.force, model=args.model) for code in targets]
    for outcome in outcomes:
        print("   " + outcome.line())

    if any(o.recovered for o in outcomes):
        print()
        print("   已恢复的源会在下一轮采集（或手动 collect）中正常参与。")
    return 0


def cmd_aggregate(args) -> int:
    init_db()
    n = run_aggregate_only()
    print(f"聚合完成：{n} 行")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from .main import create_app

    app = create_app(enable_scheduler=not args.no_scheduler)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_login(args) -> int:
    """打开真实浏览器让用户扫码登录，并把会话保存下来。"""
    from playwright.sync_api import sync_playwright

    from .services import session as sess

    spec = sess.SITES.get(args.site)
    if spec is None:
        print(f"未知站点：{args.site}，可选：{', '.join(sorted(sess.SITES))}")
        return 1

    print(f"正在准备浏览器（{spec.name}）…")
    # park=False：登录要用户看见窗口里的二维码/登录页，不能藏起来
    if not sess.launch_browser(park=False):
        print("❌ 无法启动浏览器，请确认已安装 Chrome / Edge / Chromium")
        return 1
    sess.show_browser_window()   # 万一上轮采集把它最小化了，这里还原到屏内
    print(f"   调试端口就绪：{sess.cdp_url()}")

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(sess.cdp_url())
        context = browser.contexts[0]
        page = context.new_page()
        try:
            page.goto(spec.login_url, timeout=30000, wait_until="domcontentloaded")
        except Exception as exc:  # 登录页结构常变，失败就退回首页让用户手动点
            print(f"⚠️ 打开登录页失败（{exc}），已改为打开首页，请手动点击右上角登录")
            page.goto(spec.home_url, timeout=30000, wait_until="domcontentloaded")

        print("")
        print("=" * 64)
        print(f"  请在刚打开的浏览器窗口中完成【{spec.name}】登录")
        print(f"  {spec.hint}")
        print(f"  脚本每 3 秒检查一次，最长等待 {args.timeout} 秒")
        print("=" * 64)
        print("", flush=True)

        def tick(sec: int) -> None:
            if sec % 30 == 0:
                print(f"  …已等待 {sec}s", flush=True)

        ok = sess.wait_for_login(context, spec, timeout=args.timeout, on_tick=tick)

        if not ok:
            print(f"❌ 超时未检测到登录（未发现关键 Cookie：{'、'.join(spec.required_cookies)}）")
            return 1

        state = context.storage_state()
        path = sess.save_session(spec.code, state)
        print(f"✅ {spec.name} 登录成功，会话已保存")
        print(f"   文件：{path}")
        print(f"   Cookie {len(state.get('cookies', []))} 个")
        print("")
        print("   后续采集会自动带上该登录态；Cookie 过期后重新执行本命令即可。")
    return 0


def cmd_sessions(args) -> int:
    from .services import session as sess

    print(f"{'站点':<14}{'状态':<12}{'Cookie':<9}{'保存时间':<22}已过天数")
    print("-" * 70)
    for code, spec in sess.SITES.items():
        info = sess.session_info(code)
        if not info["exists"]:
            print(f"{spec.name:<14}{'未登录':<12}{'—':<9}{'—':<22}—")
            continue
        state = "有效" if info.get("logged_in") else "Cookie 不全"
        print(
            f"{spec.name:<14}{state:<12}{info.get('cookie_count', 0):<9}"
            f"{info.get('saved_at') or '—':<22}{info.get('age_days', '—')}"
        )
    print("")
    print(f"浏览器 profile：{sess.BROWSER_PROFILE}")
    print(f"会话目录：{sess.SESSIONS_DIR}")
    return 0


def cmd_logout(args) -> int:
    from .services import session as sess

    if args.site == "all":
        for code, spec in sess.SITES.items():
            if sess.delete_session(code):
                print(f"  已清除 {spec.name} 会话")
        if sess.clear_browser_profile():
            print("  已清除浏览器 profile")
        return 0

    if sess.delete_session(args.site):
        print(f"已清除 {args.site} 的会话文件")
    else:
        print(f"{args.site} 没有已保存的会话")
    return 0


def cmd_sessions_clean(args) -> int:
    """按站点域名重过滤已落盘的会话文件（修历史遗留的跨站污染）。"""
    from .services import session as sess

    print("按站点域名重新过滤会话文件（会先备份为 .bak-<时间戳>）")
    print("")
    reports = sess.clean_saved_sessions(backup=not args.no_backup)
    if not reports:
        print("  没有找到任何会话文件")
        return 0
    print(f"  {'站点':<12}{'过滤前':>7}{'过滤后':>7}{'丢弃':>7}")
    print("  " + "-" * 34)
    for r in reports:
        if "error" in r:
            print(f"  {r['site']:<12}  读取失败：{r['error']}")
            continue
        print(f"  {sess.SITES[r['site']].name:<12}{r['before']:>7}{r['after']:>7}{r['dropped']:>7}")
    print("")
    print("  备份文件与原件同目录，确认无误后可自行删除。")
    print("  接下来建议跑一次采集验证登录态仍然有效。")
    return 0


def cmd_browser(args) -> int:
    """采集浏览器的运维命令：窗口控制 / 状态 / 回收 / 关闭 / profile 迁移。

    采集用的是真实浏览器窗口 —— headless 与轻量无头引擎都会被京东/拼多多/
    闲鱼直接拦截（实测见 services/browser_worker.py）。所以窗口默认挪到屏幕外；
    采集全程走 CDP，窗口可见与否不影响功能。
    """
    from .services import session as sess

    if args.migrate:
        report = sess.migrate_profile()
        print(f"   源目录  : {report['source']}")
        print(f"   目标    : {report['target']}")
        print(f"   {'✅' if report.get('moved') else '⚠️'} {report.get('reason')}")
        if report.get("size_mb"):
            print(f"   迁移体积: {report['size_mb']} MB（登录态随 profile 一起搬，无需重新扫码）")
        return 0 if report.get("moved") else 1

    from .services.browser_worker import get_worker

    worker = get_worker(reload_config=True)

    if args.status:
        st = worker.status()
        print("   采集浏览器状态")
        print(f"     受管实例     : {'运行中' if st['running'] else '未启动（按需拉起）'}")
        print(f"     profile      : {st['profile_dir']}")
        print(f"     CDP 端口     : {st['port']}")
        print(f"     进程数 / 内存 : {st['processes']} 个 / {st['rss_mb']:.0f} MB")
        if st["uptime_sec"] is not None:
            print(f"     实例存活     : {st['uptime_sec']:.0f}s（回收阈值 {st['max_seconds']:.0f}s）")
            print(f"     本轮任务数   : {st['tasks_done']}（回收阈值 {st['max_tasks']}）")
        print(f"     累计回收次数 : {st['recycles']}")
        if st["blocked"]:
            detail = "、".join(f"{k} {v}" for k, v in st["blocked"].items())
            print(f"     已拦截       : {detail}（放行 {st['allowed']}）")
        print(f"     请求瘦身     : {'开启' if worker.config.block_heavy else '关闭'}")
        print(f"     生命周期模式 : {'短生命周期（用完即关）' if worker.config.ephemeral else '常驻（回退模式）'}")
        return 0

    if args.kill:
        before_pids, before_rss = sess.profile_rss_mb(sess.BROWSER_PROFILE)
        if not before_pids:
            print("   ✅ 当前没有采集浏览器进程")
            return 0
        print(f"   关闭前：{before_pids} 个进程 / {before_rss:.0f} MB")
        worker.stop()
        after_pids, after_rss = sess.profile_rss_mb(sess.BROWSER_PROFILE)
        print(f"   关闭后：{after_pids} 个进程 / {after_rss:.0f} MB")
        print(f"   ✅ 已释放 {max(0.0, before_rss - after_rss):.0f} MB")
        return 0

    if args.recycle:
        if not sess.cdp_usable():
            print("   浏览器未运行，无需回收（跑一次采集会按需拉起）")
            return 0
        result = worker.recycle("手动触发")
        print(f"   ✅ 已回收重启，释放 {result.get('freed_mb', 0):.0f} MB")
        return 0 if result.get("restarted") else 1

    if not sess.cdp_usable():
        print("调试浏览器没在运行（或不可用）。跑一次采集或 login 会把它拉起来。")
        return 1

    if args.show:
        ok = sess.show_browser_window()
        print("✅ 窗口已还原到屏幕内" if ok else "❌ 还原失败")
    else:
        ok = sess.park_browser_window()
        print("✅ 窗口已隐藏（挪到屏幕外）" if ok else "❌ 隐藏失败")
        if ok:
            print("   采集不受影响；需要扫码登录时用：python -m app.cli browser --show")
    return 0 if ok else 1


def cmd_queue(args) -> int:
    """采集任务队列：查看状态 / 清理。

    队列是崩溃恢复与断点续爬的依据（见 services/task_queue.py）。
    """
    from .services import task_queue as tq

    if args.clear:
        n = tq.clear_finished()
        print(f"   ✅ 已清掉 {n} 个已完成 / 已结案任务")
        return 0

    if args.reset:
        n = tq.reset_running_and_pending(args.source)
        scope = f"源 {args.source}" if args.source else "全部源"
        print(f"   ✅ 已清掉 {n} 个未完成任务（{scope}）；游标不受影响")
        return 0

    s = tq.summary()
    print("   采集任务队列")
    print(f"     队列文件  : {s['queue_file']}")
    print(f"     任务总数  : {s['total']}")
    labels = {
        "pending": "待办", "running": "进行中", "done": "已完成",
        "empty": "无此型号(结案)", "failed": "失败",
        "routed": "路由排除(结案)",
    }
    for key in ("pending", "running", "done", "empty", "failed", "routed"):
        if s["by_status"].get(key):
            print(f"       {labels.get(key, key):<16} {s['by_status'][key]}")
    if s["by_source"]:
        print("     按源:")
        for src, counts in sorted(s["by_source"].items()):
            detail = "、".join(f"{labels.get(k, k)} {v}" for k, v in counts.items())
            print(f"       {src:<8} {detail}")
    if s.get("saved_at"):
        print(f"     最后更新  : {s['saved_at']}")
    waiting = tq.pending()
    if waiting:
        names = ", ".join(t.model or str(t.product_id) for t in waiting[:5])
        print(f"     待办前 5  : {names}")
    return 0


def cmd_breaker(args) -> int:
    """熔断状态查看 / 解除。

    熔断是"撞到限流后强迫降温"的机制（见 services/breaker.py）。它会主动
    **少采** —— 所以必须能一眼看到它当前的状态，否则"某个源今天没数据"
    很容易被误判成 bug。
    """
    from .collectors import policy
    from .services import breaker

    if args.clear:
        n = breaker.clear(args.source)
        target = args.source or "全部"
        print(
            f"✅ 已解除 {target} 的熔断（{n} 条），连续熔断计数一并清零，"
            f"下次熔断从阶梯第 1 级重新开始"
            if n else f"   {target} 本来就没有熔断记录"
        )
        return 0

    print("   平台策略（collectors/policy.PLATFORM_THROTTLE_CONFIG）")
    for pol in policy.PLATFORM_THROTTLE_CONFIG.values():
        print(f"     {pol.describe()}")

    print()
    ladder = breaker.backoff_ladder()
    print("   跨轮次退避阶梯（连续熔断第 N 次 → 冷却时长，末级封顶）")
    print("     " + " → ".join(
        f"第{i + 1}次 {breaker.human_duration(sec)}" for i, sec in enumerate(ladder)
    ))
    print(f"     软风控（系统繁忙/429/40001）另设上限："
          f"{breaker.human_duration(breaker.soft_cap())}"
          f" —— 平台自己忙不该让我们几小时不采")

    print()
    snap = breaker.snapshot()
    print("   当前熔断退避")
    if not snap:
        print("     （无 —— 全部平台可采）")
    else:
        for code, info in snap.items():
            pol = policy.policy_for(code)
            severity = info.get("severity") or breaker.classify(info.get("reason", ""))
            print(f"     ⛔ {pol.label:<6} 连续第 {info['trips']} 次"
                  f" · {breaker.SEVERITY_LABEL.get(severity, severity)}"
                  f" · 退避 {info['cooldown_text']}"
                  f" · 还剩 {info['remaining_text']}"
                  f"（至 {info['until_text']}）原因：{info['reason'] or '未记录'}")
        print()
        print("   退避期内该平台会在**调度层**被 Fast-Fail 跳过：")
        print("   不分配任务、不动游标、不启动浏览器，其它源照常采集。")
        print("   探活提前恢复：")
        print("     python -m app.cli probe --source jd      # 单源探活，成功即恢复调度")
        print("   手动解除：")
        print("     python -m app.cli breaker --clear            # 解除全部")
        print("     python -m app.cli breaker --clear --source jd")

    print()
    if breaker.disabled():
        print("   ⚠️ 熔断当前被 DIYPRICE_BREAKER_DISABLE=1 整体关闭")
    else:
        print("   门禁模式：Fast-Fail（退避期内直接跳过，不等待）")
        print(f"   人工等待上限：{breaker._max_inline_wait():.0f}s"
              f"（仅 wait_until_ready 使用；超过则跳过，"
              f"DIYPRICE_BREAKER_MAX_INLINE_WAIT 可调）")
        print("   阶梯覆盖：DIYPRICE_BREAKER_LADDER=1800,7200,21600,86400")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.cli", description="DIY 配件价格追踪系统命令行")
    parser.add_argument("--version", action="version", version=APP_VERSION)
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="建表并写入平台/型号主数据").set_defaults(func=cmd_init)

    p_backfill = sub.add_parser("backfill", help="回填历史行情")
    p_backfill.add_argument("--days", type=int, default=180, help="回填天数（默认 180）")
    p_backfill.add_argument("--sources", type=str, default=None, help="逗号分隔的采集源 code")
    p_backfill.set_defaults(func=cmd_backfill)

    p_collect = sub.add_parser("collect", help="采集当天数据")
    p_collect.add_argument("--sources", type=str, default=None, help="逗号分隔的采集源 code")
    p_collect.set_defaults(func=cmd_collect)

    sub.add_parser("aggregate", help="仅重算日聚合表").set_defaults(func=cmd_aggregate)

    p_serve = sub.add_parser("serve", help="启动 Web 服务")
    p_serve.add_argument("--host", default=HOST)
    p_serve.add_argument("--port", type=int, default=PORT)
    p_serve.add_argument("--no-scheduler", action="store_true", help="不启动内置调度器")
    p_serve.set_defaults(func=cmd_serve)

    from .services.session import SITES as _SESS_SITES

    p_login = sub.add_parser("login", help="打开浏览器登录站点并保存会话")
    p_login.add_argument("--site", required=True, choices=sorted(_SESS_SITES), help="站点 code")
    p_login.add_argument("--timeout", type=int, default=300, help="等待登录的秒数（默认 300）")
    p_login.set_defaults(func=cmd_login)

    sub.add_parser("sessions", help="查看各站点登录状态").set_defaults(func=cmd_sessions)

    p_sclean = sub.add_parser("sessions-clean", help="按站点域名重过滤会话文件（修跨站 Cookie 污染）")
    p_sclean.add_argument("--no-backup", action="store_true", help="不备份（默认会备份）")
    p_sclean.set_defaults(func=cmd_sessions_clean)

    p_browser = sub.add_parser("browser", help="采集浏览器：窗口控制 / 状态 / 回收 / 关闭 / 迁移")
    p_browser.add_argument("--show", action="store_true", help="还原到屏幕内（默认是隐藏）")
    p_browser.add_argument("--status", action="store_true", help="查看受管实例状态与内存占用")
    p_browser.add_argument("--recycle", action="store_true", help="回收重启（释放内存）")
    p_browser.add_argument("--kill", action="store_true", help="彻底关闭并等内存释放")
    p_browser.add_argument("--migrate", action="store_true", help="把旧 profile 迁到项目内 runtime/")
    p_browser.set_defaults(func=cmd_browser)

    p_breaker = sub.add_parser("breaker", help="熔断冷却：查看各平台策略与冷却状态")
    p_breaker.add_argument("--clear", action="store_true", help="解除冷却（默认全部）")
    p_breaker.add_argument("--source", type=str, default=None,
                           help="限定平台 code（jd / pdd / xianyu），配合 --clear")
    p_breaker.set_defaults(func=cmd_breaker)

    p_probe = sub.add_parser(
        "probe", help="探活：退避期内用一次真实请求确认渠道是否恢复（成功即恢复调度）"
    )
    p_probe.add_argument("--source", type=str, default=None,
                         help="限定平台 code；不给则探测所有处于退避期的源")
    p_probe.add_argument("--force", action="store_true",
                         help="忽略退避期与最小间隔两道闸（人工排障用，勿放进自动化）")
    p_probe.add_argument("--model", type=str, default=None, help="指定探针型号")
    p_probe.add_argument("--reset-interval", action="store_true",
                         help="先清掉限频记录再探测")
    p_probe.set_defaults(func=cmd_probe)

    p_queue = sub.add_parser("queue", help="采集任务队列：查看 / 清理")
    p_queue.add_argument("--clear", action="store_true", help="清掉已完成 / 已结案任务")
    p_queue.add_argument("--reset", action="store_true", help="清掉未完成任务（重置轮转）")
    p_queue.add_argument("--source", type=str, default=None, help="限定某个源（配合 --reset）")
    p_queue.set_defaults(func=cmd_queue)

    p_logout = sub.add_parser("logout", help="清除站点登录态")
    p_logout.add_argument("--site", default="all", help="站点 code，或 all")
    p_logout.set_defaults(func=cmd_logout)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
