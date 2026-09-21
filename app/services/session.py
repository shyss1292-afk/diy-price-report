"""登录态管理：保存 / 加载 / 校验各站点的浏览器会话。

原理
----
用独立 user-data-dir 启动真实 Chrome 并开放 CDP 调试端口，
Playwright 通过 `connect_over_cdp` 连上去 —— 用户在弹出的**真实窗口**里
扫码登录，脚本轮询 Cookie 判断登录是否完成，然后把 `storage_state`
落盘到 `data/sessions/<site>.json`；采集时再注入回浏览器上下文。

为什么不用 headless
------------------
1. 京东 / 淘宝会检测无头特征，headless 拿不到有效数据；
2. 扫码登录本来就需要用户看见窗口。
用「真实窗口 + CDP」是成本最低、最不容易被识别、而且不需要下载浏览器的做法。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from ..config import BASE_DIR, DATA_DIR

logger = logging.getLogger("diyprice.session")

SESSIONS_DIR = DATA_DIR / "sessions"

# 浏览器 profile 位置 —— 物理隔离的落点。
#
# 铁律：**绝不**使用日常浏览器的默认用户数据目录，也绝不复用正在
# 日常使用的浏览器实例。默认放在项目内 runtime/ 下：与系统目录、
# 日常浏览器彻底分开，便于整体备份/删除，也不会被"清理系统空间"
# 一类工具误删（放在 ~/Library/Application Support 下就有这个风险）。
RUNTIME_DIR = BASE_DIR / "runtime"
DEFAULT_PROFILE = RUNTIME_DIR / "chrome_profile"
# 旧位置（2026-09-21 之前用过）。里面只有数据，迁移靠 migrate_profile()。
LEGACY_PROFILE = Path.home() / "Library/Application Support/diyprice/browser-profile"

_env_profile = os.getenv("DIYPRICE_PROFILE_DIR", "").strip()
BROWSER_PROFILE = Path(_env_profile).expanduser() if _env_profile else DEFAULT_PROFILE


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


# 调试端口必须可配：Worker 与登录流程若各自探测不同端口，会出现
# "明明启动了却连不上"的假故障。
CDP_PORT = _env_int("DIYPRICE_CDP_PORT", 9222)

# 磁盘缓存目录 —— **外置**到系统临时目录，不留在 profile 里。
#
# 为什么必须外置：profile 是长期复用的（登录态在 Cookies 里），而磁盘缓存
# 是纯可丢弃数据。留在 profile 里就只增不减 —— 实测跑几轮后
# `Default/Cache` + `Profile 1/Cache` 合计涨到 230 MB。
#
# 与 `purge_profile_cache()` 的区别：
#   · 外置目录：**每轮启动前整个清空**（内容本来就是一次性的）
#   · profile 内的目录：只清白名单里的缓存子目录，绝不碰身份数据
# 两者是互补的双保险 —— 外置解决"涨得快"，profile 内清理解决"历史遗留"。
_env_cache = os.getenv("DIYPRICE_DISK_CACHE_DIR", "").strip()
DISK_CACHE_DIR = (
    Path(_env_cache).expanduser()
    if _env_cache
    else Path(tempfile.gettempdir()) / "diyprice-chrome-cache"
)
# 调试端口监听在哪个回环地址上并不固定：Chrome 152 实测**只监听 IPv6 回环**
# （[::1]:9222），而早期版本监听 127.0.0.1。写死任一个都会误判 —— 表现为
# 「端口明明在，却怎么都连不上」，采集据此认为浏览器已死，每小时白重启一次。
# 两个回环都探，谁通用谁。
CDP_HOSTS = ("127.0.0.1", "[::1]")
_cdp_base: str | None = None

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
]


@dataclass(frozen=True)
class SiteSpec:
    """一个站点的登录约定。"""

    code: str
    name: str
    login_url: str
    home_url: str
    # 出现这些 Cookie 即视为登录成功
    required_cookies: tuple[str, ...]
    hint: str
    # 任一命中即算登录成功。用于同一站点存在多种登录态 Cookie
    # （例如抖音可能出现 sessionid / sessionid_ss / sid_tt 中的任意组合）
    any_cookies: tuple[str, ...] = ()
    # 保存会话时只保留这些域名（及其子域）的 Cookie。
    # 见 `cookie_domains_for()` 里「为什么必须过滤」的说明。
    # 留空则由 login_url / home_url 自动推导，但涉及多域名登录的站点建议显式写。
    cookie_domains: tuple[str, ...] = ()


SITES: dict[str, SiteSpec] = {
    "jd": SiteSpec(
        code="jd",
        name="京东",
        login_url="https://passport.jd.com/new/login.aspx",
        home_url="https://www.jd.com/",
        # 实测（2026-09）：PC 网页版登录后出现的是 thor + pin，
        # 而非移动端的 pt_key / pt_pin —— 最初照老资料写，导致登录成功却检测不到。
        required_cookies=("thor", "pin"),
        hint="用京东 App 扫码，或输入账号密码登录",
        # thor/pin 会被京东广播到 30+ 个子品牌域名（jd.hk / jingxi.com / baitiao.com …），
        # 那些对 search.jd.com 没用，只留主域。实测 .jd.com 上两份 Cookie 都齐全。
        cookie_domains=("jd.com",),
    ),
    "pdd": SiteSpec(
        code="pdd",
        name="拼多多",
        login_url="https://mobile.yangkeduo.com/login.html",
        home_url="https://mobile.yangkeduo.com/",
        # 实测（2026-09）：登录后写入 PDDAccessToken + pdd_user_id
        required_cookies=("PDDAccessToken", "pdd_user_id"),
        hint="用手机号+验证码登录，或切到「扫码登录」用拼多多 App 扫码",
        cookie_domains=("yangkeduo.com", "pinduoduo.com"),
    ),
    "taobao": SiteSpec(
        code="taobao",
        name="淘宝/天猫",
        login_url="https://login.taobao.com/",
        home_url="https://www.taobao.com/",
        required_cookies=("cookie2", "_tb_token_"),
        hint="用手机淘宝扫码登录（可能需要滑块验证，按提示操作即可）",
        cookie_domains=("taobao.com", "tmall.com"),
    ),
    "goofish": SiteSpec(
        code="goofish",
        name="闲鱼",
        # passport.goofish.com 实测是空页，改为打开首页让用户点右上角「登录」
        login_url="https://www.goofish.com/",
        home_url="https://www.goofish.com/",
        # 实测（2026-09）：登录后出现 unb（淘宝用户标识）与 _m_h5_tk（mtop 签名用）
        required_cookies=("unb",),
        hint="点页面右上角的「登录」，用闲鱼 App 或淘宝 App 扫码",
        # 实测 unb / _m_h5_tk / cookie2 / _tb_token_ 四个都在 .goofish.com 上，
        # 所以不需要再带 taobao.com
        cookie_domains=("goofish.com",),
    ),
    "douyin": SiteSpec(
        code="douyin",
        name="抖音",
        # 抖音没有独立可用的登录页，直接开首页点右上角「登录」
        # （搜索页会弹登录浮层，但不适合作为起始页）
        login_url="https://www.douyin.com/",
        home_url="https://www.douyin.com/",
        # 实测（2026-09）：未登录时搜索结果区不渲染，页面直接提示
        # 「登录后即可搜索更多精彩视频」。登录后写入 sessionid 系列 Cookie，
        # 具体名字待首次登录后用 Cookie 差集确证。
        required_cookies=(),
        any_cookies=("sessionid", "sessionid_ss", "sid_tt", "uid_tt"),
        hint="点页面右上角「登录」，用抖音 App 扫码",
        cookie_domains=("douyin.com",),
    ),
    "zhuanzhuan": SiteSpec(
        code="zhuanzhuan",
        name="转转",
        login_url="https://www.zhuanzhuan.com/",
        home_url="https://www.zhuanzhuan.com/",
        required_cookies=("zz_b_uid", "zz_b_token"),
        hint="在页面右上角点击登录后用微信扫码",
        cookie_domains=("zhuanzhuan.com",),
    ),
}


# ------------------------------------------------------------------ 浏览器

def find_browser() -> str | None:
    for path in CHROME_CANDIDATES:
        if Path(path).exists():
            return path
    return None


def build_launch_args(
    binary: str,
    profile_dir: Path | str,
    port: int,
    park_window: bool = True,
    disk_cache_dir: Path | str | None = None,
) -> list[str]:
    """构造 Chromium 启动参数 —— **全项目唯一来源**。

    为什么必须只留一处：登录流程（要可见窗口）和采集 Worker（要屏幕外）
    用的是同一个浏览器，如果各自维护一套参数，迟早会漂移成"登录时用的
    是 A 浏览器、采集时用的是 B 浏览器"，从而出现"明明登录了却采不到"。

    参数分两类，目的完全不同：

    A) 物理隔离 / 不打扰用户
       独立 profile 目录（**绝不**碰日常浏览器的默认目录）、不问同步、
       不报遥测、不装扩展、不弹首次运行向导。

    B) 抗指纹底线
       **尽量像"用户自己启动的 Chrome"**。风控真正看的是 WebGL、Canvas、
       GPU 与 navigator 特征，所以这里坚决不加任何削弱渲染能力的参数
       （见下方「故意不加」的清单）。
    """
    args = [
        binary,
        f"--remote-debugging-port={port}",
        # 物理隔离的核心。也是登录态能跨实例保留的原因（profile 里有 Cookie 罐）
        f"--user-data-dir={profile_dir}",
        # ---- A) 隔离与降噪 ----
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",                   # 云同步会把 profile 关联到用户账号
        "--disable-background-networking",   # 后台遥测 / 更新检查
        "--disable-component-update",        # 组件热更新（会写 profile）
        "--disable-breakpad",                # 崩溃上报
        "--disable-client-side-phishing-detection",
        "--no-service-autorun",
        "--no-pings",
        "--password-store=basic",            # 不碰系统钥匙串，避免弹窗打断
        "--use-mock-keychain",
        "--disable-extensions",              # 禁个人扩展
        # 磁盘缓存外置到临时目录，见 DISK_CACHE_DIR 的说明。
        # 这是"防膨胀"的第一道：从源头就不让它写进 profile。
        f"--disk-cache-dir={disk_cache_dir or DISK_CACHE_DIR}",
        # Translate 是唯一会干扰采集的内置特性（弹条会改变 DOM 结构）
        "--disable-features=Translate",
    ]

    if park_window:
        # 起手就在屏幕外。短生命周期模式下窗口会被反复创建，光靠事后
        # 用 CDP 挪窗不够 —— 必须在启动参数里就定好落点。
        args += [
            f"--window-position={OFFSCREEN_X},{OFFSCREEN_Y}",
            "--window-size=1280,900",
        ]
    else:
        # 登录流程：窗口要在屏内、用户能看见二维码
        args += ["--window-size=1280,900"]

    args.append("about:blank")

    # ⚠️ 以下参数**故意不加**。它们看着像"省资源/优化"，实际会直接把
    #    指纹特征暴露给风控，属于自毁：
    #
    #   --headless / --headless=new
    #       京东/拼多多/闲鱼实测直接拦。headless 特征是最低成本的检测项
    #   --disable-gpu / --disable-software-rasterizer
    #       WebGL 拿不到，或退化成软件渲染 —— 这正是 headless 的典型特征
    #   --use-gl=swiftshader / --use-angle=swiftshader
    #       同上，且 swiftshader 的 renderer 字符串本身就是指纹
    #   --disable-blink-features=AutomationControlled
    #       这是"隐藏自动化"补丁。我们是**手动 Popen 启动的普通 Chrome**，
    #       本来就没有 navigator.webdriver，不需要补丁；加上它等于主动
    #       断言"我在做自动化"，反而可疑
    #   --enable-automation
    #       会把 navigator.webdriver 置为 true
    #   --disable-images / --blink-settings=imagesEnabled=false
    #       图片是滑块验证码的载体，禁掉等于废掉验证码
    return args


def _http_ok(url: str, timeout: float) -> bool:
    """GET 一下，状态码 200 即视为可达。"""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def cdp_alive(timeout: float = 2.0) -> bool:
    """调试端口是否可用（只探 HTTP 端点，IPv4 / IPv6 回环都试）。

    命中哪个地址会记进 `_cdp_base`，供 `cdp_url()` 复用。
    """
    global _cdp_base

    for host in CDP_HOSTS:
        base = f"http://{host}:{CDP_PORT}"
        if _http_ok(f"{base}/json/version", timeout):
            _cdp_base = base
            return True
    _cdp_base = None
    return False


def cdp_url() -> str:
    """当前可用的 CDP base URL（未探测或已失效时重新探测）。

    务必用它而不是自己拼 `127.0.0.1` —— 见 CDP_HOSTS 的说明。
    """
    if _cdp_base is None:
        cdp_alive()
    return _cdp_base or f"http://{CDP_HOSTS[0]}:{CDP_PORT}"


def cdp_usable(timeout_ms: int = 6000) -> bool:
    """CDP **真的能用吗** —— 端口活着不等于连得上。

    实测教训一（2026-09-16 夜）：Mac 睡眠后 `/json/version` 照常返回 200，
    但 WebSocket 已经废了。采集脚本只检查了 HTTP，于是以为浏览器正常，
    接着在 connect_over_cdp 上白等 3 分钟才超时 —— 夜里那几轮采集全部这么失败。
    所以这里真的连一次。

    实测教训二（2026-09-17）：调试端口可能只监听 IPv6 回环，写死 127.0.0.1
    同样会误判"浏览器已死"。地址必须通过 cdp_url() 取。
    """
    if not cdp_alive():
        return False
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # 没装 playwright 时退回浅检查
        return True
    try:
        with sync_playwright() as pw:
            pw.chromium.connect_over_cdp(cdp_url(), timeout=timeout_ms)
        return True
    except Exception as exc:  # noqa: BLE001 —— 任何连不上的情况都算不可用
        logger.warning("CDP 端口在但连接失败：%s", str(exc)[:140])
        return False


def launch_browser(wait_seconds: float = 20.0, force: bool = False, park: bool = True) -> bool:
    """确保有一个**可用的**调试浏览器（已启动且健康则直接返回 True）。

    注意：profile 目录会保留登录态，所以这是**有状态**的 ——
    删掉该目录等同于退出所有站点登录。

    force=True 时无条件重启（profile 仍保留，登录态不丢）。
    park=True 时把窗口藏起来（见 `park_browser_window`）；登录流程要传 False。
    """
    if not force and cdp_usable():
        if park:
            park_browser_window()
        return True

    if cdp_alive():
        # 端口活着但连不上 —— 必须重启，否则调用方会白等一次超时
        logger.warning("浏览器调试端口存在但不可用（常见于系统睡眠后），重启浏览器")
        close_browser()
        time.sleep(2)

    binary = find_browser()
    if binary is None:
        raise RuntimeError("未找到可用的 Chromium 内核浏览器（Chrome / Edge / Chromium / Brave）")

    BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        build_launch_args(binary, BROWSER_PROFILE, CDP_PORT, park_window=park),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # 登录流程保留 start_new_session：扫码可能要几分钟，浏览器不该
        # 随命令一起消失。
        # （采集 Worker 走另一条路 —— 见 services/browser_worker.py，那边
        #   刻意**不**用这个标志，好让浏览器随采集会话一起被回收。）
        start_new_session=True,
    )

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if cdp_alive(timeout=1.0):
            time.sleep(1.0)          # 给 CDP 端点一点启动时间
            if park:
                park_browser_window()
            return True
        time.sleep(0.5)
    return False


# 屏幕外的落点。
#
# macOS 会把窗口位置**夹住**，保证总有一部分留在屏内 —— 实测水平方向最多允许
# 40px 露出，垂直方向会把它按到底边。所以：
#   · left 设成远小于 0 → 夹到 `-(宽度-40)`，左侧只露 40px 宽的一条
#   · top  设成一个比任何屏幕都大的值 → 夹到屏幕底边，垂直方向只露约 118px
# 两个方向都超出去，屏幕上就只剩左下角一个 40×118 的小方块。
# 只压一侧的话会剩下一条 40×900 的竖边，比较碍眼。
OFFSCREEN_X = -3000
OFFSCREEN_Y = 5000
PARK_WIDTH = 1280
PARK_HEIGHT = 900


def _window_id_of(ctx):
    """拿一个可用的 windowId。

    ⚠️ 第二个返回值 `borrowed=False` 表示这个页面是**为了拿窗口而临时建的**，
    调用方**必须留着它、不能 close()**。

    原因（实测 2026-09-18）：Chrome 自动更新后会以 `--no-startup-window` 重启 ——
    此时一个窗口都没有。若这里建了页、停靠完又 `page.close()`，就等于关掉最后一个
    标签页 → **窗口随之销毁**，停靠白做。下次采集 `new_page()` 会新建一个窗口，
    落在 macOS 的层叠默认位置上（实测漂到 (-1000, 581)，屏内露出 280×469），
    用户就又能看见它了。
    留一个空白标签页把窗口"撑住"，停靠的位置才留得住。
    """
    borrowed = bool(ctx.pages)
    page = ctx.pages[0] if borrowed else ctx.new_page()
    return ctx.new_cdp_session(page).send("Browser.getWindowForTarget")["windowId"], page, borrowed


def park_browser_window() -> bool:
    """把调试浏览器窗口挪到所有显示器之外，让用户看不见。

    为什么必须处理
    --------------
    采集用的是**真实浏览器窗口**（京东/拼多多会识别无头特征，headless 拿不到数据），
    它默认出现在屏幕中间；而且采集器每轮都 `set_viewport_size({"width": 430})`
    把窗口缩成移动端尺寸。每个采集源每轮开头还会 `context.new_page()` ——
    开新标签页会让 Chrome **激活窗口**。用户于是看到一个小窗口反复冒出来。
    采集全程走 CDP，**窗口可见与否不影响功能**，所以可以安全地挪走。

    ⚠️ 不要改成「最小化」
    --------------------
    直觉上最小化更干净，但**实测行不通**：`context.new_page()` 会把最小化的窗口
    还原，而且位置被**重置到 (0, 25)** —— 也就是屏幕左上角正中间闪一下。
    三个源 × 17 轮/天 = 每天 51 次闪屏，比现在还糟。

    只挪屏幕外（保持 `normal` 状态）才是稳的：实测 `new_page()`、`goto`、
    `set_viewport_size()` 都不会改变它的位置，窗口始终留在屏幕外。

    代价：macOS 会把位置夹住、最多允许 40px 留在屏内，所以左侧会有一条
    40px 宽的窄边。把窗口高度压小（PARK_HEIGHT）可以缩短这条窄边。

    想临时关掉这个行为：`DIYPRICE_KEEP_BROWSER_HIDDEN=0`。
    """
    if os.environ.get("DIYPRICE_KEEP_BROWSER_HIDDEN", "1") == "0":
        return False
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp_url(), timeout=8000)
            ctx = browser.contexts[0]
            wid, page, borrowed = _window_id_of(ctx)
            sess = ctx.new_cdp_session(page)
            # 先恢复成 normal —— 处于 minimized 时设坐标会被忽略
            sess.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
            sess.send(
                "Browser.setWindowBounds",
                {"windowId": wid,
                 "bounds": {"left": OFFSCREEN_X, "top": OFFSCREEN_Y,
                            "width": PARK_WIDTH, "height": PARK_HEIGHT}},
            )
            if not borrowed:
                # 留一个空白标签页撑住窗口 —— 关掉它窗口会一起消失，停靠就白做了
                page.goto("about:blank")
        return True
    except Exception as exc:  # noqa: BLE001 —— 挪窗口失败不该影响采集
        logger.warning("隐藏调试浏览器窗口失败：%s", str(exc)[:120])
        return False


def park_context_window(ctx) -> bool:
    """用**调用方已有的 context** 把窗口挪回屏幕外。

    采集器在 `page.set_viewport_size(...)` 之后要调一次 —— 那是改**操作系统窗口尺寸**，
    而 macOS 会随之**重新夹取窗口位置**，实测能把停靠点从 (-1240, 932)
    漂到 (-1053, 688)，屏内可见面积从 40×118 涨到 227×362。
    每轮开头 `launch_browser()` 的停靠只保证起点正确，轮内还得按需补一次。
    """
    if os.environ.get("DIYPRICE_KEEP_BROWSER_HIDDEN", "1") == "0":
        return False
    try:
        wid, page, borrowed = _window_id_of(ctx)
        sess = ctx.new_cdp_session(page)
        sess.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
        sess.send(
            "Browser.setWindowBounds",
            {"windowId": wid,
             "bounds": {"left": OFFSCREEN_X, "top": OFFSCREEN_Y,
                        "width": PARK_WIDTH, "height": PARK_HEIGHT}},
        )
        if not borrowed:
            page.goto("about:blank")   # 同上：留着撑窗口
        return True
    except Exception as exc:  # noqa: BLE001 —— 挪窗口失败不该影响采集
        logger.warning("重新停靠浏览器窗口失败：%s", str(exc)[:120])
        return False


def park_window_of(page) -> bool:
    """把**指定页面**所在的窗口停靠到屏幕外。

    与 `park_context_window(ctx)` 的区别（很重要，踩过）：
    后者自己去 `ctx.pages[0]` 里挑页面，在短生命周期场景下可能挑到
    about:blank、甚至刚被 close 掉的僵尸页 —— 实测会让正在采集的页面
    报 `Target page, context or browser has been closed`，
    整轮采集直接归零（2026-09-21）。

    这个版本只认调用方给的页面，语义明确，不会误伤别的标签页。
    """
    if os.environ.get("DIYPRICE_KEEP_BROWSER_HIDDEN", "1") == "0":
        return False
    try:
        sess = page.context.new_cdp_session(page)
        wid = sess.send("Browser.getWindowForTarget")["windowId"]
        # 先恢复成 normal —— 处于 minimized 时设坐标会被忽略
        sess.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
        sess.send(
            "Browser.setWindowBounds",
            {
                "windowId": wid,
                "bounds": {
                    "left": OFFSCREEN_X, "top": OFFSCREEN_Y,
                    "width": PARK_WIDTH, "height": PARK_HEIGHT,
                },
            },
        )
        return True
    except Exception as exc:  # noqa: BLE001 —— 藏窗口失败不该影响采集
        logger.warning("停靠窗口失败：%s", str(exc)[:100])
        return False


def show_browser_window() -> bool:
    """还原并激活窗口 —— 登录流程要用户看见二维码，必须在屏内且未最小化。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp_url(), timeout=8000)
            ctx = browser.contexts[0]
            wid, page, borrowed = _window_id_of(ctx)
            sess = ctx.new_cdp_session(page)
            sess.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
            sess.send(
                "Browser.setWindowBounds",
                {"windowId": wid, "bounds": {"left": 120, "top": 80, "width": 1100, "height": 820}},
            )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("还原浏览器窗口失败：%s", str(exc)[:120])
        return False



