"""浏览器 Worker：物理隔离 + 短生命周期 + 内存自愈。

为什么需要这个模块
------------------
原来三个采集源都直接 `connect_over_cdp` 连一个**常驻** Chrome ——
`launch_browser()` 只在探测失败时才重启，而且用了
`start_new_session=True`，浏览器在采集脚本退出后**继续活着**。

实测（2026-09-21）常驻代价：

    主进程连续运行 2 天 18 小时
    11 个进程 / 合计 RSS 352 MB / profile 406 MB

内存不是唯一问题。Chromium 的 V8 堆长期常驻会持续膨胀，而且每个新标签页
都可能留下渲染进程残留 —— 进程数会随采集轮次单调增长，直到某次 OOM
或系统卡顿。用户的实际感受就是"后台一直在占内存、干扰正常工作"。

设计：借用 → 用够就还
--------------------
不再维护一个永久浏览器，而是把浏览器当**短生命周期 worker**：

    with worker.session():                    # 一个采集会话
        with worker.page(site="jd") as page:  # 借一页，用完还
            page.goto(...)
    # 会话结束时彻底关闭，等操作系统回收内存

在会话内部，达到任一阈值即触发**回收重启**（回收动作发生在借页之前，
所以不会打断正在进行的采集）：

    · 累计处理 N 个采集任务（默认 60）
    · 单实例存活超过 M 秒（默认 900 = 15 分钟）

为什么不能改成 headless / 轻量引擎
--------------------------------
京东 / 拼多多 / 闲鱼会检测无头特征。实测（2026-09-21）用 Lightpanda
这类轻量无头引擎，三家全部直接被拦 —— 闲鱼的原话是
「非法访问 为了保障您的体验，请使用正常浏览器访问闲鱼~」，
京东和拼多多直接跳登录页。所以必须保留**真实 Chromium 内核**。

推论：**不要**为了省内存去加 `--disable-gpu` 之类的参数。
WebGL / Canvas 指纹正是风控重点，砍掉这些等于自报家门。
见 `build_launch_args()` 里「故意不加」的那组说明。
"""
from __future__ import annotations

import atexit
import logging
import os
import signal
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .session import (
    BROWSER_PROFILE,
    build_launch_args,
    park_window_of,
    profile_pids,
    profile_rss_mb,
    purge_disk_cache,
    purge_profile_cache,
)

logger = logging.getLogger("diyprice.worker")

# 拦这两种资源类型：明确的音视频 + 字体。
#
# 为什么这两类可以放心拦：它们只影响「像素长什么样」，而我们取数是走
# DOM/JSON 的 —— 字体缺了顶多渲染成方框，innerText 与节点结构都不受影响。
# 语雀图标字体同理：图标是装饰，数据在文本里。
#
# 🚫 **不要**把 "image" 加进来 —— 滑块的背景图与缺口拼图都是 image，
#    拦掉会让验证码前端永远等不到图、直接死锁。（代码里对 image 显式放行，
#    见 should_block，这一层是额外的保险。）
_HEAVY_RESOURCE_TYPES = {"media", "font"}

# 扩展名兜底：有些资源会被 Chromium 归成 "other"/"xhr"，
# 但扩展名骗不了人。只认字体扩展名，不做通配。
_BLOCKED_EXTENSIONS = (".woff2", ".woff")

# 只拦「明知无用且高开销」的请求。判据尽量保守 —— 拦错一条就可能让
# 目标页缺组件、甚至让风控判定为异常客户端。
_BLOCKED_URL_SUBSTRINGS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "hm.baidu.com",
    "cnzz.com",
    "umeng.com",
    "umengcloud.com",
    "talkingdata.com",
    "sensorsdata.cn",
    "growingio.com",
    "hotjar.com",
    "mixpanel.com",
    "segment.io",
    "bugsnag.com",
    "sentry-cdn.com",
)

