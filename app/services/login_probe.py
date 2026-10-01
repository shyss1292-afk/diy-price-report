"""低成本登录态探针 —— **开跑前**判定，而不是等采集失败再反推。

为什么值得单独做（与 `policy.AuthExpiredError` 的分工）：

    AuthExpiredError  采集**途中**发现 → 立刻中止整批（已经省下了整轮空转）
    本模块            采集**之前**发现 → 连浏览器都不用为它多跑一个型号

两者的价值不重叠：前者止住已经开始的一轮，后者连一轮都不浪费。

⚠️ 成本与风险的诚实说明
----------------------
每次探针都是一次**真实请求**。对已经被风控标记的平台（当前是京东与拼多多），
任何额外请求都要算进"今天又打扰了它几次"。所以：

  · 默认**不自动跑**，只在 `python -m app.cli login-check` 时手动触发；
  · 或显式设 `DIYPRICE_LOGIN_PROBE=1` 让它每轮开跑前跑一次（每源 1 次请求）；
  · 同一轮同一站点**只探一次**（内部缓存），避免多个来源重复打。
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger("diyprice.login_probe")

# site → 探针定义
#   kind="json"     : 读响应体 JSON，按 retcode 判定
#   kind="redirect" : 看最终落地 URL / 页面文案是否被踢到登录
PROBES: dict[str, dict] = {
    "jd": {
        "url": "https://me-api.jd.com/user_new/info/GetJDUserInfoUnion",
        "kind": "json",
        # 京东的"未登录"判定。注意是**字符串** "1001"（接口返回的就是字符串）。
        "logout_markers": ("1001",),
    },
    "goofish": {
        # 强鉴权页：未登录会被重定向到登录页
        "url": "https://www.goofish.com/bought",
        "kind": "redirect",
        "logout_url_markers": ("login", "passport"),
        "logout_text_markers": ("请先登录", "登录后查看", "扫码登录"),
    },
}

# 拼多多没有可用的轻量端点 —— 明说，而不是给一个假探针。
UNSUPPORTED: dict[str, str] = {
    "pdd": "拼多多没有可用的轻量登录端点（搜索接口响应加密），只能靠采集途中判定",
}

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 600.0     # 同一进程内 10 分钟内不重复探


def supported(site: str) -> bool:
    return site in PROBES


def probe_login(page, site: str, *, use_cache: bool = True) -> dict:
    """用**已借到的页面**探一次登录态。返回：

        {"site", "supported", "logged_in" (True/False/None), "reason", "url", "elapsed_ms"}

    `logged_in=None` 表示**探不出来**（超时/结构变化）—— 这与 False（明确未登录）
    是两件事，调用方必须区分：把"探不出来"当成"未登录"会导致好端端的源被跳过。
    """
    if not supported(site):
        return {
            "site": site, "supported": False, "logged_in": None,
            "reason": UNSUPPORTED.get(site, "该站点没有配置登录探针"),
            "url": "", "elapsed_ms": 0,
        }

    if use_cache:
        hit = _CACHE.get(site)
        if hit and time.time() - hit[0] < _CACHE_TTL:
            return dict(hit[1], cached=True)

    spec = PROBES[site]
    started = time.time()
    out = {
        "site": site, "supported": True, "logged_in": None,
        "reason": "", "url": "", "elapsed_ms": 0,
    }
    try:
        response = page.goto(spec["url"], timeout=20000, wait_until="domcontentloaded")
        # 探针本身也是请求 —— 照抄参考项目"太快了容易被判定为机器"的做法，
        # 但把幅度压到最小（0.4~1.2s），因为我们已经够慢了。
        page.wait_for_timeout(400 + int((time.time() * 1000) % 800))
        out["url"] = (page.url or "")[:200]

        if spec["kind"] == "json":
            body = ""
            try:
                body = page.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                ) or ""
            except Exception:  # noqa: BLE001
                body = ""
            status = getattr(response, "status", None)
            if status and int(status) >= 400:
                out["reason"] = f"HTTP {status}"
            else:
                import json as _json

                retcode = ""
                try:
                    retcode = str((_json.loads(body) or {}).get("retcode", ""))
                except Exception:  # noqa: BLE001
                    out["reason"] = f"响应不是预期 JSON（前 60 字：{body[:60]!r}）"
                if retcode:
                    if retcode in spec["logout_markers"]:
                        out["logged_in"] = False
                        out["reason"] = f"retcode={retcode}（未登录）"
                    else:
                        out["logged_in"] = True
                        out["reason"] = f"retcode={retcode}"
        else:
            low_url = out["url"].lower()
            text = ""
            try:
                text = page.evaluate(
                    "() => document.body ? document.body.innerText.slice(0, 800) : ''"
                ) or ""
            except Exception:  # noqa: BLE001
                pass
            if any(m in low_url for m in spec.get("logout_url_markers", ())):
                out["logged_in"] = False
                out["reason"] = f"被重定向到登录页：{out['url'][:90]}"
            elif any(m in text for m in spec.get("logout_text_markers", ())):
                out["logged_in"] = False
                out["reason"] = "页面出现登录提示文案"
            else:
                out["logged_in"] = True
                out["reason"] = "强鉴权页可正常访问（未跳登录）"
    except Exception as exc:  # noqa: BLE001 —— 探针失败绝不影响采集
        out["reason"] = f"探针异常：{type(exc).__name__} {str(exc)[:80]}"
    out["elapsed_ms"] = int((time.time() - started) * 1000)
    logger.info(
        "登录探针 %s：%s（%s，%dms）",
        site,
        {True: "已登录", False: "未登录", None: "无法判定"}[out["logged_in"]],
        out["reason"], out["elapsed_ms"],
    )
    _CACHE[site] = (time.time(), out)
    return out


def probe_via_worker(worker, site: str, viewport: dict | None = None,
                     user_agent: str | None = None) -> dict:
    """借 worker 的一个页面探一次（复用同一个已登录的浏览器）。"""
    try:
        with worker.page(site=site, viewport=viewport, user_agent=user_agent) as page:
            return probe_login(page, site)
    except Exception as exc:  # noqa: BLE001
        return {
            "site": site, "supported": supported(site), "logged_in": None,
            "reason": f"借页失败：{type(exc).__name__} {str(exc)[:80]}",
            "url": "", "elapsed_ms": 0,
        }


def clear_cache() -> None:
    _CACHE.clear()