def _looks_like_browser(command: str) -> bool:
    """命令行是否指向 Chromium 内核浏览器。

    为什么需要这一层：`pkill -f` / `pgrep -f` 做的是**全命令行子串匹配**，
    会命中任何碰巧含该字符串的进程。典型误伤对象：

      · 调用它的 shell —— `bash -c 'pkill -f user-data-dir=...'` 自己的
        cmdline 就含这个串，等于**把自己杀掉**
      · 正在读该路径的编辑器 / 备份工具 / 文件索引进程

    所以清理前必须再确认「这确实是个浏览器」。

    用**安装路径特征**判断，而不是 `command.split()[0]`：macOS 上的可执行
    路径本身含空格（`/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`），
    按空格切会切碎。
    """
    low = (command or "").lower()
    if not low:
        return False
    if any(candidate.lower() in low for candidate in CHROME_CANDIDATES):
        return True
    # 兜底：按**安装包根目录**匹配，而不是可执行文件路径。
    #
    # ⚠️ 这一条必不可少：Chrome 的渲染 / GPU / utility 子进程，可执行文件在
    #    `/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome
    #     Framework.framework/Versions/<ver>/Helpers/Google Chrome Helper.app/
    #     Contents/MacOS/Google Chrome Helper`
    #    —— 路径里**不含** `Contents/MacOS/Google Chrome`。
    #    只按 CHROME_CANDIDATES 匹配会把它们全判成"非浏览器"（实测一次采集
    #    漏判 215 次），后果是 `profile_rss_mb()` 只剩主进程（内存统计严重
    #    低估）、清理时也可能留下孤儿子进程。
    return any(
        token in low
        for token in (
            "/google chrome.app/",          # 覆盖主进程与所有 Helper 子进程
            "/chromium.app/",
            "/microsoft edge.app/",
            "/brave browser.app/",
            "/vivaldi.app/",
            "/opt/google/chrome/",          # Linux 官方包
            "/google-chrome",
            "/chrome-linux",
            "/chromium",
        )
    )