# ⚠️ 白名单：命中这些**一律放行**，优先级高于任何拦截规则。
#
# 【大厂自研安全 SDK —— 最关键的一组】
# 目标平台用的**不是**极验 / 易盾 / 顶象这类第三方验证码，而是各自自研的
# 安全 SDK 与风控链路。所以下面这组才是真正决定"验证码会不会白屏"的：
#
#   阿里系 / 闲鱼 / 淘宝：
#     punish       —— 风控惩罚页（触发风控时跳转过去）
#     rgv587       —— 惩罚页的资源前缀，拦了它页面渲染不出来，
#                     连带正文里的提示文案也拿不到（影响限流识别）
#     baxia        —— 霸下（阿里安全核心风控 SDK）
#     awsc         —— 阿里云盾 / 安全组件
#     uab          —— 阿里安全 UAB 组件
#     um.js        —— 阿里统计/安全脚本。注意它可能挂在 umeng.com / cnzz.com
#                     域名下（这两个域名在下面的打点黑名单里）——
#                     所以必须靠**白名单优先级更高**来放行，不能指望域名
#     sec.taobao.com —— 安全域
#   京东：
#     risk.jd.com   —— 风控域
#     anti.jd.com   —— 反爬域
#     blackhole     —— 京东反爬黑洞（命中即被标记）
#
# 早先只写了 geetest / yidun / dingxiang —— 那对这三个平台**基本无效**，
# 保留它们只是给其它站点留个兜底。
_SELF_HOSTED_RISK_SDK = (
    # 阿里系（闲鱼 / 淘宝 / 天猫）
    "punish",
    "rgv587",
    "baxia",
    "awsc",
    "sec.taobao.com",
    "uab",
    "um.js",
    # 京东
    "risk.jd.com",
    "anti.jd.com",
    "blackhole",
)

# 自研 SDK + 第三方验证码兜底 + 三家平台主域与 CDN
_NEVER_BLOCK_SUBSTRINGS = _SELF_HOSTED_RISK_SDK + (
    # 第三方验证码（其它站点兜底）
    "geetest",
    "yidun",
    "dingxiang",
    "captcha",
    # 阿里系静态资源
    "aliyuncs.com",
    "alicdn.com",
    "alibaba.com",
    "taobao.com",
    "tbcdn.cn",
    # 京东
    "jd.com",
    "360buyimg.com",
    # 拼多多
    "yangkeduo.com",
    "pinduoduo.com",
    "pddpic.com",
    # 闲鱼
    "goofish.com",
)


@dataclass
class WorkerConfig:
    """Worker 运行参数（全部可用环境变量覆盖）。"""

    profile_dir: Path = BROWSER_PROFILE
    port: int = 9222
    # 单实例最多处理多少个采集任务（型号级）后回收
    max_tasks: int = 60
    # 单实例最长存活秒数（到点回收，防"长期不重启"型膨胀）
    max_seconds: float = 900.0
    # 是否隐藏窗口（登录流程需要 False）
    park_window: bool = True
    # 是否启用请求瘦身
    block_heavy: bool = True
    # 是否等内存真正归还系统（关停时）
    wait_release: bool = True
    # 短生命周期开关。False = 回退到"常驻浏览器"的老行为（会话结束不关闭）。
    # 留这个开关是为了**出问题能一键回退**，不是留两条长期维护路径。
    ephemeral: bool = True

    @classmethod
    def from_env(cls) -> "WorkerConfig":
        def _int(name: str, default: int) -> int:
            try:
                return int(os.getenv(name, "") or default)
            except ValueError:
                return default

        def _float(name: str, default: float) -> float:
            try:
                return float(os.getenv(name, "") or default)
            except ValueError:
                return default

        profile = os.getenv("DIYPRICE_PROFILE_DIR", "").strip()
        return cls(
            profile_dir=Path(profile).expanduser() if profile else BROWSER_PROFILE,
            port=_int("DIYPRICE_CDP_PORT", 9222),
            max_tasks=_int("DIYPRICE_WORKER_MAX_TASKS", 60),
            max_seconds=_float("DIYPRICE_WORKER_MAX_SECONDS", 900.0),
            park_window=os.getenv("DIYPRICE_KEEP_BROWSER_HIDDEN", "1") != "0",
            block_heavy=os.getenv("DIYPRICE_BLOCK_HEAVY", "1") != "0",
            wait_release=os.getenv("DIYPRICE_WAIT_MEMORY_RELEASE", "1") != "0",
        )


