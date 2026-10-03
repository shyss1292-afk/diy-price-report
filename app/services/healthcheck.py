"""轮次健康检查：判断「这轮是不是出问题了」，出问题就调 `alerting` 报警。

分工
----
· `alerting`  —— 通道层（怎么把消息发出去、怎么抑制重复）
· `healthcheck`（本模块）—— 规则层（什么情况算问题）

规则
----
1. **连续 N 轮 0 条** → critical
   2026-09-29 那次就是 15 轮全 0 条。单轮 0 条其实很常见（反爬源本来就时好时坏），
   所以默认阈值 1 轮就报，但**靠 alerting 的抑制窗口**避免刷屏。
2. **某个源连续 N 轮失败** → warn（默认 3 轮）
   注意 `skipped`（熔断退避）**不算失败** —— 那是设计行为，不是故障。
   把设计行为当故障报警，会让人对告警脱敏，比不报还糟。
3. **全部源都没产出**（全 failed / 全 skipped）→ critical
4. **型号 N 天没采到** → warn（默认 7 天，需要 DB，单独按天跑）

状态
----
`data/health_state.json` 记录「连续 0 条轮数」「各源连续失败轮数」，
跨进程重启有效。
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path

from . import alerting

logger = logging.getLogger("diyprice.health")

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
STATE_FILE = DATA_DIR / "health_state.json"

# 源状态里哪些算「失败」。skipped 不算 —— 熔断退避是设计行为。
FAIL_STATUSES = {"failed"}
OK_STATUSES = {"success", "partial"}

# 超过这么久没跑过轮次，连续计数视为过期（避免恢复后第一轮误报）
STALE_HOURS = 6


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("健康状态写入失败：%s", e)


# 熔断原因里出现这些 → 是**登录态失效**，不是限流。
# 区分很重要：限流「等一会儿」会好，登录失效**必须重新扫码**，
# 等多久都没用 —— 退避阶梯只会让它越等越久（PDD 就这样卡了一整天）。
LOGIN_EXPIRED_MARKERS = ("login.html", "passport", "请登录", "未登录", "登录失效")


def evaluate_login_expired(cooldowns: dict) -> list[dict]:
    """**纯函数**：从熔断状态里识别「登录态失效」，返回该报的警。

    ⚠️ 为什么必须和限流分开：熔断器只看「命中限流特征」，
       而 `login.html` 既是它的限流特征、也是登录失效的证据。
       两者被混为一谈的后果（2026-10-01 实测）：
         PDD 跳 login.html → 熔断 1 天 → 到期重试 → 还是 login.html
         → 熔断更久 → 无限循环，永远采不到，而且**看起来像在正常退避**。
       所以这里要单独识别，明确告诉用户「去重新扫码」。
    """
    alerts: list[dict] = []
    # ⚠️ `breaker.snapshot()` 返回的是**扁平**结构：{源code: {reason, trips, ...}}
    #    （不是嵌套在 "sources" 下 —— 那只是磁盘文件 breaker.json 的形状）
    sources = (cooldowns or {}).get("sources")
    if not isinstance(sources, dict) or not sources:
        # 兼容两种形状：扁平 {pdd: {...}} / 嵌套 {sources: {pdd: {...}}}
        sources = {
            k: v for k, v in (cooldowns or {}).items()
            if isinstance(v, dict) and ("reason" in v or "trips" in v)
        }
    for code, entry in sources.items():
        reason = str((entry or {}).get("reason") or "")
        if any(m.lower() in reason.lower() for m in LOGIN_EXPIRED_MARKERS):
            trips = entry.get("trips")
            remaining = entry.get("remaining_text") or ""
            alerts.append({
                "level": "critical",
                "key": f"login-expired:{code}",
                "title": f"{code} 登录态失效，需要重新扫码",
                "body": (
                    f"熔断原因「{reason}」= 平台把请求跳到了登录页，**不是限流**。"
                    f"等待退避没有用（已连续第 {trips} 次，当前退避还剩 {remaining}）。"
                    f"请执行：python -m app.cli login --site {code}"
                ),
            })
    return alerts


def evaluate_round(
    summary: dict,
    zero_streak: int,
    fails: dict,
    rules: dict,
) -> tuple[list[dict], dict, dict]:
    """**纯函数**：根据本轮结果算出「该报哪些警」和「新的计数」。

    把判定逻辑从 `check_round` 里抽出来，是为了能**直接断言**——
    自检不该为了测一条规则去发真实通知、写真实状态文件。

    Returns:
        (alerts, new_zero_streak, new_fails)

    ⚠️ 计数语义（踩过，别改错）：
      · `failed`  → 计数 +1
      · `success` 且 items > 0 → 归零（**真采到数据才算恢复**）
      · `skipped`（熔断退避）→ **不动**。既不 +1 也不归零：
        - 不 +1：熔断是设计行为，不是新的失败
        - 不归零：熔断恰恰是"撞到风控"的证据，归零会把问题掩盖掉
    """
    sources = summary.get("sources", []) or []
    listings = int(summary.get("listings", 0) or 0)

    zero_streak = (zero_streak + 1) if listings == 0 else 0

    new_fails = dict(fails)
    for s in sources:
        code, status = s.get("source", "?"), s.get("status", "")
        if status in FAIL_STATUSES:
            new_fails[code] = int(new_fails.get(code, 0)) + 1
        elif status in OK_STATUSES and int(s.get("items", 0) or 0) > 0:
            new_fails[code] = 0

    alerts: list[dict] = []

    # ---- 规则 1：连续 N 轮 0 条 ----
    threshold = int(rules.get("zero_yield_rounds", 1) or 1)
    if listings == 0 and zero_streak >= threshold:
        detail = "；".join(
            f"{s.get('source')}={s.get('status')}({s.get('items', 0)}条)" for s in sources
        ) or "无源"
        # 说清「连续几轮」，不然第 1 轮和第 10 轮长得一样
        alerts.append({
            "level": "critical",
            "key": "zero-yield",
            "title": f"采集连续 {zero_streak} 轮 0 条",
            "body": f"本轮 {zero_streak} 轮无产出。{detail}",
        })

    # ---- 规则 2：单源连续 N 轮失败 ----
    sf = int(rules.get("source_fail_rounds", 3) or 3)
    for code, n in sorted(new_fails.items()):
        if n >= sf:
            alerts.append({
                "level": "warn",
                "key": f"source-fail:{code}",
                "title": f"{code} 连续 {n} 轮采集失败",
                "body": f"该源已连续 {n} 轮没有任何成功产出，检查登录态/风控/页面结构。",
            })

    # ---- 规则 3：全部源都没产出 ----
    active = [s for s in sources if s.get("status") != "skipped"]
    if sources and not active:
        skipped = "、".join(s.get("source", "?") for s in sources)
        alerts.append({
            "level": "critical",
            "key": "all-sources-down",
            "title": "所有采集源都不可用",
            "body": f"全部源处于熔断退避或无产出：{skipped}。检查网络与登录态。",
        })

    return alerts, zero_streak, new_fails


def check_round(summary: dict) -> list[dict]:
    """检查一轮采集结果，必要时报警。返回触发的告警列表。

    ⚠️ 这个函数**绝不抛异常** —— 它在轮次收尾被调用，把采集带崩就本末倒置了。
    """
    try:
        cfg = alerting.load_config()
        rules = cfg.get("rules", {}) or {}
        state = _load_state()

        # ---- 陈旧计数保护 ----
        # 机器关机/停跑一段时间后，`source_fails` 里可能留着几天前的计数。
        # 若直接沿用，恢复后的第一轮就会误报「连续 N 轮失败」——
        # 那个"连续"其实横跨了一个断档，不成立。
        # 所以超过 STALE_HOURS 没跑过轮次，就把连续计数当过期清掉。
        last_at = (state.get("last_round") or {}).get("at", "")
        if last_at:
            try:
                import time as _t
                last_ts = _t.mktime(_t.strptime(last_at, "%Y-%m-%d %H:%M:%S"))
                if (_t.time() - last_ts) > STALE_HOURS * 3600:
                    logger.info("上次轮次距今超过 %d 小时，重置连续计数（避免误报）", STALE_HOURS)
                    state["zero_streak"] = 0
                    state["source_fails"] = {}
            except Exception:
                pass

        alerts, zero_streak, fails = evaluate_round(
            summary,
            zero_streak=int(state.get("zero_streak", 0)),
            fails=state.get("source_fails", {}) or {},
            rules=rules,
        )

        # 登录态失效单独识别（熔断器会把它误当成限流，见 evaluate_login_expired）
        alerts += evaluate_login_expired(summary.get("cooldowns") or {})

        state["zero_streak"] = zero_streak
        state["source_fails"] = fails
        state["last_round"] = {
            "at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
            "listings": int(summary.get("listings", 0) or 0),
        }
        _save_state(state)

        for a in alerts:
            alerting.send(a["level"], a["title"], a["body"], key=a["key"])
        return alerts
    except Exception as e:
        logger.warning("健康检查自身出错（已忽略）：%s", e)
        return []


def maybe_check_coverage() -> dict | None:
    """每天跑一次覆盖检查（同一自然日只跑一次），返回结果或 None。

    为什么挂在轮次里而不是单独起定时任务：轮次本来就在跑，
    多跑一次 SQL 的成本可以忽略；单独起一个定时任务反而多一处可能失效的地方
    （这个项目已经吃过「告警只挂在单入口」的亏）。
    """
    try:
        state = _load_state()
        today = date.today().isoformat()
        if state.get("last_coverage_check") == today:
            return None
        r = check_coverage()
        state = _load_state()          # check_coverage 可能改过状态，重读
        state["last_coverage_check"] = today
        _save_state(state)
        logger.info(
            "覆盖检查（%s）：%d 个在追型号，从未采到 %d 个，超期未采 %d 个",
            today, r["total_models"], len(r["never_collected"]), len(r["stale_models"]),
        )
        return r
    except Exception as e:
        logger.warning("覆盖检查失败（已忽略）：%s", e)
        return None


def reset_state() -> dict:
    """清掉连续计数。测试污染、或人工确认"这些计数已经没意义"时用。"""
    state = _load_state()
    old = {
        "zero_streak": state.get("zero_streak", 0),
        "source_fails": dict(state.get("source_fails", {}) or {}),
    }
    state["zero_streak"] = 0
    state["source_fails"] = {}
    _save_state(state)
    return old


def check_coverage(days: int = 14, stale_after: int | None = None) -> dict:
    """型号覆盖检查（需要 DB，按天跑即可，不必每轮）。

    找「很久没采到」的型号 —— 这是覆盖率问题的**早期信号**：
    2026-10-01 统计时发现 54 个型号 14 天只采到过 1 天，
    但当时没有任何机制会告诉你这件事。

    ⚠️ 与 `scripts/coverage.py` 的分工（**两套口径，别混淆**）
    ---------------------------------------------------------
    | | 本函数 | `scripts/coverage.py` |
    |---|---|---|
    | 范围 | **只 gpu + cpu（= 采集范围）** | 全品类（351 个型号） |
    | 口径 | 从未采到 / 超 N 天没采到 | 累计有过 / 今天有没有 |
    | 用途 | **自动告警**（每天一次） | **人工查看**缺口明细 |

    所以两者的数字**本来就不该相等**（130 vs 351）。真要对比，
    先看范围是否一致 —— 这是最容易踩的坑。
    采集范围由 `scripts/collect_scheduled.sh` 的
    `DIYPRICE_FOCUS_CATEGORY=gpu,cpu` 决定；哪天扩了范围，这里的
    `category in ('gpu','cpu')` 要跟着改，否则会漏报新品类。
    """
    from sqlalchemy import text

    from ..db import session_scope

    cfg = alerting.load_config()
    stale_after = stale_after or int((cfg.get("rules") or {}).get("model_stale_days", 7) or 7)

    with session_scope() as session:
        total = session.execute(text(
            "select count(*) from products where is_active=1 and category in ('gpu','cpu')"
        )).scalar() or 0

        stale = session.execute(text("""
            select p.model, p.category,
                   (select max(l.trade_date) from listings l
                      join platforms pf on pf.id = l.platform_id
                     where l.product_id = p.id and l.is_synthetic = 0 and pf.is_active = 1) last_seen
              from products p
             where p.is_active = 1 and p.category in ('gpu','cpu')
        """)).all()

    cutoff = date.today() - timedelta(days=stale_after)

    def _as_date(v):
        """SQLite 原生查询返回的是字符串，不是 date —— 直接比较会 TypeError。
        （踩过：'<' not supported between 'str' and 'datetime.date'）"""
        if v is None:
            return None
        if isinstance(v, date):
            return v
        try:
            return date.fromisoformat(str(v)[:10])
        except ValueError:
            return None

    never = [r for r in stale if r.last_seen is None]
    old = []
    for r in stale:
        d = _as_date(r.last_seen)
        if d is not None and d < cutoff:
            old.append({"model": r.model, "last_seen": d.isoformat()})

    result = {
        "total_models": total,
        "never_collected": [r.model for r in never],
        "stale_models": old,
        "stale_after_days": stale_after,
    }

    if never:
        alerting.send(
            "warn",
            f"{len(never)} 个型号从未采到过",
            "、".join(result["never_collected"][:12]),
            key="coverage-never",
        )
    if old:
        alerting.send(
            "warn",
            f"{len(old)} 个型号超过 {stale_after} 天没采到",
            "、".join(f"{x['model']}({x['last_seen']})" for x in result["stale_models"][:10]),
            key="coverage-stale",
        )
    return result


def parse_scutil_proxy(raw: str) -> dict:
    """**纯函数**：解析 `scutil --proxy` 的输出。

    抽出来是为了能直接断言 —— 自检不该为了测一行正则去调系统命令。

    macOS 的输出长这样（`<dictionary> { ... }`）：

        HTTPEnable : 1
        HTTPPort : 7897
        HTTPProxy : 127.0.0.1
        HTTPSEnable : 1
        ...

    三种代理（HTTP/HTTPS/SOCKS）**任一为 1** 就算开了代理 ——
    Chromium 会继承它们，任何一个指向死端口都会让请求全挂。
    """
    import re

    out = {"enabled": False, "host": "", "port": 0, "kind": ""}
    for kind in ("HTTP", "HTTPS", "SOCKS"):
        if re.search(rf"{kind}Enable\s*:\s*1", raw or ""):
            m_host = re.search(rf"{kind}Proxy\s*:\s*(\S+)", raw)
            m_port = re.search(rf"{kind}Port\s*:\s*(\d+)", raw)
            out.update({
                "enabled": True,
                "kind": kind,
                "host": m_host.group(1) if m_host else "",
                "port": int(m_port.group(1)) if m_port else 0,
            })
            break
    return out


def _system_proxy() -> dict:
    """读 macOS 系统代理设置（`scutil --proxy`）。"""
    import subprocess

    try:
        r = subprocess.run(["scutil", "--proxy"], capture_output=True, text=True, timeout=5)
        raw = r.stdout or ""
    except Exception as e:
        logger.warning("读取系统代理失败：%s", e)
        raw = ""
    out = parse_scutil_proxy(raw)
    out["raw"] = raw
    return out


def _tcp_ok(host: str, port: int, timeout: float = 3.0) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


# 采集目标 —— 前置检查必须探**这些**，而不是随便一个网站。
#
# ⚠️ 2026-10-03 教训（白丢 17 轮 / 约 26 小时）：
#    原先的「基础连通性」判据探的是**公共 DNS 的 TCP 53 端口**
#    （`_tcp_ok("223.5.5.5", 53) and _tcp_ok("1.1.1.1", 53)`）。两个问题：
#      · "能上网" ≠ "能连外部 DNS 的 53 端口" —— 公司网 / 路由器策略 /
#        运营商策略常拦外部 DNS，但 HTTP/HTTPS 完全正常；
#      · `1.1.1.1:53` 在国内**本来就长期不通**（本机实测 False），
#        这个判据实际只押在 223.5.5.5 一个点上。
#    结果：那段时间 HTTP 目标全通（实测 curl 200），却被判"没网"，整轮早退。
#
# ⇒ 判据必须与**采集浏览器真正走的路径**一致。它走的是这几个域的 HTTP(S)。
_PROBE_TARGETS = (
    ("jd", "https://search.jd.com/"),
    ("xianyu", "https://www.goofish.com/"),
    ("pdd", "https://mobile.yangkeduo.com/"),
)


def probe_targets(timeout: float = 6.0) -> tuple[bool, dict]:
    """探真实采集目标。返回 (是否有任一个可达, 每个目标的明细)。

    任一可达即认为网络可用 —— 单点失败可能是平台侧抖动，不该拦整轮。
    """
    results: dict = {}
    for code, url in _PROBE_TARGETS:
        results[code] = _http_ok(url=url, timeout=timeout)
    return any(results.values()), results


def _http_ok(proxy_host: str = "", proxy_port: int = 0,
             url: str = "https://www.baidu.com", timeout: float = 6.0) -> bool:
    """发一个**真实 HTTP 请求**，走指定的代理（或显式不走代理）。

    ⚠️ 为什么不能只探端口：代理端口开着 ≠ 代理能上网。
       节点挂掉时端口照样 accept，TCP 探活会通过，然后 Chromium 拿到的
       还是 `ERR_INTERNET_DISCONNECTED` / `ERR_PROXY_CONNECTION_FAILED` ——
       预检就白做了。必须真的走一次请求。

    ⚠️ 必须**显式**传 ProxyHandler：`urllib` 的默认 opener 在 macOS 上会读
       系统代理，但行为不稳定；显式指定才能保证探的就是 Chromium 会走的那条路。
    """
    import urllib.request

    if proxy_host and proxy_port:
        handlers = [urllib.request.ProxyHandler({
            "http": f"http://{proxy_host}:{proxy_port}",
            "https": f"http://{proxy_host}:{proxy_port}",
        })]
    else:
        handlers = [urllib.request.ProxyHandler({})]   # 空 dict = 显式不走代理
    opener = urllib.request.build_opener(*handlers)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with opener.open(req, timeout=timeout) as resp:
            resp.read(256)
            return 200 <= resp.status < 500
    except Exception as e:
        logger.debug("HTTP 探活失败（proxy=%s:%s）：%s", proxy_host or "-", proxy_port or "-", e)
        return False


def preflight() -> dict:
    """轮次前置检查：系统代理可用性 + 网络连通性。

    为什么必须有
    ------------
    2026-09-29：macOS 系统代理开着、指向 `127.0.0.1:7897`，但 **Clash Verge 没在运行**。
    Chromium 默认继承系统代理 → 每个请求都打到一个死端口 → 15 轮全部 0 条，
    跑了一整天没人知道（当天 51 次 `ERR_PROXY_CONNECTION_FAILED`）。

    在轮次开始前花几秒探一次，不通就**早退 + 告警**，
    而不是浪费整轮（约 2 分钟 + 一堆无效请求）去撞。

    ⚠️ 探测必须覆盖「采集浏览器实际走的路径」，而不是「命令行能不能通」：
       **curl 不读 macOS 系统代理，Chromium 读**。用 curl 探会得出
       "网络正常"的错误结论 —— 我 09-29 就是这么误判的。

    🔧 2026-10-01 起：采集浏览器已配 `--no-proxy-server`
       （见 `session.browser_proxy_args()`），**不再继承系统代理**。
       所以这里以「浏览器实际走的那条路」为准去探；系统代理状态仍然记录在
       detail 里供排查，但**不再据此拦轮次** —— 那正是 22:30 白丢一整轮的原因。

       🔧 2026-10-03 改：连通性判据从「公共 DNS 的 53 端口」换成
          「**真实采集目标**（京东/闲鱼/拼多多的 HTTP）」。旧判据把
          "能上网但连不上外部 DNS 53"（公司网/路由器策略常见）误判成断网，
          白丢 17 轮约 26 小时。**判据必须与采集浏览器真正走的路径一致。**
          DNS 53 探活降级为 detail 记录，不参与判定。
    """
    from .session import browser_proxy_args

    problems: list[str] = []
    detail: dict = {}

    proxy = _system_proxy()
    detail["system_proxy"] = (
        f"{proxy['host']}:{proxy['port']}" if proxy["enabled"] else "未开启"
    )

    # 探「采集浏览器实际会走的那条路」，而不是「系统代理怎么配」
    browser_path = browser_proxy_args()
    if browser_path and browser_path[0].startswith("--proxy-server="):
        spec = browser_path[0].split("=", 1)[1]
        detail["browser_path"] = f"proxy:{spec}"
        host = spec.split("//", 1)[-1].rpartition(":")[0]
        port_s = spec.rpartition(":")[2]
        try:
            port = int(port_s)
        except ValueError:
            port = 0
        if not port or not _tcp_ok(host, port):
            problems.append(f"DIYPRICE_BROWSER_PROXY 指向 {spec} 但**连不上**")
        elif not _http_ok(host, port):
            problems.append(f"DIYPRICE_BROWSER_PROXY {spec} 端口通但**上不了网**")
    else:
        # 默认路径：浏览器直连。系统代理开着也与采集无关 —— 只记录，不拦轮次。
        detail["browser_path"] = "direct"
        if not _http_ok():
            problems.append(
                "直连上不了网 —— 采集浏览器已配 --no-proxy-server（不继承系统代理），"
                "所以这是**本机网络**问题：检查 Wi-Fi / 热点"
            )
        if proxy["enabled"]:
            detail["system_proxy_ignored"] = True

    # ---- 基础连通性：探**真实采集目标** ----
    #
    # ⚠️ 2026-10-03 改：旧判据探公共 DNS 的 53 端口，把「能上网但连不上外部
    #    DNS 53」误判成断网，白丢 17 轮（约 26 小时）。判据必须与采集路径一致。
    targets_ok, targets_detail = probe_targets()
    detail["targets"] = targets_detail
    if not targets_ok:
        # 全都不通 —— 再探一个中立站点，用来区分两类完全不同的故障：
        #   · 中立站点也不通  → 本机确实断网（检查 Wi-Fi / 热点）
        #   · 中立站点通      → 本机有网，是采集目标侧不可达（平台故障 / DNS 劫持）
        neutral_ok = _http_ok(timeout=6.0)
        detail["neutral_probe"] = neutral_ok
        if neutral_ok:
            problems.append(
                "采集目标全部不可达（京东/闲鱼/拼多多），但中立站点可达 —— "
                "本机网络正常，疑为平台侧故障或 DNS 劫持；本轮跳过"
            )
        else:
            problems.append(
                "直连上不了网（采集目标与中立站点都不可达）—— "
                "采集浏览器已配 --no-proxy-server，检查 Wi-Fi / 热点"
            )

    # DNS 53 端口的可访问性 —— **只记录，不拦轮次**。
    # ⚠️ 它是 2026-10-03 那次误判的来源；保留只为排障时能一眼看到
    #    "网络正常但外部 DNS 被拦"这种环境特征。注意 1.1.1.1:53 在国内长期不通。
    detail["dns53"] = {
        "223.5.5.5": _tcp_ok("223.5.5.5", 53, timeout=2.0),
        "1.1.1.1": _tcp_ok("1.1.1.1", 53, timeout=2.0),
    }

    return {
        "ok": not problems,
        "problems": problems,
        "proxy_enabled": proxy["enabled"],
        "proxy": f"{proxy['host']}:{proxy['port']}" if proxy["enabled"] else "未开启",
        **detail,
    }


def check_preflight() -> dict:
    """跑预检，不通就报警（同一故障在抑制窗口内只报一次）。"""
    r = preflight()
    if not r["ok"]:
        alerting.send(
            "critical",
            "采集前置检查不通过，本轮跳过",
            "；".join(r["problems"]),
            key="preflight",
        )
    return r


def status() -> dict:
    """当前健康状态，供 CLI / 网页展示。"""
    return {
        "state_file": str(STATE_FILE),
        **_load_state(),
    }