def profile_pids(profile_dir: Path | str) -> list[int]:
    """属于该 profile 的**浏览器**进程 PID。

    双重校验，缺一不可：

      1. 命令行含专属标记 `--user-data-dir=<profile>`
      2. 命令行指向 Chromium 内核浏览器（见 `_looks_like_browser`）

    只用第 1 条不够 —— 它是子串匹配，会命中碰巧含该串的非浏览器进程。
    第 2 条把误杀风险关掉。

    实现上刻意**不用 `pgrep -f`**（它会连自己一起匹配，也会匹配到父 shell），
    改为扫 `ps` 全表后逐条判定：目标明确，可解释，可审计。

    🚫 绝不使用模糊进程名（如 `pkill -f "Google Chrome"`）—— 那会命中
       用户**日常使用**的浏览器，等于砸掉用户正开着的窗口。
    """
    if not profile_dir:
        return []

    marker = f"--user-data-dir={profile_dir}"
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,command="],
            capture_output=True, text=True, timeout=8, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    pids: list[int] = []
    self_pid = os.getpid()
    for line in out.stdout.splitlines():
        line = line.strip()
        if marker not in line:
            continue
        pid_text, _, command = line.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == self_pid:
            continue
        if not _looks_like_browser(command):
            logger.warning(
                "跳过 PID %s：命令行含专属 profile 标记但不像浏览器，不清理 —— %s",
                pid, command[:100],
            )
            continue
        pids.append(pid)
    return pids