# ------------------------------------------------------------------ 请求瘦身

def should_block(url: str, resource_type: str) -> bool:
    """判断单个请求是否该被拦掉。

    放行优先级从高到低（**顺序即语义，改动前先想清楚**）：

      1. **平台自研安全 SDK** / 第三方验证码 / 三家平台主域 → 必须放行
         （含 punish / rgv587 / baxia / awsc / uab / um.js 与 risk/anti/blackhole）
      2. `image` 资源 → 一律放行。滑块的背景图与缺口拼图都是 image，
         拦掉会让验证码前端等不到图、直接死锁
      3. 音视频（`media`）与字体（`font`）→ 拦。只影响渲染观感，
         不影响 DOM 取数
      4. `.woff` / `.woff2`（**扩展名**兜底，覆盖被归成 other 的字体）
      5. 显式黑名单里的站外打点域名

    宁可少拦，不可错拦 —— 拦错的代价（采集失败 / 风控标记）远大于省下的带宽。
    """
    low = (url or "").lower()

    if any(key in low for key in _NEVER_BLOCK_SUBSTRINGS):
        return False

    if resource_type == "image":
        return False

    if resource_type in _HEAVY_RESOURCE_TYPES:
        return True

    # 字体按扩展名判断（先剥掉 query / fragment）
    path = low.split("?", 1)[0].split("#", 1)[0]
    if path.endswith(_BLOCKED_EXTENSIONS):
        return True

    return any(key in low for key in _BLOCKED_URL_SUBSTRINGS)


def install_request_slimming(context, stats: dict | None = None) -> None:
    """挂上请求瘦身路由。

    **保护验证码**靠四条约束：
      1. 白名单**最高优先级**：三家平台自研安全 SDK（阿里 punish/rgv587/baxia/
         awsc/uab/um.js/sec.taobao.com，京东 risk/anti/blackhole）与第三方
         验证码域名、平台主域一律放行。注意 um.js 可能挂在 umeng.com 下，
         而该域名在黑名单里 —— 全靠这一层优先级兜住
      2. `image` **显式放行**：滑块背景图与缺口拼图都是图片，拦了会死锁
      3. 只拦 `media` / `font` 两类资源类型 + `.woff/.woff2` 扩展名兜底；
         **不碰** document / script / xhr / fetch / image / stylesheet
      4. 站外打点走**显式黑名单**，不做通配猜测

    代价：启用后所有请求都要经 Playwright 往返一次，重站点会略慢。
    如果发现风控加剧或验证码异常，用 `DIYPRICE_BLOCK_HEAVY=0` 关掉。
    """
    counters = stats if stats is not None else {}

    def _handler(route, request):  # noqa: ANN001
        try:
            rtype = request.resource_type
        except Exception:  # noqa: BLE001 —— 拿不到类型就放行，宁可多下不可错拦
            rtype = ""
        try:
            if should_block(request.url, rtype):
                counters[rtype or "other"] = counters.get(rtype or "other", 0) + 1
                route.abort()
            else:
                counters["allowed"] = counters.get("allowed", 0) + 1
                route.continue_()
        except Exception:  # noqa: BLE001 —— 路由回调绝不能抛出去
            try:
                route.continue_()
            except Exception:  # noqa: BLE001
                pass

    try:
        context.route("**/*", _handler)
    except Exception as exc:  # noqa: BLE001
        logger.warning("安装请求瘦身失败（不影响采集）：%s", str(exc)[:120])


# ------------------------------------------------------------------ Worker