def profile_rss_mb(profile_dir: Path | str) -> tuple[int, float]:
    """(进程数, 合计 RSS MB)。

    Chromium 是多进程架构（browser / renderer / gpu / utility），
    只看主进程会严重低估 —— 实测主进程 77MB，但 11 个进程合计 352MB。
    """
    pids = profile_pids(profile_dir)
    if not pids:
        return 0, 0.0
    try:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", ",".join(str(p) for p in pids)],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return len(pids), 0.0
    total_kb = 0
    for token in out.stdout.split():
        try:
            total_kb += int(token)
        except ValueError:
            continue
    return len(pids), round(total_kb / 1024, 1)


# ---------------------------------------------------------------- 磁盘占用治理
#
# 目标：防止 17 轮/天跑下来把 profile 撑大。
#
# ⚠️ 实测教训（2026-09-21）：第一版清单只扫了 `Default/`，结果**漏掉了
#    159 MB** —— 大头其实在 `Profile 1/Cache`（108 MB）与
#    `Profile 1/Code Cache`（51 MB）。所以扫描必须覆盖**所有**用户数据目录
#    （`Default` + 所有 `Profile N`），不能假设数据只在 Default 下。


def _profile_data_dirs(root: Path) -> list[Path]:
    """profile 下的用户数据目录：`Default` 以及所有 `Profile N`。"""
    dirs: list[Path] = []
    default = root / "Default"
    if default.is_dir():
        dirs.append(default)
    for child in sorted(root.glob("Profile *")):
        if child.is_dir():
            dirs.append(child)
    return dirs


# 【第一层】各用户数据目录（Default / Profile N）下的纯缓存目录名。
# 渲染 / JIT / 磁盘缓存 —— 删掉只影响下次启动速度，不影响身份。
_CACHE_DIR_NAMES = (
    "Cache",
    "Code Cache",
    "GPUCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "GrShaderCache",
    "ShaderCache",
    "GraphiteDawnCache",
)

# 【第二层】profile 根目录下的纯缓存 / 可再生数据。
# 不含登录态与指纹；我们禁了后台网络，Chrome 不会自动重下。
_ROOT_CACHE_DIRS = (
    "GraphiteDawnCache",
    "GrShaderCache",
    "ShaderCache",
    "BrowserMetrics",                   # 遥测指标，纯垃圾（实测 16 MB）
    "DeferredBrowserMetrics",
    "optimization_guide_model_store",   # 优化提示模型，采集用不到（实测 40 MB）
    "WasmTtsEngine",                    # 语音合成组件，采集用不到（实测 22 MB）
    "CertificateRevocation",            # 证书吊销列表，会重建（实测 4 MB）
    "segmentation_platform",
    "component_crx_cache",
    "extensions_crx_cache",
)

# 🚫 绝对不删（承载登录态 / 指纹 / 用户数据）：
#     Default/Cookies         Default/Local Storage    Default/IndexedDB
#     Default/Preferences     Local State              Default/Session Storage
#     Default/Login Data      Default/Network          Default/History
#
# ⚠️ `Default/Extensions` 也**不动** —— 那是用户装的扩展文件。运行时已经用
#    `--disable-extensions` 禁掉加载了，但"删除文件"属于处置用户数据，
#    必须由用户决定，不能由清理逻辑代劳（实测占 104 MB）。


def _dir_size_mb(path: Path) -> float:
    total = 0
    try:
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total / 1024 / 1024


def _cache_targets(root: Path) -> list[Path]:
    """所有可安全清理的缓存目录（只保留真实存在的）。"""
    targets: list[Path] = []
    for data_dir in _profile_data_dirs(root):
        for name in _CACHE_DIR_NAMES:
            candidate = data_dir / name
            if candidate.is_dir():
                targets.append(candidate)
    for name in _ROOT_CACHE_DIRS:
        candidate = root / name
        if candidate.is_dir():
            targets.append(candidate)
    return targets