class ChromiumWorker:
    """短生命周期 Chromium 管理器（Context Factory + 内存回收）。"""

    def __init__(self, config: WorkerConfig | None = None) -> None:
        self.config = config or WorkerConfig.from_env()
        self._pw = None
        self._browser = None
        self.started_at: float | None = None
        self.tasks_done = 0
        self.recycles = 0
        self.block_stats: dict = {}
        self._closing = False
        # 「实例已不可信」标记。页面崩溃（Page crashed）后浏览器状态可能
        # 已经坏了，但进程还活着 —— 靠这个标记在下一次借页前强制回收，
        # 而不是让坏实例继续污染后续几个源（2026-09-21 踩过）。
        self._dirty = False

    # ------------------------------------------------ 状态查询

    @property
    def running(self) -> bool:
        return self._browser is not None

    def status(self) -> dict:
        pids, rss = profile_rss_mb(self.config.profile_dir)
        uptime = None
        if self.started_at is not None:
            uptime = round(time.monotonic() - self.started_at, 1)
        return {
            "running": self.running,
            "profile_dir": str(self.config.profile_dir),
            "port": self.config.port,
            "uptime_sec": uptime,
            "tasks_done": self.tasks_done,
            "max_tasks": self.config.max_tasks,
            "max_seconds": self.config.max_seconds,
            "recycles": self.recycles,
            "processes": pids,
            "rss_mb": rss,
            "blocked": {k: v for k, v in self.block_stats.items() if k != "allowed"},
            "allowed": self.block_stats.get("allowed", 0),
        }

    def mark_dirty(self, reason: str = "") -> None:
        """把当前实例标记为「不可信」，下一次借页前会被回收重建。

        典型场景：页面崩溃（`Page crashed`）。此时浏览器进程还活着、CDP
        也连得上，但 context 可能已经失效 —— 实测会让后续每个源都在
        `contexts[0]` 上抛 IndexError，**整轮采集归零**。
        """
        if not self._dirty:
            logger.warning("标记浏览器实例不可信：%s", reason[:110] or "未说明原因")
        self._dirty = True

    def should_recycle(self) -> tuple[bool, str]:
        """是否该回收重启，返回 (是否, 原因)。"""
        if not self.running:
            return False, ""
        if self._dirty:
            return True, "上一次借页出现页面崩溃"
        if self.tasks_done >= self.config.max_tasks:
            return True, f"已处理 {self.tasks_done} 个任务（阈值 {self.config.max_tasks}）"
        if self.started_at is not None:
            alive = time.monotonic() - self.started_at
            if alive >= self.config.max_seconds:
                return True, f"已存活 {alive:.0f}s（阈值 {self.config.max_seconds:.0f}s）"
        return False, ""

    # ------------------------------------------------ 进程管理

    def _purge_stale(self, timeout: float = 12.0) -> int:
        """清掉占着 profile 的残留进程（上一个实例没死干净时）。

        这一步是"强制杀死残留子进程后重新初始化"的落点 —— 不做的话，
        新实例会和残留进程抢 profile 锁，表现为起不来或 profile 损坏。
        """
        pids = profile_pids(self.config.profile_dir)
        if not pids:
            return 0
        logger.warning("发现 %d 个残留进程占用 profile，先清理：%s", len(pids), pids[:6])
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for pid in pids:
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError, OSError):
                    continue
            deadline = time.time() + timeout / 2
            while time.time() < deadline:
                if not profile_pids(self.config.profile_dir):
                    return len(pids)
                time.sleep(0.3)
        return len(pids)

    def start(self) -> bool:
        """启动浏览器并等 CDP 就绪。"""
        from .session import cdp_alive, cdp_usable, find_browser

        if self.running and cdp_usable():
            return True

        binary = find_browser()
        if binary is None:
            raise RuntimeError("未找到 Chromium 内核浏览器（Chrome / Edge / Chromium / Brave）")

        # 从零开始：先放掉旧的 Playwright/浏览器句柄
        self._disconnect()

        self._purge_stale()

        # 磁盘占用治理：清掉上一轮留下的纯缓存。
        # 17 轮/天会让 Cache / Code Cache 持续增长，而 profile 是长期复用的
        # （登录态在里面），只增不减。放在这里是因为 _purge_stale() 刚保证过
        # 浏览器没在跑 —— 运行中删缓存会半途失败。
        freed = purge_profile_cache(self.config.profile_dir)
        if freed.get("freed_mb"):
            logger.info("启动前清理 profile 缓存，释放 %.1f MB", freed["freed_mb"])

        # 外置磁盘缓存同样清空（配合 --disk-cache-dir）。
        # 两处都清是**有意的双保险**：外置目录可能因为环境变量/历史遗留
        # 而不生效，那时 profile 内的清理还在兜底。
        disk = purge_disk_cache()
        if disk.get("freed_mb"):
            logger.info("启动前清空外置磁盘缓存，释放 %.1f MB", disk["freed_mb"])

        self.config.profile_dir.mkdir(parents=True, exist_ok=True)

        args = build_launch_args(
            binary, self.config.profile_dir, self.config.port, self.config.park_window
        )
        subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # 注意：这里**不**用 start_new_session —— 让浏览器留在采集进程的
            # 进程组里，这样采集脚本被 Ctrl-C / SIGTERM 干掉时浏览器不会
            # 变成无人认领的孤儿。短生命周期模型下它本来就该随会话消失。
            start_new_session=False,
        )

        deadline = time.time() + 25.0
        while time.time() < deadline:
            if cdp_alive(timeout=1.0):
                time.sleep(0.8)          # 给 CDP 端点一点启动余量
                from playwright.sync_api import sync_playwright

                if self._pw is None:
                    self._pw = sync_playwright().start()
                try:
                    self._browser = self._pw.chromium.connect_over_cdp(
                        self._cdp_url(), timeout=8000
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("CDP 连接失败，重试中：%s", str(exc)[:100])
                    time.sleep(0.5)
                    continue
                self.started_at = time.monotonic()

                # 每次启动都用 CDP 重新停靠一次窗口。
                #
                # 光靠启动参数 `--window-position` 不够：实测（2026-09-21）
                # 回收重启后窗口落在 (0, 73) —— 屏幕左上角，用户直接看得见。
                # 原因是 Chrome 会把它记住的窗口位置存在 profile 里，从已有
                # profile 启动时以 profile 为准，命令行参数被忽略。
                # 停靠是幂等操作，多调一次没有副作用。
                if self.config.park_window:
                    ctx = self._browser.contexts[0]
                    # 显式挑一个页面传进去 —— 此后各步骤都只认这一个页面
                    if ctx.pages:
                        park_window_of(ctx.pages[0])

                _, rss = profile_rss_mb(self.config.profile_dir)
                logger.info(
                    "浏览器已就绪（profile=%s，启动后 RSS %.0f MB）",
                    self.config.profile_dir, rss,
                )
                return True
            time.sleep(0.4)

        logger.error("浏览器启动超时（%.0fs 内未就绪）", 25.0)
        self._purge_stale()
        return False

    @staticmethod
    def _cdp_url() -> str:
        from .session import cdp_url

        return cdp_url()

    def _disconnect(self) -> None:
        """放掉 Playwright 句柄，但**不**杀浏览器进程。"""
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:  # noqa: BLE001
                pass
            self._browser = None
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None

    def stop(self, wait_release: bool | None = None) -> dict:
        """彻底关闭浏览器，并等操作系统回收内存。

        "彻底"是关键词：Chromium 是多进程架构，主进程退出后渲染/GPU
        子进程可能还要几秒才散尽。不等它们就走人，下一个实例会和残留
        进程抢 profile 锁。
        """
        if wait_release is None:
            wait_release = self.config.wait_release

        before_pids, before_rss = profile_rss_mb(self.config.profile_dir)

        # 1) 优雅关闭：走 CDP 的 Browser.close
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:  # noqa: BLE001
                pass
        else:
            from .session import cdp_alive

            if cdp_alive(timeout=1.0):
                try:
                    import urllib.request

                    urllib.request.urlopen(f"{self._cdp_url()}/json/close", timeout=2)
                except Exception:  # noqa: BLE001
                    pass

        # 2) 等进程自然消失
        deadline = time.time() + 12.0
        while time.time() < deadline:
            if not profile_pids(self.config.profile_dir):
                break
            time.sleep(0.4)

        # 3) 还活着就升级信号
        if profile_pids(self.config.profile_dir):
            self._purge_stale(timeout=10.0)

        self._disconnect()

        # 4) 等内存真正归还系统（可选，但这是"杜绝内存累积"的验收点）
        released_rss = None
        if wait_release:
            deadline = time.time() + 10.0
            while time.time() < deadline:
                pids, rss = profile_rss_mb(self.config.profile_dir)
                if not pids:
                    released_rss = rss
                    break
                time.sleep(0.3)
            else:
                pids, rss = profile_rss_mb(self.config.profile_dir)
                released_rss = rss
                if pids:
                    logger.warning("仍有 %d 个进程未退出，RSS 剩 %.0f MB", len(pids), rss)

        self.started_at = None
        # 实例已销毁 —— 本实例的任务计数、拦截统计与"不可信"标记全部归零，
        # 否则下一轮的阈值判断会被上一轮的累计值提前触发。
        self.tasks_done = 0
        self.block_stats = {}
        self._dirty = False
        final_pids, final_rss = profile_rss_mb(self.config.profile_dir)
        result = {
            "before": {"processes": before_pids, "rss_mb": before_rss},
            "after": {"processes": final_pids, "rss_mb": final_rss},
            "freed_mb": round(max(0.0, before_rss - final_rss), 1),
        }
        logger.info(
            "浏览器已关闭：%d 进程 / %.0f MB → %d 进程 / %.0f MB（释放 %.0f MB）",
            before_pids, before_rss, final_pids, final_rss, result["freed_mb"],
        )
        return result

    def recycle(self, reason: str = "") -> dict:
        """回收重启：关掉旧实例（释放内存）再开新的。"""
        if reason:
            logger.info("回收浏览器：%s", reason)
        # stop() 内部已把本实例的 tasks_done / block_stats 归零，这里不用重复
        result = self.stop()
        self.recycles += 1
        ok = self.start()
        result["restarted"] = ok
        result["reason"] = reason
        return result

    def note_task(self, count: int = 1) -> None:
        """记一次采集任务（型号级），供阈值判断。"""
        self.tasks_done += count

    # ------------------------------------------------ 页面借用

    def _open_page(
        self,
        site: str | None,
        viewport: dict | None,
        user_agent: str | None,
        inject_session: bool,
    ):
        """开一个页面并完成上下文准备（不含重试逻辑，重试在 page() 里）。"""
        if not self.running:
            if not self.start():
                raise RuntimeError("浏览器启动失败，无法提供页面")

        contexts = self._browser.contexts
        if not contexts:
            # 页面崩溃后 context 可能整个消失。此时直接写 contexts[0]
            # 会抛 IndexError，被上层当成"采集源失败" —— 实测三个源
            # 会一起归零（2026-09-21 京东崩溃后 pdd/xianyu 全挂）。
            raise RuntimeError("浏览器没有可用的 context（上一次可能崩过）")
        context = contexts[0]

        if inject_session and site:
            from .session import apply_session

            injected = apply_session(context, site)
            if injected:
                logger.info("%s 已注入 %d 条登录 Cookie", site, injected)
            else:
                logger.warning("%s 会话文件为空，登录态可能已失效", site)

        if self.config.block_heavy:
            install_request_slimming(context, self.block_stats)

        page = context.new_page()

        # 单标签串行监控：策略要求"禁止多 Tab 并发"时，若 context 里还有
        # 别的活动页面就告警。结构性保证在采集器侧（单线程、页面用完即还），
        # 这里只是让"约束被破坏"这件事**可见**，而不是静默并发。
        #
        # 只看非 about:blank 的页面 —— 停靠窗口时会刻意留一个空白页
        # （关掉它窗口会一起销毁），那是正常的，不该告警。
        if site:
            try:
                from ..collectors.policy import policy_for

                if not policy_for(site).allow_multi_tab:
                    others = [
                        p for p in context.pages
                        if p is not page and (getattr(p, "url", "") or "") not in ("", "about:blank")
                    ]
                    if others:
                        logger.warning(
                            "%s 策略要求单标签串行，但 context 里还有 %d 个活动页面：%s"
                            "（若是残留，会在下一次回收时清掉）",
                            site, len(others),
                            [str(getattr(p, "url", ""))[:60] for p in others[:3]],
                        )
            except Exception:  # noqa: BLE001 —— 监控失败绝不影响采集
                pass

        try:
            if viewport:
                # 设视口会改**操作系统窗口尺寸**，macOS 随之重新夹取位置，
                # 屏外坐标可能因此失效 —— 所以下面要补一次停靠
                page.set_viewport_size(viewport)
            if user_agent:
                page.set_extra_http_headers({"User-Agent": user_agent})
            if self.config.park_window:
                # 用**我们自己这个页面**停靠，不要用 park_context_window
                # —— 后者会自己去 ctx.pages[0] 挑页面，可能挑到 about:blank
                # 或刚关闭的僵尸页，实测会让采集页报
                # "Target page, context or browser has been closed"。
                park_window_of(page)
        except Exception:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
            raise
        return page

    @contextmanager
    def page(
        self,
        site: str | None = None,
        viewport: dict | None = None,
        user_agent: str | None = None,
        inject_session: bool = True,
    ):
        """借一个可用的页面，退出时自动归还（不关闭浏览器）。

        Args:
            site: 站点 code。给了就注入该站登录态（见 session.apply_session）
            viewport: 视口尺寸，如 {"width": 430, "height": 900}
            user_agent: 覆盖 UA（拼多多需要移动端 UA）
            inject_session: 是否注入登录态

        回收时机：阈值检查放在**借页之前** —— 正在采集的页面不会被半路
        掐掉，重启只发生在两次借用之间。

        失败重试：借页最多试 2 次，第一次失败就把整个实例换掉。
        短生命周期模型下重启只要约 3 秒，比让一整轮采集归零划算得多 ——
        实测京东页面崩溃会连带 pdd / xianyu 全部 `list index out of range`。
        """
        need_recycle, why = self.should_recycle()
        if need_recycle:
            self.recycle(why)

        page = None
        last_exc: Exception | None = None
        for attempt in (1, 2):
            try:
                page = self._open_page(site, viewport, user_agent, inject_session)
                break
            except Exception as exc:  # noqa: BLE001 —— 借页失败必须能重试
                last_exc = exc
                logger.warning("借页失败（第 %d/2 次）：%s", attempt, str(exc)[:110])
                self.recycle("借页失败，重建实例")

        if page is None:
            raise RuntimeError(f"无法取得可用页面：{last_exc}")

        try:
            yield page
        finally:
            gone = False
            try:
                page.close()
            except Exception:  # noqa: BLE001
                # 关不掉通常意味着页面/渲染进程已经崩了 —— 这种实例不能
                # 留给下一个源用，否则会连累整轮
                gone = True
            if gone:
                self.mark_dirty("关闭页面失败（疑似页面已崩溃）")

    @contextmanager
    def session(self):
        """采集会话：进入时按需启动，退出时**无条件彻底关闭**。

        这是短生命周期模型的入口 —— 一个 `collect` 命令 = 一个会话，
        命令结束浏览器就消失，内存归还系统。
        """
        try:
            if not self.running:
                self.start()
            yield self
        finally:
            if not self._closing:
                self.stop()


# ------------------------------------------------------------------ 全局单例

_worker: ChromiumWorker | None = None


def get_worker(reload_config: bool = False) -> ChromiumWorker:
    global _worker
    if _worker is None or reload_config:
        _worker = ChromiumWorker()
    return _worker


def shutdown(reason: str = "") -> None:
    """优雅退出：关闭浏览器并清掉孤儿进程。

    注册到 atexit + SIGTERM/SIGINT，保证 Ctrl-C、kill、launchd 停止
    脚本这三种情况都不会留下无人认领的 Chrome。
    """
    global _worker
    if _worker is None:
        return
    _worker._closing = True
    try:
        if reason:
            logger.info("收到退出信号（%s），正在关闭浏览器…", reason)
        _worker.stop()
    except Exception:  # noqa: BLE001
        pass
    finally:
        _worker = None


_handlers_installed = False


def install_shutdown_handlers() -> None:
    """安装优雅退出钩子（幂等）。"""
    global _handlers_installed
    if _handlers_installed:
        return
    _handlers_installed = True

    atexit.register(lambda: shutdown("atexit"))

    def _signal_handler(signum, _frame):  # noqa: ANN001
        name = signal.Signals(signum).name
        shutdown(name)
        # 恢复默认行为再退出，避免信号被反复处理导致卡住
        try:
            signal.signal(signum, signal.SIG_DFL)
        except Exception:  # noqa: BLE001
            pass
        os.kill(os.getpid(), signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _signal_handler)
        except (ValueError, OSError):  # 非主线程 / 不支持时忽略
            pass