def profile_cache_mb(profile_dir: Path | str | None = None) -> float:
    """profile 下可清理缓存的合计占用（MB）—— 用于观测磁盘膨胀趋势。"""
    root = Path(profile_dir) if profile_dir else BROWSER_PROFILE
    if not root.exists():
        return 0.0
    return round(sum(_dir_size_mb(t) for t in _cache_targets(root)), 1)


def disk_cache_mb(cache_dir: Path | str | None = None) -> float:
    """外置磁盘缓存目录的体积（MB）—— 观测膨胀趋势用。"""
    root = Path(cache_dir) if cache_dir else DISK_CACHE_DIR
    if not root.exists():
        return 0.0
    return round(_dir_size_mb(root), 1)


def purge_disk_cache(
    cache_dir: Path | str | None = None, dry_run: bool = False
) -> dict:
    """清空**外置**的磁盘缓存目录。

    与外置启动参数（`--disk-cache-dir`）配套：缓存写到临时目录后，
    这里在每轮启动前整个清掉 —— 临时目录里的东西本来就是一次性的，
    可以整棵删，不用像 profile 那样挑目录。

    Returns:
        {"freed_mb", "removed", "failed", "skipped"}
    """
    root = Path(cache_dir) if cache_dir else DISK_CACHE_DIR
    result: dict = {"freed_mb": 0.0, "removed": [], "failed": [], "skipped": None}

    if not root.exists():
        result["skipped"] = "外置缓存目录不存在"
        return result

    running = profile_pids(BROWSER_PROFILE)
    if running:
        result["skipped"] = f"浏览器仍在运行（{len(running)} 个进程），跳过"
        return result

    size = _dir_size_mb(root)
    if dry_run:
        result["removed"].append(f"{root} ({size:.1f} MB)")
        result["freed_mb"] = round(size, 1)
        return result

    try:
        shutil.rmtree(root)
        result["removed"].append(str(root))
        result["freed_mb"] = round(size, 1)
    except OSError as exc:
        result["failed"].append(f"{root}: {str(exc)[:60]}")

    if result["freed_mb"]:
        logger.info("已清空外置磁盘缓存：释放 %.1f MB", result["freed_mb"])
    return result


def purge_profile_cache(
    profile_dir: Path | str | None = None, dry_run: bool = False
) -> dict:
    """清理 profile 下的纯缓存目录，防止 17 轮/天把磁盘撑大。

    为什么需要
    ----------
    短生命周期模型下每轮采集都会写渲染 / JIT 缓存，而 profile 是**长期复用**的
    （登录态在里面），缓存只增不减 —— 实测跑几轮后从 402 MB 涨到 422 MB，
    其中可清理缓存约 250 MB（含 `Profile 1/Cache` 的 108 MB）。

    为什么不用 `--disk-cache-dir` 指到 /tmp
    ----------------------------------------
    那样缓存虽脱离 profile，但 /tmp 同样会积累、清理时机不可控；更关键的是它
    属于**非默认启动参数**，与"尽量像普通用户浏览器"的抗指纹原则相冲突
    （多一个不常见 flag 就多一分特征）。主动清理 profile 内缓存更精准、更不显眼。

    安全边界
    --------
      · 只删 `_CACHE_DIR_NAMES` / `_ROOT_CACHE_DIRS` 列出的目录，其余一律不动
      · 必须在浏览器**未运行**时调用（文件被占用时删除会半途失败）
      · 不碰 Cookies / Local Storage / IndexedDB / Preferences / Extensions

    Returns:
        {"freed_mb", "removed", "failed", "skipped"}
    """
    root = Path(profile_dir) if profile_dir else BROWSER_PROFILE
    result: dict = {"freed_mb": 0.0, "removed": [], "failed": [], "skipped": None}

    if not root.exists():
        result["skipped"] = "profile 不存在"
        return result

    running = profile_pids(root)
    if running:
        result["skipped"] = f"浏览器仍在运行（{len(running)} 个进程），跳过以避免删到一半"
        return result

    for target in _cache_targets(root):
        rel = target.relative_to(root)
        size_mb = _dir_size_mb(target)
        if dry_run:
            result["removed"].append(f"{rel} ({size_mb:.1f} MB)")
            result["freed_mb"] += size_mb
            continue
        try:
            shutil.rmtree(target)
            result["removed"].append(str(rel))
            result["freed_mb"] += size_mb
        except OSError as exc:
            result["failed"].append(f"{rel}: {str(exc)[:60]}")

    result["freed_mb"] = round(result["freed_mb"], 1)
    if result["freed_mb"]:
        logger.info(
            "已清理 profile 缓存：释放 %.1f MB（%d 个目录）",
            result["freed_mb"], len(result["removed"]),
        )
    return result


def close_browser(wait_release: bool = False, timeout: float = 15.0) -> bool:
    """关闭调试浏览器（**不影响**用户自己的浏览器窗口）。

    Args:
        wait_release: 是否等所有子进程退出、内存归还系统。
            短生命周期回收建议开启 —— Chromium 主进程退出后渲染/GPU
            子进程还要几秒才散尽，不等它们就开新实例会抢 profile 锁。
    """
    if not cdp_alive() and not profile_pids(BROWSER_PROFILE):
        return False

    # 1) 优雅路径：CDP 的 /json/close
    try:
        import urllib.request

        urllib.request.urlopen(f"{cdp_url()}/json/close", timeout=2)
    except Exception:
        pass

    # 2) CDP 没有"退出整个浏览器"的 HTTP 接口，兜底用信号。
    #
    #    ⚠️ 刻意**不用 `pkill -f`**：那是全命令行子串匹配，会命中调用它的
    #       shell 自己（`bash -c 'pkill -f user-data-dir=...'` 的 cmdline 就
    #       含这个串），也可能命中用户日常浏览器。
    #       改成「先由 profile_pids 精确解析出浏览器 PID（带双重校验），
    #       再逐个 os.kill」—— 目标明确，绝不误伤。
    killed = False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        pids = profile_pids(BROWSER_PROFILE)
        if not pids:
            killed = True
            break
        for pid in pids:
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                continue
        if not wait_release:
            time.sleep(1)
            killed = True
            break
        deadline = time.time() + timeout / 2
        while time.time() < deadline:
            if not profile_pids(BROWSER_PROFILE):
                killed = True
                break
            time.sleep(0.3)

    if wait_release:
        # 等内存真正归还系统 —— 这是"杜绝内存累积"的验收点
        deadline = time.time() + timeout
        while time.time() < deadline:
            pids, _ = profile_rss_mb(BROWSER_PROFILE)
            if not pids:
                break
            time.sleep(0.3)
    return killed


def migrate_profile(target: Path | None = None) -> dict:
    """把旧位置的浏览器 profile 搬到项目内 —— **保留登录态**。

    为什么不新建一个空 profile
    -------------------------
    profile 里不只有 Cookie：还有 localStorage、IndexedDB，以及浏览器
    自己维护的渲染/GPU 缓存等状态。新建空 profile 相当于换了一台"新电脑"，
    三家平台的登录态和信任度都得从零再养一遍 —— 而它们现在都在风控敏感期
    （拼多多刚从风控里恢复）。搬过去则完全延续。

    安全前提
    --------
    必须**先关掉占用它的浏览器**。Chrome 运行时 profile 里有 SingletonLock
    和活跃的 LevelDB，直接搬会搬出一个损坏的 profile。
    """
    target = Path(target) if target else BROWSER_PROFILE
    source = LEGACY_PROFILE

    report: dict = {"source": str(source), "target": str(target), "moved": False}

    if source == target:
        report["reason"] = "新旧位置相同，无需迁移"
        return report
    if not source.exists():
        report["reason"] = "旧位置不存在，无需迁移"
        return report

    busy = profile_pids(source)
    if busy:
        report["reason"] = f"旧 profile 仍被 {len(busy)} 个进程占用，请先关闭浏览器"
        report["busy_pids"] = busy
        return report

    if target.exists():
        if any(target.iterdir()):
            report["reason"] = "目标位置已存在且非空，拒绝覆盖（请先人工确认）"
            return report
        target.rmdir()

    target.parent.mkdir(parents=True, exist_ok=True)
    size_mb = 0.0
    try:
        size_mb = round(
            sum(f.stat().st_size for f in source.rglob("*") if f.is_file()) / 1024 / 1024, 1
        )
    except OSError:
        pass

    try:
        shutil.move(str(source), str(target))
    except (OSError, shutil.Error) as exc:
        report["reason"] = f"迁移失败：{str(exc)[:120]}"
        return report

    report.update({"moved": True, "size_mb": size_mb, "reason": "迁移完成，登录态已保留"})
    logger.info("profile 已迁移：%s → %s（%.1f MB）", source, target, size_mb)
    return report


# ------------------------------------------------------------------ 会话文件

def session_path(site: str) -> Path:
    return SESSIONS_DIR / f"{site}.json"


def _registrable(host: str) -> str:
    """粗略取可注册域名（不引第三方库，够本项目用）。

    `mobile.yangkeduo.com` → `yangkeduo.com`
    `pic.zol.com.cn`       → `zol.com.cn`（处理 com.cn 这类二级后缀）
    """
    host = (host or "").strip().lower().lstrip(".")
    parts = host.split(".")
    if len(parts) >= 3 and parts[-1] in {"cn", "uk", "jp", "hk", "tw"} and len(parts[-2]) <= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def cookie_domains_for(site: str) -> tuple[str, ...]:
    """该站点保存会话时**允许保留**的域名。

    为什么必须过滤
    --------------
    三个采集源共用**同一个浏览器上下文**（都挂在 `browser.contexts[0]`），
    而 `context.storage_state()` 抓的是**整个上下文**的 Cookie ——
    不做过滤的话，每个会话文件都会塞满其他站的 Cookie。

    实测（2026-09-17）污染程度：

    | 文件 | 总 Cookie | 属于本站 |
    |---|---|---|
    | `pdd.json`      | 245 | **10** |
    | `goofish.json`  | 268 | 18 |
    | `douyin.json`   | 331 | 61 |
    | `jd.json`       | 226 | 28 |

    多余的几乎全是京东 SSO 广播到 30+ 个子品牌域名的 `thor`/`pin`。
    危害有两点：文件白胖 20 倍；`apply_session()` 每次把几百条无关 Cookie
    注回上下文，等于持续向各站暴露"这个浏览器还登着别家"。
    """
    spec = SITES.get(site)
    if spec is None:
        return ()
    if spec.cookie_domains:
        return spec.cookie_domains
    hosts = {urlparse(spec.login_url).hostname or "", urlparse(spec.home_url).hostname or ""}
    return tuple(sorted({_registrable(h) for h in hosts if h}))


def _domain_matches(domain: str, allowed: tuple[str, ...]) -> bool:
    """`domain` 是否等于 `allowed` 之一，或是其子域。"""
    d = (domain or "").strip().lower().lstrip(".")
    if not d:
        return False
    return any(d == a or d.endswith("." + a) for a in allowed)


def _is_same_domain(url: str, allowed: tuple[str, ...]) -> bool:
    try:
        return _domain_matches(urlparse(url).hostname or "", allowed)
    except ValueError:
        return False


def filter_state(site: str, storage_state: dict) -> tuple[dict, int]:
    """按站点域名过滤 storage_state，返回 (过滤后的状态, 丢弃条数)。"""
    allowed = cookie_domains_for(site)
    if not allowed or not isinstance(storage_state, dict):
        return storage_state, 0

    before = len(storage_state.get("cookies", []))
    out = dict(storage_state)
    out["cookies"] = [
        c for c in storage_state.get("cookies", []) if _domain_matches(c.get("domain", ""), allowed)
    ]
    # localStorage/origins 同理，否则会把别站的站点数据也抄进来
    out["origins"] = [
        o for o in storage_state.get("origins", []) if _is_same_domain(o.get("origin", ""), allowed)
    ]
    return out, before - len(out["cookies"])


def save_session(site: str, storage_state: dict) -> Path:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = session_path(site)
    state, dropped = filter_state(site, storage_state)
    payload = {
        "site": site,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "cookie_count": len(state.get("cookies", [])),
        "filtered_out": dropped,
        "domains": list(cookie_domains_for(site)),
        "storage_state": state,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    if dropped:
        logger.info(
            "会话 %s 已过滤掉 %s 条非本站 Cookie（保留 %s 条，域名 %s）",
            site, dropped, payload["cookie_count"], "、".join(payload["domains"]),
        )
    return path



def clean_saved_sessions(backup: bool = True) -> list[dict]:
    """把已落盘的会话文件按域名重新过滤一遍（修历史遗留的污染）。

    带回滚余地：默认先备份成 `<site>.json.bak-<时间戳>`，确认没问题再自行删除。
    """
    reports: list[dict] = []
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for site in SITES:
        path = session_path(site)
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            reports.append({"site": site, "error": str(exc)[:80]})
            continue

        state = raw.get("storage_state", {})
        before = len(state.get("cookies", []))
        state, dropped = filter_state(site, state)
        after = len(state.get("cookies", []))
        if not dropped:
            reports.append({"site": site, "before": before, "after": after, "dropped": 0})
            continue

        if backup:
            shutil.copy2(path, path.with_suffix(f".json.bak-{stamp}"))
        raw["storage_state"] = state
        raw["cookie_count"] = after
        raw["filtered_out"] = dropped
        raw["domains"] = list(cookie_domains_for(site))
        raw["cleaned_at"] = datetime.now().isoformat(timespec="seconds")
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
        reports.append({"site": site, "before": before, "after": after, "dropped": dropped})
    return reports


def load_session(site: str) -> dict | None:
    """读回 storage_state（同时兼容裸的 storage_state 格式）。"""
    path = session_path(site)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data.get("storage_state", data)


def apply_session(context, site: str) -> int:
    """把保存的登录态注入浏览器上下文，返回注入的 Cookie 条数（0 = 没注入）。

    **为什么必须做这一步**

    采集器连的是**常驻浏览器**（CDP），发请求用的是浏览器自己的 Cookie 罐；
    而 `load_session()` 读的是磁盘上的会话文件。二者会各自演化 ——
    浏览器一旦重启（Mac 睡眠、健康检查判假活、手动 close_browser），
    现场登录态就可能丢失，而会话文件仍然是"有效"的。
    此时采集会**静默返回 0 条**，看上去像被风控，实际是没登录。

    实测（2026-09-17）：只做 `load_session() is not None` 的检查时，
    拼多多被重定向到登录页、闲鱼搜索页只有 692 字符；
    注入会话 Cookie 后，闲鱼恢复为 5356 字符的正常搜索结果。

    所以：登录检查用文件判"有没有"，真正发请求前必须把文件注入浏览器。
    """
    state = load_session(site)
    if not state:
        return 0

    cookies = state.get("cookies") or []
    if not cookies:
        return 0

    try:
        context.add_cookies(cookies)
    except Exception as exc:  # noqa: BLE001 —— 注入失败不应中断采集，但要留下痕迹
        logger.warning("注入 %s 登录态失败：%s", site, str(exc)[:120])
        return 0
    return len(cookies)


def session_info(site: str) -> dict:
    path = session_path(site)
    if not path.exists():
        return {"site": site, "exists": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"site": site, "exists": True, "valid": False, "error": "会话文件损坏"}

    cookies = data.get("storage_state", {}).get("cookies", [])
    names = {c.get("name") for c in cookies}
    spec = SITES.get(site)
    if spec and spec.any_cookies:
        # 「任一命中」语义：任一存在即算已登录，否则列出全部候选供排查
        missing = [] if any(c in names for c in spec.any_cookies) else list(spec.any_cookies)
    else:
        missing = [c for c in (spec.required_cookies if spec else ()) if c not in names]
    saved_at = data.get("saved_at")
    age_days = None
    if saved_at:
        try:
            age_days = round((datetime.now() - datetime.fromisoformat(saved_at)).total_seconds() / 86400, 1)
        except ValueError:
            pass
    return {
        "site": site,
        "exists": True,
        "saved_at": saved_at,
        "age_days": age_days,
        "cookie_count": len(cookies),
        "missing_cookies": missing,
        "logged_in": not missing,
        "path": str(path),
    }


def delete_session(site: str) -> bool:
    path = session_path(site)
    if path.exists():
        path.unlink()
        return True
    return False


def _host_of(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0].split(":")[0]


def is_logged_in(context, spec: SiteSpec) -> bool:
    """判断是否登录成功。

    优先用关键 Cookie（准确）；站点若未约定 Cookie 名（首次接入、
    还不知道登录后设了哪些 Cookie），退化为「目标域名下已存在非登录页」。
    """
    names = {c.get("name") for c in context.cookies()}
    if spec.required_cookies:
        return all(c in names for c in spec.required_cookies)
    if spec.any_cookies:
        # 任一命中即可 —— 此时**不能**再退化用 URL 判断，
        # 否则会把「已打开首页但没登录」误判成登录成功
        return any(c in names for c in spec.any_cookies)

    host = _host_of(spec.login_url)
    for page in context.pages:
        url = page.url or ""
        if host not in url:
            continue
        # 只判断路径，不看 query —— 拼多多登录后会跳到
        # index.html?refer_page_name=login，query 里带 "login" 会被误判成仍在登录页
        path = url.split("?", 1)[0].lower()
        if "login" in path:
            continue
        return True
    return False


def wait_for_login(context, spec: SiteSpec, timeout: int = 300, interval: float = 3.0, on_tick=None) -> bool:
    """轮询等待用户完成登录。"""
    deadline = time.time() + timeout
    elapsed = 0
    while time.time() < deadline:
        if is_logged_in(context, spec):
            return True
        time.sleep(interval)
        elapsed += int(interval)
        if on_tick:
            on_tick(elapsed)
    return False


def clear_browser_profile() -> bool:
    """彻底清除浏览器 profile（等于退出所有站点登录）。"""
    if not BROWSER_PROFILE.exists():
        return False
    shutil.rmtree(BROWSER_PROFILE, ignore_errors=True)
    return True
