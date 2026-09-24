"""纯逻辑自检 —— 不依赖浏览器与网络，跑得很快。

    python -m scripts.selftest

存在的理由：有些 bug 不会抛异常、也不影响接口状态码，只在界面上表现为
「这一列全是 —」。涨跌榜的 direction 语义就是如此 —— 排序键里的
「无数据排最后」守卫被 `reverse=True` 一起反转，导致涨榜被 None 行占满，
而跌榜恰好正常，所以长期没被发现。这类约定必须靠断言钉住。

覆盖范围：
  · 涨跌榜方向语义（最初的那批断言）
  · 请求瘦身的拦截/放行规则（拦错了会让验证码白屏，且极难归因）
  · 平台差异化节奏的数值边界（间隔必须夹取，否则会出现 0.2 秒连发）
  · 限流识别的真阳性与**真阴性**（误判会白停一个源）
  · 熔断器的冷却语义与持久化

只放**与数据无关**的纯函数断言；涉及数据库的检查交给 scripts/diagnose.py。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

from app.services.trend import build_ranking

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(f"{name}{(' — ' + detail) if detail else ''}")


def make(model: str, pct: float | None, latest: float = 100.0) -> dict:
    return {"model": model, "change_pct": pct, "latest": latest}


SAMPLE = [
    make("涨30", 30.0), make("涨5", 5.0),
    make("无数据-A", None), make("无数据-B", None),
    make("跌8", -8.0), make("跌22", -22.0),
    make("平0", 0.0),
]


# ====================================================================== 涨跌榜

def test_ranking_direction() -> None:
    # build_ranking 的 rows 参数允许直接传入快照，因此不碰数据库
    up = build_ranking(None, direction="up", rows=SAMPLE, limit=10)  # type: ignore[arg-type]
    down = build_ranking(None, direction="down", rows=SAMPLE, limit=10)  # type: ignore[arg-type]

    check("涨榜只含上涨型号",
          all((r["change_pct"] or 0) > 0 for r in up),
          f"实际 {[(r['model'], r['change_pct']) for r in up]}")
    check("涨榜按涨幅从高到低",
          [r["model"] for r in up] == ["涨30", "涨5"],
          f"实际 {[r['model'] for r in up]}")
    check("跌榜只含下跌型号",
          all((r["change_pct"] or 0) < 0 for r in down),
          f"实际 {[(r['model'], r['change_pct']) for r in down]}")
    check("跌榜按跌幅从大到小",
          [r["model"] for r in down] == ["跌22", "跌8"],
          f"实际 {[r['model'] for r in down]}")
    check("两榜都不含无数据（change_pct 为 None）",
          all(r["change_pct"] is not None for r in up + down))
    check("持平（0%）不进入涨榜也不进入跌榜",
          all(r["model"] != "平0" for r in up + down))
    check("limit 生效",
          len(build_ranking(None, direction="up", rows=SAMPLE, limit=1)) == 1)  # type: ignore[arg-type]
    check("传入的共享快照未被改动",
          [r["model"] for r in SAMPLE] == ["涨30", "涨5", "无数据-A", "无数据-B", "跌8", "跌22", "平0"])


def test_ranking_empty() -> None:
    only_none = [make("A", None), make("B", None)]
    check("全部无数据时返回空列表（而不是塞满 None 行）",
          build_ranking(None, direction="up", rows=only_none) == []  # type: ignore[arg-type]
          and build_ranking(None, direction="down", rows=only_none) == [])  # type: ignore[arg-type]
    check("空输入不报错",
          build_ranking(None, direction="up", rows=[]) == [])  # type: ignore[arg-type]


# ============================================================== 请求瘦身白名单

def test_request_slimming() -> None:
    """拦错一条就可能让验证码白屏，所以放行/拦截都要有断言。"""
    from app.services.browser_worker import should_block

    must_allow = [
        # ⚠️ 用例 URL 刻意用「只含一个受测关键词」的虚构域名。
        #    原因：白名单是子串匹配，如果 URL 里同时含平台主域
        #    （如 `baxia.taobao.com` 同时命中 `baxia` 和 `taobao.com`），
        #    删掉 `baxia` 这条规则测试照样通过 —— 用例会互相遮蔽，
        #    等于没测。凡是能隔离的关键词都隔离。
        # 阿里系自研风控 SDK（用户明确要求）
        ("https://static-cdn.example.net/punish/entry.js", "script", "punish 惩罚页"),
        ("https://rgv587-cdn.example.net/entry.js", "script", "rgv587 惩罚页资源"),
        ("https://baxia-cdn.example.net/entry.js", "script", "baxia 霸下"),
        ("https://awsc-cdn.example.net/sdk.js", "script", "awsc 云盾"),
        ("https://uab-cdn.example.net/uab.js", "script", "uab 组件"),
        ("https://um-cdn.example.net/js/um.js", "script", "um.js 统计脚本"),
        # ⚠️ um.js 可能挂在 umeng.com 下，而该域名在打点黑名单里 ——
        #    这条专门钉住"白名单优先级高于黑名单"这个相互作用
        ("https://www.umeng.com/js/um.js", "script", "um.js 挂在 umeng 域下"),
        # 域名型关键词（本身包含平台主域，无法隔离，属于预期内）
        ("https://sec.taobao.com/risk.js", "script", "安全域 sec.taobao.com"),
        ("https://risk.jd.com/fingerprint.js", "script", "风控域 risk.jd.com"),
        ("https://anti.jd.com/v2/x.js", "script", "反爬域 anti.jd.com"),
        ("https://blackhole-cdn.example.net/report", "xhr", "blackhole 黑洞"),
        # 第三方验证码兜底
        ("https://static.geetest.com/gt.js", "script", "极验（其它站兜底）"),
        # image 一律放行（滑块背景图 / 缺口拼图）
        ("https://cdn.example.com/captcha-piece.png", "image", "验证码碎片"),
        ("https://img.example.net/verify/sprite.jpg", "image", "验证图 sprite"),
        ("https://unknown-cdn.com/random.gif", "image", "任意图片"),
        ("https://cdn.example.org/no-extension", "image", "无扩展名的图片"),
        # 平台主域
        ("https://search.jd.com/Search?keyword=x", "document", "京东搜索页"),
        ("https://www.goofish.com/search?q=x", "document", "闲鱼搜索页"),
        ("https://mobile.yangkeduo.com/x.js", "script", "拼多多主域"),
    ]
    bad = [
        f"{label}: {'放行' if should_block(url, rt) else '拦截'}"
        for url, rt, label in must_allow
        if should_block(url, rt)
    ]
    check(f"白名单与 image 全部放行（{len(must_allow)} 例）", not bad, "; ".join(bad))

    # 结构性断言：拦截面里绝不能出现 image。
    # 行为断言之外再钉一层 —— 以后有人"顺手"把 image 加进这个集合时，
    # should_block 里的显式早返回会兜住，但那属于意外；这里让它直接失败。
    from app.services.browser_worker import _HEAVY_RESOURCE_TYPES

    check("拦截资源类型集合里不含 image（防止误加导致验证码死锁）",
          "image" not in _HEAVY_RESOURCE_TYPES,
          f"实际 {sorted(_HEAVY_RESOURCE_TYPES)}")
    check("拦截资源类型只含 media / font",
          _HEAVY_RESOURCE_TYPES == {"media", "font"},
          f"实际 {sorted(_HEAVY_RESOURCE_TYPES)}")

    must_block = [
        ("https://cdn.x.com/a.woff2", "font", "woff2 字体（按类型）"),
        ("https://cdn.x.com/a.woff", "font", "woff 字体（按类型）"),
        ("https://cdn.x.com/a.ttf", "font", "ttf 字体（按类型）"),
        ("https://cdn.x.com/a.woff2?v=1", "other", "字体（按扩展名兜底）"),
        ("https://cdn.x.com/promo.mp4", "media", "视频流"),
        ("https://www.google-analytics.com/collect", "xhr", "GA 打点"),
        ("https://hm.baidu.com/hm.js", "script", "百度统计"),
        ("https://www.cnzz.com/stat.js", "script", "CNZZ 统计"),
    ]
    missed = [
        f"{label}: 放行了"
        for url, rt, label in must_block
        if not should_block(url, rt)
    ]
    check(f"该拦的仍然拦住（{len(must_block)} 例）", not missed, "; ".join(missed))

    check("白名单优先级高于扩展名（京东风控域名下的字体不拦）",
          should_block("https://static.jd.com/blackhole/a.woff2", "font") is False)
    check("普通脚本不被误拦",
          should_block("https://apm.example.com/app.js", "script") is False)

    # ---- 白名单**优先级**的精确用例 ----
    #
    # 上面那批关键词用例其实测不出"关键词是否还在白名单里"：它们本来就
    # 没人拦（script 类型 + 非黑名单域名），把白名单条目删掉，请求照样放行，
    # 用例仍然通过 —— 这叫做用例互相遮蔽，是"永远绿的测试"。
    #
    # 真正能验证优先级的是：URL 带受测关键词，**同时**这个请求在其他规则下
    # 本应被拦（这里用显式打点黑名单域名）。此时只有白名单能救它。
    priority_cases = [
        ("punish", "https://www.google-analytics.com/punish.js"),
        ("rgv587", "https://www.google-analytics.com/rgv587.js"),
        ("baxia", "https://www.google-analytics.com/baxia.js"),
        ("awsc", "https://www.google-analytics.com/awsc.js"),
        ("uab", "https://www.google-analytics.com/uab.js"),
        ("um.js", "https://www.google-analytics.com/js/um.js"),
    ]
    # 先证明这些 URL **去掉关键词后确实会被拦**，否则这组用例同样无意义
    baseline_bad = [
        url for _, url in priority_cases
        if not should_block(url.replace("punish.js", "x.js")
                            .replace("rgv587.js", "x.js")
                            .replace("baxia.js", "x.js")
                            .replace("awsc.js", "x.js")
                            .replace("uab.js", "x.js")
                            .replace("/js/um.js", "/js/x.js"), "script")
    ]
    check("优先级用例的前置条件成立（去掉关键词后确实会被拦）",
          not baseline_bad, f"这些 URL 本身就放行，用例无意义：{baseline_bad}")

    priority_bad = [
        kw for kw, url in priority_cases if should_block(url, "script")
    ]
    check(f"白名单优先级压过打点黑名单（{len(priority_cases)} 个关键词）",
          not priority_bad, f"被拦的关键词：{priority_bad}")


# ============================================================== 平台节奏边界

class _StubRandom:
    """替身随机源：让高斯与长停顿都可预期。"""

    def __init__(self, gauss_value: float, rnd: float) -> None:
        self._gauss = gauss_value
        self._rnd = rnd

    def gauss(self, _mu: float, _sigma: float) -> float:
        return self._gauss

    def random(self) -> float:
        return self._rnd

    def uniform(self, a: float, b: float) -> float:
        return (a + b) / 2.0


def test_policy_delays() -> None:
    from app.collectors import policy

    # 高斯采样低于下界 → 必须被抬到 delay_floor，而不是"来一次快速连发"
    with mock.patch.object(policy, "random", _StubRandom(-999.0, 0.99)):
        low = [policy.next_delay("jd") for _ in range(5)]
    check("低于下界被抬到 delay_floor（京东 3.0s）",
          all(abs(v - 3.0) < 1e-6 for v in low), f"实际 {low}")

    # 高斯采样高于上界 → 必须被压到 delay_ceil
    with mock.patch.object(policy, "random", _StubRandom(999.0, 0.99)):
        high = [policy.next_delay("jd") for _ in range(5)]
    check("高于上界被压到 delay_ceil（京东 8.0s）",
          all(abs(v - 8.0) < 1e-6 for v in high), f"实际 {high}")

    # 命中长停顿分支：正常值 + 长停顿区间中值
    with mock.patch.object(policy, "random", _StubRandom(5.0, 0.0)):
        long_delay = policy.next_delay("jd")
    check("偶发长停顿会叠加（5.0 + (10+25)/2 = 22.5s）",
          abs(long_delay - 22.5) < 1e-6, f"实际 {long_delay}")

    # 真实随机下界：绝不允许出现"过快的连发"
    for code, floor in (("jd", 3.0), ("xianyu", 5.0), ("pdd", 6.0)):
        samples = [policy.next_delay(code) for _ in range(2000)]
        pol = policy.policy_for(code)
        cap = pol.delay_ceil + pol.long_pause_range[1]
        check(f"{pol.label} 2000 次采样都在 [{floor}, {cap}] 内",
              min(samples) >= floor - 1e-9 and max(samples) <= cap + 1e-9,
              f"实际 [{min(samples):.2f}, {max(samples):.2f}]")
        check(f"{pol.label} 间隔确实在波动（不是固定值）",
              len({round(v, 2) for v in samples}) > 100)

    # 多标签并发是明确禁止的（并发请求会让"间隔随机化"失效）
    check("三个平台都禁止多 Tab 并发",
          all(not pol.allow_multi_tab for pol in policy.PLATFORM_THROTTLE_CONFIG.values()),
          f"实际 {[(c, p.allow_multi_tab) for c, p in policy.PLATFORM_THROTTLE_CONFIG.items()]}")

    # 单会话任务上限：拼多多按用户要求"严格控制在 15~20"
    pdd = policy.PLATFORM_THROTTLE_CONFIG["pdd"]
    check("拼多多单会话任务上限落在 15~20（用户指定）",
          15 <= pdd.session_task_limit <= 20, f"实际 {pdd.session_task_limit}")

    # 冷却时长符合约定
    check("京东冷却 180s（用户指定）", policy.PLATFORM_THROTTLE_CONFIG["jd"].cooldown_seconds == 180)
    check("闲鱼/拼多多冷却落在 240~300s（用户指定）",
          all(240 <= policy.PLATFORM_THROTTLE_CONFIG[c].cooldown_seconds <= 300
              for c in ("xianyu", "pdd")),
          f"实际 闲鱼={policy.PLATFORM_THROTTLE_CONFIG['xianyu'].cooldown_seconds} "
          f"拼多多={policy.PLATFORM_THROTTLE_CONFIG['pdd'].cooldown_seconds}")

    check("拼多多要求等 networkidle（前端签名要先算完）",
          policy.PLATFORM_THROTTLE_CONFIG["pdd"].wait_networkidle
          and policy.PLATFORM_THROTTLE_CONFIG["pdd"].ready_extra_ms >= 500)

    check("未知平台回落到兜底策略而不是抛异常",
          policy.policy_for("unknown").code == "_default"
          and policy.policy_for(None).code == "_default")

    # 京东的核心分布应与需求一致：多数落在 3~8s
    jd = [policy.next_delay("jd") for _ in range(2000)]
    core_ratio = sum(1 for v in jd if v <= 8.0) / len(jd)
    check("京东多数间隔落在 3~8s 核心区间（长停顿是少数）",
          core_ratio > 0.8, f"实际 {core_ratio:.0%}")


# ============================================================== 限流识别

class _FakePage:
    """只实现 detect_rate_limit 用到的三个接口。"""

    def __init__(self, url: str = "", title: str = "", text: str = "") -> None:
        self.url = url
        self._title = title
        self._text = text

    def title(self) -> str:
        return self._title

    def evaluate(self, _js: str) -> str:  # noqa: ANN001
        return self._text


def test_rate_limit_detection() -> None:
    from app.collectors import policy

    positives = [
        ("jd", _FakePage("https://search.jd.com/Search?keyword=x", "商品搜索",
                         "抱歉由于访问频繁导致无法搜索，请稍后再试！"), "访问频繁", "京东文案"),
        ("jd", _FakePage("https://search.jd.com/busy.html"), "busy.html", "京东 busy 页"),
        ("jd", _FakePage("https://passport.jd.com/new/login.aspx"), "passport.jd.com", "被踢到登录页"),
        ("xianyu", _FakePage("https://www.goofish.com/punish?x=1"), "punish", "闲鱼 punish 页"),
        ("xianyu", _FakePage("https://www.goofish.com/search", "闲鱼",
                             "系统繁忙，请稍后再试"), "系统繁忙", "闲鱼文案"),
        ("xianyu", _FakePage("https://www.goofish.com/search", "",
                             "非法访问 为了保障您的体验，请使用正常浏览器访问闲鱼~"),
         "非法访问", "闲鱼拦截文案（实测出现过）"),
        # 指标表里 `error_code=40001` 排在裸 `40001` 之前，命中的是更具体的那个
        ("pdd", _FakePage("https://mobile.yangkeduo.com/x", "", '{"error_code=40001"}'),
         "error_code=40001", "拼多多错误码"),
        ("pdd", _FakePage("https://mobile.yangkeduo.com/x", "", "访问异常 40001"),
         "40001", "拼多多裸错误码"),
        ("pdd", _FakePage("https://mobile.yangkeduo.com/verify.html"), "verify", "拼多多验证页"),
        ("pdd", _FakePage("https://mobile.yangkeduo.com/login.html"), "login.html", "被踢到登录页"),
    ]
    misses = []
    for code, page, expect, label in positives:
        hit = policy.detect_rate_limit(page, code)
        if not hit or hit[0] != expect:
            misses.append(f"{label}: 期望「{expect}」实际 {hit}")
    check(f"真阳性：{len(positives)} 种限流页都能识别", not misses, "; ".join(misses))

    # 403 / 429 走状态码分支（不依赖页面内容）
    check("HTTP 403 识别为限流",
          (policy.detect_rate_limit(_FakePage("https://search.jd.com/"), "jd", 403) or [None])[0] == "HTTP 403")
    check("HTTP 429 识别为限流",
          (policy.detect_rate_limit(_FakePage("https://www.goofish.com/"), "xianyu", 429) or [None])[0] == "HTTP 429")

    # ---- 真阴性：正常页面绝不能误判（误判会白停一个源）----
    negatives = [
        ("jd", _FakePage("https://search.jd.com/Search?keyword=RTX+5070",
                         "RTX 5070 - 商品搜索 - 京东",
                         "京东 全部分类 搜索 RTX 5070 显卡 七彩虹 ¥4599.00 自营 加入购物车")),
        ("xianyu", _FakePage("https://www.goofish.com/search?q=RTX+5070", "闲鱼",
                             "闲鱼 搜索 RTX 5070 显卡 九成新 ¥1899 包邮 我想要 宝贝详情")),
        ("pdd", _FakePage("https://mobile.yangkeduo.com/search_result.html?search_key=x",
                          "拼多多", "拼多多 搜索 显卡 ¥2688 已拼10万件 单独购买 发起拼单")),
    ]
    false_alarms = []
    for code, page in negatives:
        hit = policy.detect_rate_limit(page, code)
        if hit:
            false_alarms.append(f"{code}: 误判为「{hit[0]}」")
    check("真阴性：正常商品页不会被误判为限流", not false_alarms, "; ".join(false_alarms))

    # 异常页面（取不到标题/正文）不应崩，也不应误报
    class _BrokenPage:
        url = "about:blank"

        def title(self):  # noqa: ANN201
            raise RuntimeError("page crashed")

        def evaluate(self, _js):  # noqa: ANN001, ANN201
            raise RuntimeError("page crashed")

    check("页面崩溃时检测不抛异常且不误报",
          policy.detect_rate_limit(_BrokenPage(), "jd") is None)

    # assert 版本必须抛熔断信号
    try:
        policy.assert_not_rate_limited(
            _FakePage("https://search.jd.com/Search", "", "抱歉由于访问频繁导致无法搜索"), "jd"
        )
        check("assert_not_rate_limited 命中时抛 RateLimitError", False, "没抛异常")
    except policy.RateLimitError as exc:
        check("assert_not_rate_limited 命中时抛 RateLimitError",
              exc.indicator == "访问频繁" and exc.source == "jd")
    except Exception as exc:  # noqa: BLE001
        check("assert_not_rate_limited 命中时抛 RateLimitError", False,
              f"抛了 {type(exc).__name__}")

    # 不应抛的场合
    try:
        policy.assert_not_rate_limited(
            _FakePage("https://www.goofish.com/search?q=x", "闲鱼", "显卡 ¥1899"), "xianyu"
        )
        check("assert_not_rate_limited 正常页不抛", True)
    except Exception as exc:  # noqa: BLE001
        check("assert_not_rate_limited 正常页不抛", False, f"抛了 {type(exc).__name__}")


# ============================================================== 熔断器

def test_breaker() -> None:
    from app.services import breaker

    tmp = Path(tempfile.mkdtemp(prefix="diyprice_breaker_test_"))
    original = breaker.BREAKER_FILE
    breaker.BREAKER_FILE = tmp / "breaker.json"
    try:
        _run_breaker_cases(breaker, tmp)
    finally:
        breaker.BREAKER_FILE = original
        shutil.rmtree(tmp, ignore_errors=True)


def _run_breaker_cases(breaker, tmp: Path) -> None:
    check("初始状态无冷却", breaker.cooldown_remaining("jd") == 0.0
          and not breaker.is_cooling("jd"))
    check("初始 snapshot 为空", breaker.snapshot() == {})

    # ---- 触发 ----
    until = breaker.trip("jd", 180, "访问频繁")
    remaining = breaker.cooldown_remaining("jd")
    check("trip 后进入冷却且剩余时间合理",
          170 < remaining <= 180, f"实际 {remaining:.1f}s")
    check("is_cooling 为真", breaker.is_cooling("jd"))
    check("冷却不影响其它源", not breaker.is_cooling("pdd"))
    check("记录里带着原因与截止时间",
          breaker.entry_of("jd")["reason"] == "访问频繁"
          and breaker.entry_of("jd")["until_text"])

    # ---- 持久化：换个"进程"读同一个文件 ----
    raw = (tmp / "breaker.json").read_text(encoding="utf-8")
    check("冷却状态已落盘（跨进程可见）", "访问频繁" in raw and "until" in raw)
    check("无 .tmp 残留（原子写）", not list(tmp.glob("*.tmp")))
    check("反序列化后仍处于冷却",
          170 < breaker.cooldown_remaining("jd") <= 180)

    # ---- 重复触发不缩短冷却 ----
    before = breaker.cooldown_remaining("jd")
    breaker.trip("jd", 10, "再次命中")
    after = breaker.cooldown_remaining("jd")
    check("重复 trip 时取较晚的截止时间（冷却不会被缩短）",
          after >= before - 0.5, f"{before:.1f}s → {after:.1f}s")
    check("累计触发次数被记录", breaker.entry_of("jd")["trips"] == 2)

    # ---- wait_until_ready：三种分支 ----
    t0 = time.monotonic()
    ok, left = breaker.wait_until_ready("jd", "京东", max_wait=5.0)
    elapsed = time.monotonic() - t0
    check("剩余 > 上限时**不睡**且返回 False（避免拖住整轮）",
          ok is False and left > 100 and elapsed < 1.0,
          f"ok={ok} left={left:.0f} 耗时 {elapsed:.2f}s")

    ok2, left2 = breaker.wait_until_ready("pdd", "拼多多")
    check("没冷却的源立即放行", ok2 is True and left2 == 0.0)

    breaker.clear("jd")
    breaker.trip("jd", 0.4, "短冷却")
    t0 = time.monotonic()
    ok3, left3 = breaker.wait_until_ready("jd", "京东", max_wait=5.0)
    waited = time.monotonic() - t0
    check("剩余 ≤ 上限时真的睡够冷却再返回 True",
          ok3 is True and left3 == 0.0 and waited >= 0.3,
          f"ok={ok3} 睡了 {waited:.2f}s")
    check("睡完后冷却已清空", breaker.cooldown_remaining("jd") == 0.0)

    # ---- 过期条目不进 snapshot ----
    breaker.clear()
    breaker.trip("jd", 0, "已过期")
    check("已过期的冷却不出现在 snapshot 里", "jd" not in breaker.snapshot())
    check("已过期的冷却不影响放行",
          breaker.wait_until_ready("jd")[0] is True)

    # ---- 多源共存 + 整体清除 ----
    breaker.trip("jd", 60, "a")
    breaker.trip("pdd", 60, "b")
    check("多源冷却互不干扰",
          set(breaker.snapshot()) == {"jd", "pdd"}
          and "闲鱼" not in breaker.active_summary())
    check("active_summary 能同时列出两个源",
          "jd" in breaker.active_summary() and "pdd" in breaker.active_summary())
    removed = breaker.clear()
    check("clear() 清空全部", removed == 2 and breaker.snapshot() == {})

    # ============================================================ 跨轮次退避阶梯
    # 用户 2026-09-21 指定的核心契约：
    #   连续熔断 1 次 → 30min，2 次 → 2h，3 次 → 6h，4 次及以上 → 24h 封顶
    #   Fast-Fail：退避期内**不等待**，直接跳过
    #   成功重置：正常采到数据且无限流 → 连续计数清零
    breaker.clear()
    ladder = breaker.backoff_ladder()
    check("退避阶梯默认 30min/2h/6h/24h",
          ladder == (1800.0, 7200.0, 21600.0, 86400.0), f"实际 {ladder}")
    check("阶梯逐级放大",
          [breaker.backoff_for(n) for n in (1, 2, 3, 4)] == [1800.0, 7200.0, 21600.0, 86400.0])
    check("阶梯末级封顶（第 5、9 次不再增长）",
          breaker.backoff_for(5) == 86400.0 and breaker.backoff_for(9) == 86400.0)
    check("backoff_for 对 0 / 负数取第 1 级（不越界）",
          breaker.backoff_for(0) == 1800.0 and breaker.backoff_for(-3) == 1800.0)

    # 生产路径：trip 不传 seconds → 走阶梯（传死值会让阶梯形同虚设）
    breaker.trip("jd", reason="访问频繁")
    check("trip 默认走阶梯第 1 级（30 分钟）",
          1780 < breaker.cooldown_remaining("jd") <= 1800,
          f"实际 {breaker.cooldown_remaining('jd'):.0f}s")
    check("连续次数记为 1", breaker.consecutive_trips("jd") == 1)

    breaker.trip("jd", reason="访问频繁")
    check("连续第 2 次 → 冷却放大到 2 小时",
          7180 < breaker.cooldown_remaining("jd") <= 7200,
          f"实际 {breaker.cooldown_remaining('jd'):.0f}s")
    check("连续次数记为 2", breaker.consecutive_trips("jd") == 2)

    breaker.trip("jd", reason="访问频繁")
    breaker.trip("jd", reason="访问频繁")
    check("连续第 4 次 → 封顶 24 小时",
          86380 < breaker.cooldown_remaining("jd") <= 86400)
    check("连续次数记为 4", breaker.consecutive_trips("jd") == 4)

    # ---- Fast-Fail：核心是"不睡" ----
    t0 = time.monotonic()
    ok, left = breaker.fast_fail("jd", "京东")
    elapsed = time.monotonic() - t0
    check("退避期内 Fast-Fail 返回 False 且**不睡**",
          ok is False and left > 86000 and elapsed < 0.5,
          f"ok={ok} left={left:.0f} 耗时 {elapsed:.3f}s")
    check("Fast-Fail 不阻塞其它源", breaker.fast_fail("pdd", "拼多多")[0] is True)

    # ---- 成功重置 ----
    check("冷却期内 record_success 被拒绝（不许侥幸清零）",
          breaker.record_success("jd") is False and breaker.consecutive_trips("jd") == 4)

    breaker.clear("jd")
    breaker.trip("jd", 0.2, "短冷却")
    check("冷却未过期时 record_success 仍被拒绝", breaker.record_success("jd") is False)
    time.sleep(0.3)
    check("冷却过期后 record_success 清零成功", breaker.record_success("jd") is True)
    check("清零后连续次数归零", breaker.consecutive_trips("jd") == 0)
    check("清零后不再处于冷却", breaker.is_cooling("jd") is False)
    check("无记录时 record_success 返回 False（不白写盘）",
          breaker.record_success("xianyu") is False)

    breaker.clear()
    breaker.trip("jd", reason="访问频繁")
    check("清零后再次熔断回到阶梯第 1 级（30 分钟）",
          breaker.cooldown_remaining("jd") <= 1800
          and breaker.consecutive_trips("jd") == 1)
    breaker.clear()

    # ---- 阶梯可被环境变量覆盖（排障时压到秒级做演练）----
    with mock.patch.dict(os.environ, {"DIYPRICE_BREAKER_LADDER": "5,10,20"}):
        check("DIYPRICE_BREAKER_LADDER 覆盖生效",
              breaker.backoff_ladder() == (5.0, 10.0, 20.0)
              and breaker.backoff_for(3) == 20.0
              and breaker.backoff_for(7) == 20.0)
    with mock.patch.dict(os.environ, {"DIYPRICE_BREAKER_LADDER": "abc"}):
        check("阶梯环境变量非法时回落默认（不让采集崩）",
              breaker.backoff_ladder() == (1800.0, 7200.0, 21600.0, 86400.0))

    # ---- snapshot / active_summary 要带上"连续第几次" ----
    breaker.clear()
    breaker.trip("jd", reason="访问频繁")
    snap = breaker.snapshot()["jd"]
    check("snapshot 带连续次数与人类可读时长",
          snap["trips"] == 1 and bool(snap["remaining_text"]) and bool(snap["cooldown_text"]))
    check("active_summary 写明连续第几次（否则看起来像 bug）",
          "连续第 1 次" in breaker.active_summary())
    breaker.clear()

    # ---- 文件损坏时不能把采集全停掉 ----
    breaker.BREAKER_FILE.write_text("{ 这不是合法 JSON", encoding="utf-8")
    check("状态文件损坏时当成无冷却（不让采集停摆）",
          breaker.cooldown_remaining("jd") == 0.0)

    # ---- 开关 ----
    with mock.patch.dict(os.environ, {"DIYPRICE_BREAKER_DISABLE": "1"}):
        breaker.trip("jd", 60, "开关测试")
        check("DIYPRICE_BREAKER_DISABLE=1 时冷却被忽略",
              breaker.cooldown_remaining("jd") == 0.0
              and breaker.wait_until_ready("jd")[0] is True)


def test_zero_yield_reason() -> None:
    """0 条入库的原因文案，不能把"数据质量问题"说成"限流"。

    实测踩过：搜「RTX 5090 D 32G」返回 30 条报价，全部因离群被剔除，
    旧文案记成"疑似被限流" —— 会把排查方向从"匹配规则/基准价"
    带偏到"风控"，白查一轮。
    """
    from app.services.pipeline import zero_yield_reason

    nothing = zero_yield_reason(0, 0, 0)
    check("解析出 0 条 → 指向限流/结构变化",
          "限流" in nothing and "数据质量" not in nothing)

    rejected = zero_yield_reason(0, 4, 26)
    check("解析出 30 条但全被剔除 → 判为数据质量问题，不冤枉平台",
          "数据质量" in rejected and "非限流" in rejected, f"实际：{rejected}")
    check("文案带上具体条数（便于直接定位）",
          "30 条" in rejected and "未匹配 4" in rejected and "被过滤 26" in rejected)
    check("只有未匹配 / 只有被过滤，都算数据质量问题",
          "数据质量" in zero_yield_reason(0, 5, 0)
          and "数据质量" in zero_yield_reason(0, 0, 5))


def test_backoff_reset_rule() -> None:
    """成功重置的三个条件：采到数据 + 没熔断中止 + 不是空结果推断的限流。

    这条规则一旦被改宽（`if quotes:` 这种图省事的写法），退避阶梯就白设了 ——
    一次侥幸的少量结果会把连续计数清零，下一轮又立刻撞墙。
    """
    from app.collectors.base import should_reset_backoff

    check("正常采到数据且无限流 → 允许清零", should_reset_backoff(30, False, False))
    check("本轮 0 条 → 不清零", not should_reset_backoff(0, False, False))
    check("命中限流特征被熔断中止 → 不清零", not should_reset_backoff(30, True, False))
    check("连续空结果推断为限流 → 不清零", not should_reset_backoff(30, False, True))
    check("三种不合格条件同时成立也不清零",
          not should_reset_backoff(0, True, True))
    check("计数为 None/负数也不误判为成功",
          not should_reset_backoff(None, False, False)  # type: ignore[arg-type]
          and not should_reset_backoff(-1, False, False))


def test_error_severity() -> None:
    """错误分级：硬拦截 vs 软风控。

    **匹配顺序是这条的命门**：京东的拦截页文案同时含硬词（访问频繁）和
    软词（请稍后再试）。若先判软，这条真·硬拦截会被误降级成 15 分钟上限，
    等于放它反复撞 —— 顺序反了就是 bug。
    """
    from app.services import breaker

    check("访问频繁 → 硬拦截", breaker.classify("访问频繁") == "hard")
    check("系统繁忙 → 软风控", breaker.classify("系统繁忙") == "soft")
    check("error_code=40001 / HTTP 429 → 软风控",
          breaker.classify("error_code=40001") == "soft"
          and breaker.classify("HTTP 429") == "soft")
    check("滑动验证 / punish / 非法访问 → 硬拦截",
          breaker.classify("滑动验证") == "hard"
          and breaker.classify("punish") == "hard"
          and breaker.classify("非法访问") == "hard")
    jd_text = "抱歉由于访问频繁导致无法搜索，请稍后再试！"
    check("京东真实文案（硬词+软词并存）必须判硬拦截 —— 先硬后软",
          breaker.classify(jd_text) == "hard", f"实际 {breaker.classify(jd_text)}")
    check("未知原因 / 空原因按硬拦截处理（宁多等，不乱撞）",
          breaker.classify("某个没见过的原因") == "hard" and breaker.classify("") == "hard")


def test_soft_cap() -> None:
    """软风控走**专用递增阶梯**（15min / 2h / 4h），不再是一律封顶 15 分钟。

    ⚠️ 旧断言「软上限短于轮次间隔 → 软风控不会跳过任何一轮」是**刻意推翻**的。
       实测：09:46:14 退避到期 → 10:31:22 重试 → 10:31:35 立刻再中「系统繁忙」。
       说明"退避到期"≠"风控解除"，15 分钟封顶等于每小时都去撞一次。
       现在第 2 次起**就是要跨轮跳过**，给账号真实冷却期。
    """
    from app.services import breaker

    hard = breaker.backoff_ladder()
    check("硬拦截不受软阶梯影响（仍走完整阶梯 30min→…→24h）",
          breaker.backoff_for(1) == hard[0] and breaker.backoff_for(4) == hard[-1])

    check("软风控阶梯 = 15分钟 / 2小时 / 4小时",
          breaker.soft_ladder() == (900.0, 7200.0, 14400.0),
          f"实际 {breaker.soft_ladder()}")
    check("软风控第 1 次 15 分钟（短于轮次间隔 → 首犯不跳过任何一轮）",
          breaker.backoff_for(1, "soft") == 900.0)
    check("软风控第 2 次 2 小时（刻意跨轮跳过，给账号冷却期）",
          breaker.backoff_for(2, "soft") == 7200.0)
    check("软风控第 3 次起 4 小时封顶（末级不再递增）",
          breaker.backoff_for(3, "soft") == 14400.0
          and breaker.backoff_for(9, "soft") == 14400.0)
    check("软风控 0 / 负数取第 1 级（不越界）",
          breaker.backoff_for(0, "soft") == 900.0
          and breaker.backoff_for(-3, "soft") == 900.0)
    check("软风控第 2 次长于轮次间隔（60 分钟）→ 会跳过至少 1 个定时轮次",
          breaker.backoff_for(2, "soft") > 3600)
    check("软风控第 1 次**不**跳过任何轮次（15min < 60min）",
          breaker.backoff_for(1, "soft") < 3600)
    check("默认软上限 = 软阶梯末级（4 小时）", breaker.soft_cap() == 14400.0)

    with mock.patch.dict(os.environ, {"DIYPRICE_BREAKER_SOFT_CAP": "120"}):
        check("DIYPRICE_BREAKER_SOFT_CAP 可把软风控整体压到 120s（排障用）",
              breaker.soft_cap() == 120.0 and breaker.backoff_for(3, "soft") == 120.0)
    with mock.patch.dict(os.environ, {"DIYPRICE_BREAKER_SOFT_LADDER": "60,120"}):
        check("DIYPRICE_BREAKER_SOFT_LADDER 可覆盖软阶梯（排障用）",
              breaker.soft_ladder() == (60.0, 120.0)
              and breaker.backoff_for(1, "soft") == 60.0
              and breaker.backoff_for(5, "soft") == 120.0)


def test_soft_trip_end_to_end() -> None:
    """软风控 trip：第 1 次 15 分钟，连续第 2 次必须升到 2 小时（走真实 trip()）。

    ⚠️ 第 2 次这条是本轮修复的核心 —— 旧逻辑下第 2 次仍是 15 分钟，
       于是"退避到期即再拦"可以无限循环。
    """
    from app.services import breaker

    tmp = Path(tempfile.mkdtemp(prefix="diyprice_softcap_"))
    original = breaker.BREAKER_FILE
    breaker.BREAKER_FILE = tmp / "breaker.json"
    try:
        breaker.clear()
        breaker.trip("pdd", reason="系统繁忙")
        entry = breaker.entry_of("pdd")
        check("软风控第 1 次 trip 冷却 = 软阶梯第 1 级（15 分钟），不是硬阶梯的 30 分钟",
              880 < breaker.cooldown_remaining("pdd") <= 900,
              f"{entry['cooldown_text']}")
        check("记录里带 severity=soft", entry.get("severity") == "soft")
        check("snapshot 也带 severity", breaker.snapshot()["pdd"]["severity"] == "soft")

        breaker.trip("pdd", reason="系统繁忙")
        entry2 = breaker.entry_of("pdd")
        check("软风控连续第 2 次 trip 冷却升到 2 小时",
              7180 < breaker.cooldown_remaining("pdd") <= 7200,
              f"{entry2['cooldown_text']}")
        check("连续计数 trips 累加到 2", entry2.get("trips") == 2)
        check("第 2 次冷却长于轮次间隔 → 下一个定时轮次会被 Fast-Fail 跳过",
              breaker.cooldown_remaining("pdd") > 3600)

        breaker.clear()
        breaker.trip("jd", reason="访问频繁")
        check("硬拦截 trip 后仍是完整阶梯第 1 级（30 分钟）",
              1780 < breaker.cooldown_remaining("jd") <= 1800)
        check("记录里带 severity=hard", breaker.entry_of("jd").get("severity") == "hard")
    finally:
        breaker.BREAKER_FILE = original
        shutil.rmtree(tmp, ignore_errors=True)


def test_record_success_verified() -> None:
    """探针专用豁免：verified=True 允许在冷却期内清零；调度路径不许。"""
    from app.services import breaker

    tmp = Path(tempfile.mkdtemp(prefix="diyprice_verified_"))
    original = breaker.BREAKER_FILE
    breaker.BREAKER_FILE = tmp / "breaker.json"
    try:
        breaker.clear()
        breaker.trip("jd", reason="访问频繁")
        check("调度路径：冷却期内 record_success 被拒绝（默认 verified=False）",
              breaker.record_success("jd") is False
              and breaker.consecutive_trips("jd") == 1)
        check("探针路径：verified=True 可提前结束冷却",
              breaker.record_success("jd", verified=True) is True)
        check("清零后不再冷却且连续计数归零",
              not breaker.is_cooling("jd") and breaker.consecutive_trips("jd") == 0)
        check("无记录时 verified=True 也不白写盘",
              breaker.record_success("pdd", verified=True) is False)
    finally:
        breaker.BREAKER_FILE = original
        shutil.rmtree(tmp, ignore_errors=True)


def test_probe_gates() -> None:
    """探针的两道闸 + **失败不放大阶梯**。

    全程用假采集器 + 假浏览器，**不发任何真实请求**。
    """
    from app.collectors import policy
    from app.services import breaker, probe

    tmp = Path(tempfile.mkdtemp(prefix="diyprice_probe_"))
    b_orig, p_orig = breaker.BREAKER_FILE, probe.PROBE_STATE_FILE
    breaker.BREAKER_FILE = tmp / "breaker.json"
    probe.PROBE_STATE_FILE = tmp / "probe.json"
    try:
        breaker.clear()
        probe.reset_rate_limit()

        out = probe.probe("jd")
        check("闸 1：未处于退避期 → 跳过且不发请求",
              bool(out.skipped) and out.quotes == 0, out.skipped)

        breaker.trip("jd", reason="访问频繁")
        before = breaker.consecutive_trips("jd")

        class _FakeCollector:
            code, name, browser_site = "jd", "京东", "jd"

            def _search(self, page, product):
                raise policy.RateLimitError("jd", "访问频繁")

        class _FakePage:
            def __enter__(self):
                return object()

            def __exit__(self, *exc):
                return False

        class _FakeWorker:
            def page(self, **kw):
                return _FakePage()

            def stop(self):
                return {}

        class _FakeProduct:
            id, model, is_active = 1, "RTX 5070", True

        patches = [
            mock.patch("app.collectors.get_collectors", return_value=[_FakeCollector()]),
            mock.patch("app.services.browser_worker.get_worker", return_value=_FakeWorker()),
            mock.patch.object(probe, "_pick_canary", return_value=_FakeProduct()),
        ]
        for p in patches:
            p.start()
        try:
            failed = probe.probe("jd", force=True)
        finally:
            for p in patches:
                p.stop()

        check("探活失败（仍被限流）→ ok=False 且给出原因",
              failed.ok is False and "仍被限流" in failed.reason, failed.reason)
        check("**探针失败不放大阶梯**（连续次数不变）",
              breaker.consecutive_trips("jd") == before,
              f"{before} → {breaker.consecutive_trips('jd')}")

        out2 = probe.probe("jd")
        check("闸 2：刚探测过 → 被最小间隔挡住", "未到最小间隔" in out2.skipped, out2.skipped)

        class _OkCollector(_FakeCollector):
            def _search(self, page, product):
                return [object()] * 3

        patches = [
            mock.patch("app.collectors.get_collectors", return_value=[_OkCollector()]),
            mock.patch("app.services.browser_worker.get_worker", return_value=_FakeWorker()),
            mock.patch.object(probe, "_pick_canary", return_value=_FakeProduct()),
        ]
        for p in patches:
            p.start()
        try:
            recovered = probe.probe("jd", force=True)
        finally:
            for p in patches:
                p.stop()

        check("探活成功 → 清掉退避、恢复调度",
              recovered.ok is True and recovered.recovered is True
              and not breaker.is_cooling("jd"))
        check("探活成功也把连续计数清零", breaker.consecutive_trips("jd") == 0)
        check("探针限频默认 10 分钟（低于此值等于高频撞墙）",
              probe.min_interval() == 600.0)
    finally:
        breaker.BREAKER_FILE, probe.PROBE_STATE_FILE = b_orig, p_orig
        shutil.rmtree(tmp, ignore_errors=True)


def test_db_pragmas() -> None:
    """SQLite 并发配置：PRAGMA 是**连接级**的，必须每条新连接都带上。"""
    from app.db import busy_timeout_ms, connection_pragmas, engine

    p = connection_pragmas()
    check("journal_mode = wal（读写互不阻塞）", str(p["journal_mode"]).lower() == "wal", str(p))
    check("busy_timeout 已设置且 ≥ 1000ms（拿不到锁会等，不会立刻抛）",
          p["busy_timeout"] == busy_timeout_ms() and p["busy_timeout"] >= 1000,
          f"{p['busy_timeout']}ms")
    check("synchronous = NORMAL(1)", p["synchronous"] == 1)
    check("foreign_keys 仍开启（没被新配置挤掉）", p["foreign_keys"] == 1)
    with engine.connect() as c1, engine.connect() as c2:
        check("池内多条连接都带上 busy_timeout（漏挂事件就会退化）",
              c1.exec_driver_sql("PRAGMA busy_timeout").scalar() == busy_timeout_ms()
              and c2.exec_driver_sql("PRAGMA busy_timeout").scalar() == busy_timeout_ms())


def test_stale_log_threshold() -> None:
    """采集日志的"未收尾"判定阈值 —— 太小会误杀正在跑的轮次。

    拆分事务后，`_open_crawl_log()` 会立刻提交一条 running（管理页要能看到
    "进行中"），代价是进程被强杀时会留下永远"进行中"的记录，靠
    `reap_stale_crawl_logs()` 在下一轮收尾。阈值必须**大于看门狗**，
    否则会把正常在跑的轮次误判成异常中断。
    """
    from app.services.pipeline import _STALE_RUNNING_MINUTES

    watchdog_minutes = 30          # scripts/collect_scheduled.sh 的 MAX_COLLECT_SECONDS
    check("收尾阈值 > 采集看门狗（否则误杀正在跑的轮次）",
          _STALE_RUNNING_MINUTES > watchdog_minutes, f"{_STALE_RUNNING_MINUTES} 分钟")
    check("收尾阈值 > 正常一轮耗时的数倍（正常 9~10 分钟）",
          _STALE_RUNNING_MINUTES >= 30, f"{_STALE_RUNNING_MINUTES} 分钟")


def test_alias_variants() -> None:
    """别名变体的**召回**与**不越界** —— 用户明确要求"防过拟合"。

    放宽别名提高召回，但每一条都仍受字母数字边界校验约束，
    所以"同一型号的写法差异"能命中，"相邻型号"绝不能互相命中。
    这组断言就是这条边界的守卫。
    """
    from types import SimpleNamespace

    from app.seed_data import build_aliases, product_rows
    from app.services.normalize import ModelMatcher

    rows = {r["model"]: r for r in product_rows()}

    def matcher_for(*models: str):
        prods = [
            SimpleNamespace(
                id=i, model=m, category=rows[m]["category"], aliases=build_aliases(m)
            )
            for i, m in enumerate(models)
        ]
        return ModelMatcher(prods), {i: m for i, m in enumerate(models)}

    # ---- 召回：同一型号的不同写法都要命中 ----
    recall = [
        ("RTX 3080 12G", "影驰3080 12g星耀 锁算力 22年1月出厂"),   # 去品牌前缀
        ("RTX 3080 12G", "耕升3080 12GB 追风版 功能一切正常"),      # GB 写法
        ("RX 5500 XT 4G", "撼讯 5500XT 4G显卡 实拍，成色如图"),      # 粘连
        ("RX 5500 XT 4G", "蓝宝石RX 5500 XT 4G 显卡 双风扇"),        # 标准写法
        ("RTX 3060 Ti 8G", "七彩虹3060TI 8G 白ULTRA 原盒原码"),      # 粘连 + 全大写
        ("RTX 4090 D 24G", "RTX 4090D 24G 显卡 全新未拆封"),         # D 后缀粘连
    ]
    for model, title in recall:
        m, ids = matcher_for(model)
        hit = m.match(title, category=None)
        check(f"召回：{title[:26]}… → {model}",
              hit is not None and ids[hit] == model, f"实际 {ids.get(hit)}")

    # ---- 不越界：相邻型号 / 容量版本绝不能互相命中 ----
    guard = [
        ("影驰RTX 3070 Ti 8G显卡 三风扇", "RTX 3070 Ti 8G", "RTX 3070 8G"),
        ("影驰RTX 3070 8G显卡 三风扇", "RTX 3070 8G", "RTX 3070 Ti 8G"),
        ("华硕RTX 3080 10G显卡 三风扇", "RTX 3080 10G", "RTX 3080 12G"),
        ("华硕RTX 3080 12G显卡 三风扇", "RTX 3080 12G", "RTX 3080 10G"),
        ("七彩虹3060ti 8G 原盒原码", "RTX 3060 Ti 8G", "RTX 3060 8G"),
        ("七彩虹3060 8G 拆机件", "RTX 3060 8G", "RTX 3060 Ti 8G"),
    ]
    for title, want, avoid in guard:
        m, ids = matcher_for(want, avoid)
        got = ids.get(m.match(title, category=None))
        check(f"不越界：{title[:22]}… → {want}（不得落到 {avoid}）", got == want, f"实际 {got}")

    # ---- 禁止裸数字别名 ----
    # "去品牌"与"去容量"叠加会产出裸数字，它无法区分容量版本 ——
    # 实测把"华硕猛禽3080 vga联名 12G显卡"抢到 3080 10G。
    for model, banned in (
        ("RTX 3060 12G", "3060"),
        ("RTX 3080 12G", "3080"),
        ("RX 5500 XT 4G", "5500XT"),
        ("RX 5700 8G", "5700"),
    ):
        aliases = {a.strip() for a in build_aliases(model).split(",")}
        check(f"禁止裸数字别名：{model} 的别名不得含 {banned!r}", banned not in aliases)

    # ---- 品牌与数字粘连：修的是一个真 bug ----
    # 标题常写 "微星RTX4060 Ti魔龙"（品牌与数字粘连、后缀带空格），
    # 而 normalize_text 不会拆开粘连 —— 以数字开头的别名（"4060 Ti"）会因
    # **左边界校验失败**（前面是 "X"，属于 [0-9A-Z]）全部落空，
    # 只剩过宽的 "RTX4060" 命中，于是 4060 Ti 被判成 4060 8G。
    m, ids = matcher_for("RTX 4060 Ti 8G", "RTX 4060 Ti 16G", "RTX 4060 8G")
    got = ids.get(m.match("微星RTX4060 Ti魔龙X Trio 8G显卡", category=None))
    check("品牌粘连：RTX4060 Ti 不得被判成 RTX 4060 8G",
          got in ("RTX 4060 Ti 8G", "RTX 4060 Ti 16G"), f"实际 {got}")

    m, ids = matcher_for("RTX 5070 Ti 16G", "RTX 5070 12G")
    got = ids.get(m.match("技嘉RTX5070 Ti 魔鹰 16G 国行 双BIOS", category=None))
    check("品牌粘连：技嘉RTX5070 Ti 魔鹰 16G → RTX 5070 Ti 16G（不得落到 5070 12G）",
          got == "RTX 5070 Ti 16G", f"实际 {got}")

    # ---- 规模可控（别名爆炸会拖慢匹配）----
    counts = [len(build_aliases(r["model"]).split(",")) for r in product_rows()]
    check("每型号别名数 ≤ 40", max(counts) <= 40, f"最多 {max(counts)}")
    check("全库别名总数 ≤ 6000", sum(counts) <= 6000, f"共 {sum(counts)}")


def test_clean_noise_filters() -> None:
    """求购 / 多件打包的过滤：**既要拦住噪音，也不能误杀卖家帖**。

    这几条"不得误杀"的用例全部来自实测踩过的坑 —— 初版规则把它们全判成
    求购帖，会在采集时**删掉真实在售数据**。规则是在 10069 条存量明细上
    逐条量过误杀率才收敛成现在这样的。
    """
    # ⚠️ 必须起别名 —— `from ... import check` 会**遮蔽本模块的 check 助手**，
    #    于是第二个参数（布尔）会被当成 base_price 传进去，报 TypeError。
    from app.services.clean import check as clean_check

    blocked = [
        ("3070显卡，1500收，3070ti也行，一张就行，自用。", "求购"),
        ("620 收一个i5-12400F处理器，自用装机。要求12代酷睿正式版散片", "求购"),
        ("铭瑄 intel Arc B580 Photon 12G显卡 双风扇 自用收一张b580，颜色不限", "求购"),
        ("收英特尔270k plus，盒装自用组副机 1700收一个Intel Core Ultra 7", "求购"),
        ("三个a卡打包出，功能都好的，成色如图，打包包邮价", "打包"),
        ("一起24个打包出售拆机件，成色还可以", "打包"),
    ]
    for title, kind in blocked:
        keep, why = clean_check(1200.0, 1200.0, title)
        check(f"拦噪音：{title[:26]}… → {kind}", (not keep) and kind in why, why)

    allowed = [
        ("AMD Ryzen 5 7500F（二手）CPU 1、本店商品，绝不收购被封机码硬件", "「绝不收购」是否定语境"),
        ("蓝宝石 rx7900xt 20g超白金显卡 个人一手回收一张 有需要的老板拍", "「回收一张」是卖家在售"),
        ("i5 13400f CPU 编号3088收货请拍完整开箱视频，否则外观问题无法处理", "「收货」不是求购"),
        ("联力 O11 Dynamic EVO 机箱 1.全新未拆封 2.支持水冷 3.正品保证", "编号罗列的是规格"),
        ("影驰3060 金属大师12G，三风扇，金属背板，无拆无修", "正常在售"),
    ]
    for title, note in allowed:
        keep, why = clean_check(1200.0, 1200.0, title)
        check(f"不误杀：{note}", keep, why)


def test_wall_clock_limit() -> None:
    """采集的硬性墙钟上限（防"卡死无限挂着"）。

    2026-09-21 实测：17:00 那轮卡在 Playwright/CDP 等待里 **3 小时 27 分**，
    而 shell 看门狗没杀掉它 —— 那轮一直占着单实例锁，18/19/20 点三轮全废。
    所以加了这一层（独立线程 `os._exit`，不经过信号机制）。这条断言守住
    "上限必须大于正常一轮、又小于单实例锁的陈旧阈值"这个区间。
    """
    from app.services.pipeline import _DEFAULT_WALL_CLOCK_LIMIT, wall_clock_limit

    normal_round = 600          # 正常一轮约 9~10 分钟
    lock_stale = 2400           # collect_scheduled.sh 的 LOCK_STALE_SECONDS
    check("默认上限 > 正常一轮耗时的 2 倍", _DEFAULT_WALL_CLOCK_LIMIT > normal_round * 2,
          f"{_DEFAULT_WALL_CLOCK_LIMIT:.0f}s")
    check("默认上限 < 单实例锁的陈旧阈值（否则锁会被判陈旧而重复采集）",
          _DEFAULT_WALL_CLOCK_LIMIT < lock_stale, f"{_DEFAULT_WALL_CLOCK_LIMIT:.0f}s")
    check("未设环境变量时用默认值", wall_clock_limit() == _DEFAULT_WALL_CLOCK_LIMIT)
    with mock.patch.dict(os.environ, {"DIYPRICE_COLLECT_MAX_SECONDS": "300"}):
        check("DIYPRICE_COLLECT_MAX_SECONDS 可覆盖", wall_clock_limit() == 300.0)
    with mock.patch.dict(os.environ, {"DIYPRICE_COLLECT_MAX_SECONDS": "0"}):
        check("设 0 表示关闭兜底", wall_clock_limit() == 0.0)


def test_capacity_disambiguation() -> None:
    """显卡容量消歧：型号与容量分离时，**禁止随机漂移**。

    背景：像 "RTX4060 Ti" 这种"去容量"别名，8G 版和 16G 版**都有** ——
    改造前是"命中即返回"，等于按字典序随机挑一个。实测把
    "微星RTX4060 Ti魔龙X Trio 8G" 判成了 16G 版。

    这条断言的**底线**是最后两条：标题没写容量时，必须放弃入库，
    而不是随便挑一个把错容量写进库里 —— 错数据比缺数据更难发现。
    """
    from types import SimpleNamespace

    from app.services.normalize import (
        ModelMatcher,
        extract_capacities,
        model_capacity,
    )

    # ---- 容量抽取器 ----
    check("抽取 '8G' → {8}", extract_capacities("微星RTX4060 Ti魔龙X Trio 8G显卡") == {8})
    check("抽取 '12g' → {12}", extract_capacities("RTX3080 12g 星耀") == {12})
    check("抽取 'O16G'（电商常见写法，容量紧跟字母）→ {16}",
          extract_capacities("华硕DUAL GeForce RTX 5060 Ti O16G") == {16})
    check("抽取 'OC16G' → {16}",
          extract_capacities("索泰RTX5060Ti月白OC16G电竞") == {16})
    check("抽取 'O8G' → {8}", extract_capacities("华硕ATS-RTX5060TI-O8G") == {8})
    check("无容量标题 → 空集", extract_capacities("自用 3080 出，成色好") == set())
    check("左侧断言挡住从长数字里截断（'13080G' 不得抽出 80）",
          13080 % 10 != 0 or extract_capacities("13080G") == set(),
          str(sorted(extract_capacities("13080G"))))

    # ---- 型号主容量（取第一个）----
    check("型号 'RTX 4060 Ti 8G' → 8", model_capacity("RTX 4060 Ti 8G") == 8)
    check("型号 'RTX 3080 12G' → 12", model_capacity("RTX 3080 12G") == 12)
    check("型号 'DDR5 32GB 16GB×2 6000 C30' → 32（取第一个=整条容量）",
          model_capacity("DDR5 32GB 16GB×2 6000 C30") == 32)
    check("型号 'i5-12400F' 无容量 → None", model_capacity("i5-12400F") is None)

    # ---- 端到端决断 ----
    # ⚠️ 必须用**真实别名**（build_aliases），不能只拿型号本身当别名：
    #    容量消歧只在"多个容量版本共享同一条去容量别名"（如 "RTX4060 Ti"）
    #    时才会触发，用型号本身作别名根本构造不出这个平局。
    from app.seed_data import build_aliases

    def matcher_for(*models: str):
        prods = [
            SimpleNamespace(id=i, model=m, aliases=build_aliases(m), category="gpu")
            for i, m in enumerate(models)
        ]
        return ModelMatcher(prods), {i: m for i, m in enumerate(models)}

    pairs = [
        ("微星RTX4060 Ti魔龙X Trio 8G显卡", "RTX 4060 Ti 8G"),
        ("七彩虹RTX4060 Ti 16G 战斧豪华版", "RTX 4060 Ti 16G"),
        ("华硕RTX 3080 10G 显卡 三风扇", "RTX 3080 10G"),
        ("影驰3080 12g星耀 锁算力", "RTX 3080 12G"),
        ("华硕ATS-RTX5060TI-O8G 显卡", "RTX 5060 Ti 8G"),
        ("索泰RTX5060Ti月白OC16G电竞", "RTX 5060 Ti 16G"),
    ]
    for title, want in pairs:
        m, ids = matcher_for("RTX 4060 Ti 8G", "RTX 4060 Ti 16G",
                             "RTX 3080 10G", "RTX 3080 12G",
                             "RTX 5060 Ti 8G", "RTX 5060 Ti 16G")
        got = ids.get(m.match(title, category=None))
        check(f"容量消歧：{title[:26]}… → {want}", got == want, f"实际 {got}")

    # ---- 底线：无容量 / 容量矛盾 → 放弃入库（绝不漂移）----
    m, ids = matcher_for("RTX 3080 10G", "RTX 3080 12G")
    check("底线：标题无容量（'自用 3080 出'）→ 放弃入库，不漂移到 10G/12G",
          m.match("自用 3080 出，成色好，无拆无修", category=None) is None)

    m, ids = matcher_for("RTX 5060 Ti 8G", "RTX 5060 Ti 16G")
    check("底线：标题同时列了 16G 与 8G → 放弃入库，不随便挑",
          m.match("耕升RTX5060Ti 踏雪 16G/8G 游戏电竞", category=None) is None)

    # ---- 不误伤：单候选 / 同容量候选仍照常返回 ----
    m, ids = matcher_for("RTX 3070 Ti 8G")
    check("单候选不受影响：唯一型号照常命中",
          ids.get(m.match("影驰RTX3070 Ti 8G显卡", category=None)) == "RTX 3070 Ti 8G")

    m, ids = matcher_for("RTX 3080 12G", "RTX 3080 12G 白色版")
    check("同容量候选不受影响：容量一致时按最长关键词定，不返回 None",
          m.match("影驰3080 12g星耀", category=None) is not None)

    m, ids = matcher_for("i5-12400F")
    check("无容量型号（CPU）不受影响",
          ids.get(m.match("i5 12400F 散片", category=None)) == "i5-12400F")


def test_wall_clock_guard_absolute_time() -> None:
    """墙钟兜底必须基于**绝对时间戳 + 短轮询**，不能是一次长 sleep。

    2026-09-23 复盘发现的真实故障：机器休眠时 `time.sleep()` 的计时会被**冻结**
    （macOS 上它基于 mach_absolute_time，不计入休眠时长）。于是
    `time.sleep(35*60)` 在整夜休眠后几乎没走 —— 一轮采集的**墙钟**耗时被拉到
    **12~15 小时**，一直霸占单实例锁，9/23 整天只跑成了 1 轮。

    改法：记下 `time.time()`（Epoch 墙钟，**含**休眠时间）作起始值，
    守护线程每几秒醒一次比对绝对经过时长。机器睡 6 小时后唤醒，
    最迟一个轮询周期内就会自杀。
    """
    import inspect
    import subprocess
    import sys

    from app.services import pipeline

    src = inspect.getsource(pipeline._install_wall_clock_guard)
    check("用 time.time() 记起始时刻（绝对墙钟，含休眠）", "_time.time()" in src)
    # ⚠️ 断言**调用形式**（`_time.monotonic(`），不要断言"源码里不出现 monotonic"
    #    —— 注释里恰好写着"不能用 time.monotonic()"，负向检查会误报。
    check("**不用** _time.monotonic() —— 它在 macOS 上休眠期间会冻结",
          "_time.monotonic(" not in src)
    check("是短轮询循环，不是一次长 sleep",
          "while True" in src and "_time.sleep(poll)" in src)
    check("每轮重新比对绝对经过时长", "_time.time() - started_at" in src)
    check("轮询周期自适应（默认 5s，阈值很小时收紧到 1s 以内）",
          "min(5.0, limit / 10.0)" in src)

    # 行为验证：真起一个子进程装守护线程，看它是否准点以退出码 3 自杀。
    # 不启动浏览器 —— 只验证计时与强杀本身。
    prog = (
        "import os, sys, time\n"
        "sys.path.insert(0, '.')\n"
        "os.environ['DIYPRICE_COLLECT_MAX_SECONDS'] = '1.5'\n"
        "from app.services.pipeline import _install_wall_clock_guard, wall_clock_limit\n"
        "_install_wall_clock_guard(wall_clock_limit())\n"
        "time.sleep(120)\n"
    )
    t0 = time.monotonic()
    proc = subprocess.run([sys.executable, "-c", prog], capture_output=True,
                          text=True, cwd=str(Path(__file__).resolve().parent.parent),
                          timeout=30)
    elapsed = time.monotonic() - t0
    check("阈值 1.5s → 退出码为 3（硬退出）", proc.returncode == 3,
          f"实际 {proc.returncode}")
    check("阈值 1.5s → 在 1.5~3.5s 内精准触发",
          1.4 <= elapsed <= 3.5, f"实际 {elapsed:.2f}s")


def test_sleep_resistance_and_lock() -> None:
    """休眠对抗（caffeinate）与单实例锁的绝对时间戳判定。

    两者必须配套：caffeinate 让正常轮次不被休眠打断；万一还是被打断了，
    锁的陈旧判定要基于**绝对 Epoch 时间**，否则"只跑了 10 分钟"的进程
    可能已经霸占锁 12 小时。
    """
    from pathlib import Path

    script = (Path(__file__).resolve().parent / "collect_scheduled.sh").read_text(encoding="utf-8")

    check("用 caffeinate 阻止本轮休眠", "caffeinate -dimsu" in script)
    check("caffeinate 用 `-w $$` 跟脚本生命周期绑定（脚本退出自动收工）",
          "caffeinate -dimsu -w $$" in script)
    check("caffeinate 作为**独立后台进程**，不是包住采集命令",
          "caffeinate -dimsu -w $$ &" in script)
    check("退出时清理 caffeinate", "kill \"$CAFFEINATE_PID\"" in script)
    check("找不到 caffeinate 时明确告警，不静默跳过",
          "找不到 caffeinate" in script)

    check("锁里显式记录绝对起始时间戳", 'date +%s > "$LOCK/started"' in script)
    check("陈旧判定优先读显式时间戳", 'cat "$LOCK/started"' in script)
    check("没有显式时间戳才退回目录 mtime（兼容老锁）",
          'stat -f %m "$LOCK"' in script)
    check("接管日志里带上绝对时长与阈值（便于事后核对）",
          "绝对时长" in script and "阈值" in script)
    check("退出 trap 同时释放锁并收掉 caffeinate",
          "cleanup()" in script and "trap cleanup EXIT" in script)


def test_lifecycle_source_routing() -> None:
    """按型号生命周期做采集源路由（2026-09-24）。

    停产老硬件（GTX 10/16 系、RTX 20/30 系、RX 5000/6000 系、12 代及更早的
    Intel、5000 系及更早的 AMD）在京东/拼多多**已无正品新货** —— 发搜索要么
    0 条，要么把「显卡支架」「拆机风扇」这类配件当结果混进来。
    既占配额又抬风控概率。所以 **legacy 型号只走闲鱼**。

    ⚠️ 认不出的型号**一律判 legacy**（保守）：误判成 active 会让 JD/PDD
       白跑一轮（浪费配额 + 涨风控），误判成 legacy 只是少采两个平台
       （闲鱼仍覆盖）。两种错误里后者代价小得多。
    """
    import os

    from app.collectors.base import build_rotation
    from app.seed_data import (
        LIFECYCLE_ACTIVE, LIFECYCLE_LEGACY, SOURCE_LIFECYCLES,
        lifecycle_of, sources_for,
    )

    check("jd 只吃 active", SOURCE_LIFECYCLES["jd"] == frozenset({LIFECYCLE_ACTIVE}))
    check("pdd 只吃 active", SOURCE_LIFECYCLES["pdd"] == frozenset({LIFECYCLE_ACTIVE}))
    check("xianyu 吃全部", SOURCE_LIFECYCLES["xianyu"] ==
          frozenset({LIFECYCLE_ACTIVE, LIFECYCLE_LEGACY}))

    cases = [
        ("RTX 5090 32G", "gpu", "NVIDIA", LIFECYCLE_ACTIVE),
        ("RTX 4090 24G", "gpu", "NVIDIA", LIFECYCLE_ACTIVE),
        ("RTX 4090 D 24G", "gpu", "NVIDIA", LIFECYCLE_ACTIVE),
        ("RTX 3080 10G", "gpu", "NVIDIA", LIFECYCLE_LEGACY),
        ("RTX 2060 6G", "gpu", "NVIDIA", LIFECYCLE_LEGACY),
        ("GTX 1060 6G", "gpu", "NVIDIA", LIFECYCLE_LEGACY),
        ("RX 9070 XT 16G", "gpu", "AMD", LIFECYCLE_ACTIVE),
        ("RX 7600 8G", "gpu", "AMD", LIFECYCLE_ACTIVE),
        ("RX 6800 XT 16G", "gpu", "AMD", LIFECYCLE_LEGACY),
        ("RX 5500 XT 8G", "gpu", "AMD", LIFECYCLE_LEGACY),
        ("Arc B580 12G", "gpu", "Intel", LIFECYCLE_ACTIVE),
        ("Arc A750 8G", "gpu", "Intel", LIFECYCLE_LEGACY),
        ("Ryzen 7 9800X3D", "cpu", "AMD", LIFECYCLE_ACTIVE),
        ("Ryzen 5 7500F", "cpu", "AMD", LIFECYCLE_ACTIVE),
        ("Ryzen 7 5800X3D", "cpu", "AMD", LIFECYCLE_LEGACY),
        ("Core Ultra 9 285K", "cpu", "Intel", LIFECYCLE_ACTIVE),
        ("i5-14600KF", "cpu", "Intel", LIFECYCLE_ACTIVE),
        ("i5-13400F", "cpu", "Intel", LIFECYCLE_ACTIVE),
        ("i5-12400F", "cpu", "Intel", LIFECYCLE_LEGACY),
        ("i9-12900K", "cpu", "Intel", LIFECYCLE_LEGACY),
    ]
    wrong = [(m, lifecycle_of(m, c, b), want)
             for m, c, b, want in cases if lifecycle_of(m, c, b) != want]
    check(f"{len(cases)} 个型号的生命周期判定全部正确", not wrong, f"判错：{wrong[:3]}")
    check("Core Ultra 不能被判成 legacy（先认 Core Ultra 再匹配 i 前缀）",
          lifecycle_of("Core Ultra 7 270K Plus", "cpu", "Intel") == LIFECYCLE_ACTIVE)
    check("legacy 型号的采集源只有闲鱼",
          sources_for("GTX 1060 6G", "gpu", "NVIDIA") == ("xianyu",))
    check("active 型号走三平台",
          set(sources_for("RTX 5090 32G", "gpu", "NVIDIA")) == {"jd", "pdd", "xianyu"})

    # 轮转序列里绝不能出现 legacy（这是路由的**唯一**目的）
    class P:
        def __init__(self, m, c, b): self.model, self.category, self.brand = m, c, b

    pool = [P(m, c, b) for m, c, b, _ in cases]
    os.environ["DIYPRICE_FOCUS_CATEGORY"] = "gpu,cpu"
    try:
        for src in ("jd", "pdd"):
            rot = build_rotation(pool, source=src)
            bad = [p.model for p in rot
                   if lifecycle_of(p.model, p.category, p.brand) != LIFECYCLE_ACTIVE]
            check(f"{src} 轮转序列里零 legacy", not bad, f"混入：{bad[:3]}")
        rot_xy = build_rotation(pool, source="xianyu")
        check("闲鱼轮转包含 legacy", len(rot_xy) == len(pool))
        check("未知源不过滤（宽松兜底，不会把采集整个断掉）",
              len(build_rotation(pool, source="unknown_source")) == len(pool))

        # ⚠️ 光测 `build_rotation` 不够 —— 真正的调用方是 `pick_targets`，
        #    它忘了把 source 透传下去的话，路由就形同虚设，而上面的断言全绿。
        #    （反向验证抓到过这条虚守卫。）
        import inspect as _inspect

        from app.collectors import base as _base
        check("pick_targets 把 source 透传给 build_rotation",
              "build_rotation(products, source=source)" in _inspect.getsource(_base.pick_targets))
    finally:
        os.environ.pop("DIYPRICE_FOCUS_CATEGORY", None)


def test_queue_route_filter() -> None:
    """队列**补做**路径也必须过生命周期路由。

    ⚠️ 为什么单独测这一条：`pick_targets` 只过滤**新取**的型号。
       路由生效**之前**入队、当天仍是 pending 的 legacy 任务会绕过它被重做 ——
       只测 `build_rotation` / `pick_targets` 的话，这条路径漏了也全绿。

    这是**行为断言**（真造一个队列文件跑 `_next_batch`），不是源码子串断言 ——
    源码子串断言在这个工程已经漏过三次（见 MEMORY.md 教训 1）。
    """
    from datetime import date

    from app.collectors import base as _base
    from app.services import cursor as _cursor
    from app.services import task_queue as tq

    class P:
        def __init__(self, pid, m, c, b):
            self.id, self.model, self.category, self.brand = pid, m, c, b

    # id 1 / 3 是 legacy，2 / 4 是 active
    products = [
        P(1, "GTX 1060 6G", "gpu", "NVIDIA"),
        P(2, "RTX 5090 32G", "gpu", "NVIDIA"),
        P(3, "Arc A750 8G", "gpu", "Intel"),
        P(4, "Arc B580 12G", "gpu", "Intel"),
    ]
    day = date(2026, 9, 24)

    with tempfile.TemporaryDirectory() as td:
        qfile = Path(td) / "collect_queue.json"
        old_qfile = tq.QUEUE_FILE
        old_load, old_save = _cursor.load_offset, _cursor.save_offset
        old_focus = os.environ.get("DIYPRICE_FOCUS_CATEGORY")
        tq.QUEUE_FILE = qfile
        _cursor.load_offset = lambda src: 0          # 别动真游标
        _cursor.save_offset = lambda src, off: None
        os.environ["DIYPRICE_FOCUS_CATEGORY"] = "gpu"
        try:
            # --- 场景 A：队列够填满本轮（不走"补新取"分支）---
            tq.enqueue("jd", [(1, "GTX 1060 6G"), (3, "Arc A750 8G")], day)
            tq.enqueue("jd", [(2, "RTX 5090 32G")], day)
            batch = [t.model for t in _base._next_batch("jd", products, day, 2)]
            check("队列里的 legacy 任务不被派发给 jd",
                  "GTX 1060 6G" not in batch and "Arc A750 8G" not in batch,
                  f"实际派发：{batch}")
            check("队列里的 active 任务照常派发", "RTX 5090 32G" in batch, f"实际派发：{batch}")
            check("被排除的任务落盘为 routed 终态（不再占 pending）",
                  tq.pending("jd", day) == [] or
                  all(t.model not in ("GTX 1060 6G", "Arc A750 8G") for t in tq.pending("jd", day)),
                  f"仍 pending：{[t.model for t in tq.pending('jd', day)]}")

            # --- 场景 B：队列不够 → 走"补新取 + 重读队列"分支 ---
            tq.reset_running_and_pending("jd")
            tq.enqueue("jd", [(1, "GTX 1060 6G")], day)      # 只剩 legacy
            tq.enqueue("jd", [(2, "RTX 5090 32G")], day)
            batch_b = [t.model for t in _base._next_batch("jd", products, day, 3)]
            check("补新取后重读队列仍过滤 legacy（场景 B）",
                  "GTX 1060 6G" not in batch_b, f"实际派发：{batch_b}")
            check("补新取确实补到了 active 型号（场景 B）",
                  "RTX 5090 32G" in batch_b and "Arc B580 12G" in batch_b,
                  f"实际派发：{batch_b}")

            # --- 闲鱼：路由只管 jd/pdd，legacy 必须照常派发 ---
            tq.enqueue("xianyu", [(1, "GTX 1060 6G")], day)
            xy = [t.model for t in _base._next_batch("xianyu", products, day, 1)]
            check("闲鱼照常派发 legacy（路由只管 jd/pdd）", xy == ["GTX 1060 6G"], f"实际：{xy}")

            # --- 未登记的源：宽松兜底，不过滤 ---
            tq.enqueue("zol", [(1, "GTX 1060 6G")], day)
            z = [t.model for t in _base._next_batch("zol", products, day, 1)]
            check("未登记源宽松兜底不过滤（避免把采集整个断掉）", z == ["GTX 1060 6G"], f"实际：{z}")
        finally:
            tq.QUEUE_FILE = old_qfile
            _cursor.load_offset, _cursor.save_offset = old_load, old_save
            if old_focus is None:
                os.environ.pop("DIYPRICE_FOCUS_CATEGORY", None)
            else:
                os.environ["DIYPRICE_FOCUS_CATEGORY"] = old_focus


def test_subcategory_matrix() -> None:
    """厂商二级细分（N卡/A卡/I卡、IU/AU）。

    ⚠️ 用 products 表已有的 `brand` 字段做键，**不新增字段** ——
       品牌本来就是厂商维度，另造一套只会两处打架。
    ⚠️ 前端品牌 chip 用 `data-brand` 而非 `data-cat`，点击处理按属性分流，
       否则点「N卡」会把一级品类的选中态一起清掉。
    """
    import inspect
    import pathlib as _pl

    from app.api import routes
    from app.seed_data import SUBCATEGORIES, subcategories_of

    check("显卡有 N/A/I 三档", [i["code"] for i in SUBCATEGORIES["gpu"]] == ["NVIDIA", "AMD", "Intel"])
    check("处理器有 IU/AU 两档", [i["code"] for i in SUBCATEGORIES["cpu"]] == ["Intel", "AMD"])
    check("未知品类返回空", subcategories_of("nope") == [])

    meta = inspect.getsource(routes.get_meta)
    check("meta 暴露 subcategories", '"subcategories"' in meta)
    check("子分类计数按 brand 统计", "brand_counts" in meta)
    check("子分类计数同样只算 active", "Product.is_active.is_(True)" in meta)

    lp = inspect.getsource(routes.list_products)
    check("products 接口支持 brand 过滤", "brand: str | None = Query" in lp)
    check("快照结果也要按 brand 再筛一刀（build_snapshot 是全量快照）",
          'r.get("brand") or ""' in lp)

    js = (_pl.Path(__file__).resolve().parent.parent / "web" / "js" / "products.js").read_text(encoding="utf-8")
    check("前端渲染二级 chip", "subcategories" in js and "data-brand" in js)
    check("点品牌 chip 只重置本行选中态（不清掉一级品类）",
          "chip.closest('.chip-row')" in js)
    check("换一级品类时清空品牌", "state.brand = '';" in js)


def test_pdd_home_warmup() -> None:
    """拼多多搜索前必须先落地首页（2026-09-24 实测）。

    **直接深链到搜索 URL 会被重定向到 `psnl_verification.html`（安全验证）**，
    采集器把它判成限流 → 整轮 0 条 → 连续几次就吃满退避阶梯。
    今早 08:01 那次「命中 login.html → 24 小时退避」有一部分就是这个成因。

    先访问一次首页拿到会话上下文再搜索就正常 —— 实测连抓 3 个型号
    **3/3 成功、每个 20 条**（RTX 5090 ¥49830 / RTX 5080 ¥18300 /
    RTX 5070 Ti ¥9389），~8.7s/型号。

    ⚠️ 判据用「当前 URL 不含 search_result」而不是「只做一次」——
       被验证页劫持后（URL 变成 psnl_verification.html）下一轮会自动
       重新预热，**能自愈**，不用重启 Worker。
    """
    import inspect

    from app.collectors import pdd_source as S

    check("定义了首页常量", hasattr(S, "HOME_URL") and "yangkeduo.com" in S.HOME_URL)

    src = inspect.getsource(S.PddCollector._search)
    check("搜索前有预热步骤", "HOME_URL" in src)
    check("预热判据是「当前不在搜索页」而非「只做一次」（可自愈）",
          '"search_result" not in (page.url or "")' in src)
    check("预热失败不致命（不能让一次预热失败废掉整轮）",
          "except Exception:" in src and "预热失败不致命" in src)
    check("预热在 navigate 之前", src.index("HOME_URL") < src.index("policy.navigate(page, url"))


def test_schedule_avoids_commute() -> None:
    """采集触发时间必须避开通勤合盖窗口（2026-09-24 改）。

    用户通勤窗口 07:50~08:20 与 17:50~18:20，期间必须断电合盖装包。
    原来的**整点**调度正好压在窗口上：08:00 那轮在 08:05 被合盖打断，
    浏览器崩在 `Target page, context or browser has been closed`，
    闲鱼连续 3 个型号失败提前收工，墙钟 21分43秒（其中 14 分钟在睡）。

    挪到 **30 分**之后：
      · 07:30 那轮约 12 分钟跑完（~07:42），赶在 07:50 合盖之前
      · 08:30 那轮在通勤结束、开盖之后才触发，完全避开真空期
    ⚠️ 07:30 是**新增**的 —— 原来最早的日间轮次是 08:00，正落在窗口里。
    """
    import pathlib as _pl

    sh = (_pl.Path(__file__).resolve().parent / "service.sh").read_text(encoding="utf-8")
    check("触发分钟是 30，不是 0", '<key>Minute</key><integer>30</integer>' in sh)
    check("没有残留的整点触发", '<key>Minute</key><integer>0</integer>' not in sh)
    check("新增 07:30 轮次（赶在通勤合盖前跑完）",
          "for h in 2 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23; do" in sh)
    check("注释写明了通勤窗口这个原因", "07:50~08:20" in sh and "17:50~18:20" in sh)


def test_browser_launch_defense() -> None:
    """浏览器启动前防御：清残留 Singleton 锁 + 回收占端口的僵尸。

    2026-09-23 16:00 那轮三源全部失败，日志只有
    `connect ECONNREFUSED 127.0.0.1:9222` + `浏览器启动失败` ——
    浏览器**两次重建都没起来**，整轮白跑 4 分 49 秒。
    成因是上一轮回收时留下的 `SingletonLock` 指向已不存在的 PID，
    Chrome 启动时看到它**部分版本会直接退出**（不同版本行为不一致）。

    ⚠️ 两个函数各有一道**安全闸**，这是它们的核心价值：
       · `_clear_stale_singleton` 只在**没有活跃进程**时才清 ——
         否则会删掉正在运行实例的锁，两个 Chrome 争同一个 profile
       · `_reap_port_holders` 只杀**看起来是浏览器**的进程 ——
         端口号可能被别的服务碰巧占用，见谁杀谁会误伤
    """
    import inspect
    import os
    import tempfile
    from pathlib import Path

    from app.services import session as S

    src = inspect.getsource(S.launch_browser)
    check("启动前清残留锁", "_clear_stale_singleton(BROWSER_PROFILE)" in src)
    check("启动前回收端口僵尸", "_reap_port_holders(CDP_PORT)" in src)

    cs = inspect.getsource(S._clear_stale_singleton)
    check("有活跃进程时绝不动锁（安全闸一）", "if profile_pids(profile):" in cs)
    check("软链要单独判（SingletonLock 指向不存在的目标时 exists() 为 False）",
          "p.is_symlink() or p.exists()" in cs)

    rp = inspect.getsource(S._reap_port_holders)
    check("只杀看起来是浏览器的进程（安全闸二）", "_looks_like_browser(cmdline)" in rp)
    check("不杀自己", "if pid == me:" in rp)
    check("lsof 拿不到就跳过，不挡住启动", "except (OSError, subprocess.SubprocessError):" in rp)

    # 行为验证：三个锁（含悬空软链）必须全清掉
    with tempfile.TemporaryDirectory() as td:
        prof = Path(td)
        (prof / "SingletonSocket").write_text("x")
        (prof / "SingletonCookie").write_text("y")
        os.symlink("/nonexistent-host-99999", prof / "SingletonLock")
        removed = S._clear_stale_singleton(prof)
        check("三个残留锁全部清掉（含悬空软链）", len(removed) == 3,
              f"实际清了 {sorted(removed)}")
        check("清理后目录为空", not list(prof.iterdir()))

    check("空闲端口返回空列表（不误报）", S._reap_port_holders(9222) == [])


def test_scope_gpu_cpu_only() -> None:
    """监控范围 = GPU + CPU 双核心（2026-09-23 最终裁决）。

    内存曾被短暂加入又撤回，理由：
      · 单轮型号数 130 → 169，耗时 12.2 → ~15.9 分钟，
        **逼近拼多多 15 分钟软风控红线**
      · 8G 主力机承受不起每小时 25% 时间跑浏览器
      · 内存规格碎片化严重（频率/套条/时序），产出比过低

    ⚠️ 标签表 CATEGORIES **不能删** —— 归档品类的历史数据还在库里，
       标签缺失会让它们退化成裸 code 显示。
    """
    import inspect
    import pathlib as _pl

    from app.api import routes
    from app.seed_data import CATEGORIES, CATEGORY_GROUPS, CATEGORY_ORDER

    check("监控品类只有 gpu / cpu", CATEGORY_ORDER == ["gpu", "cpu"],
          f"实际 {CATEGORY_ORDER}")
    check("只有一个核心分组，且只含 gpu/cpu",
          len(CATEGORY_GROUPS) == 1 and CATEGORY_GROUPS[0]["categories"] == ["gpu", "cpu"])
    check("归档品类的标签仍在（历史数据要能显示中文）",
          all(k in CATEGORIES for k in ("ram", "ssd", "mb", "psu", "cooler", "case")))

    meta = inspect.getsource(routes.get_meta)
    # ⚠️ 必须断言**出现次数**，不能只查子串 —— get_meta 里有两处过滤
    #    （counts 与 brand_counts），只查子串时删掉其中一处断言照样通过。
    #    （反向验证抓到过这条虚守卫。）
    check("meta 的两处计数都过滤 is_active（否则显示 351 而列表只有 130）",
          meta.count("Product.is_active.is_(True)") >= 2,
          f"实际只出现 {meta.count('Product.is_active.is_(True)')} 次")

    sched = (_pl.Path(__file__).resolve().parent / "collect_scheduled.sh").read_text(encoding="utf-8")
    check("采集范围是 gpu,cpu（不含 ram）", "DIYPRICE_FOCUS_CATEGORY=gpu,cpu \\" in sched)
    check("采集范围注释里写明了为什么不要内存",
          "逼近拼多多 15 分钟软风控红线" in sched)


def test_market_hygiene() -> None:
    """行情底座的三层去污：聚合过滤 / 口径闸门 / 样本量闸门。

    2026-09-23 排查「+304% 暴涨」时定位到的三个独立成因，缺一个都会复发：

    1. **聚合层没过滤**（aggregate.py）
       旧 price_daily 87883 行里，按真实明细只算得出 701 行 ——
       **98% 的聚合行来自模拟数据**，而且停用平台（zol/pconline）的
       历史行也留着。上层所有指标都被污染。

    2. **口径闸门**（trend.py）
       「全市场最低价」是跨平台取 min，**样本一换结论就变**。
       RTX 5090 在 9/17 有 3 平台（最低 ¥7898 闲鱼二手），9/22 只剩 1 个
       （¥47777 拼多多全新）→ 直接取差值 = +505%，那是口径换了不是涨价。
       判据用**平台集合的 Jaccard 重合度 ≥ 0.5**，不是"平台数量 ≥2"
       （后者会让 123 个型号只剩 2 个有涨跌幅 —— 真实数据里大部分型号
        每天就只有闲鱼一个平台，那种情况**是可比的**）。

    3. **样本量闸门**
       单条样本的"最低价"就是那条本身，与几十条里的最低价不可比。
       i3-12100F：9/22 有 26 条（¥352）→ 9/23 只有 1 条（¥975）= +177% 假暴涨。
       要求两天各 ≥3 条，且样本量量级相当（≤4 倍）。
    """
    import inspect
    import pathlib as _pl

    from app.services import aggregate, trend

    agg = inspect.getsource(aggregate.refresh_daily)
    check("聚合层排除模拟记录（is_synthetic）", "is_synthetic.is_(False)" in agg)
    check("聚合层只统计激活平台（Platform.is_active）", "Platform.is_active.is_(True)" in agg)
    check("全量重算时整表清空（否则旧脏行留着 = 没清）",
          "delete(PriceDaily)" in agg and "if since is None" in agg)

    tr = inspect.getsource(trend.build_snapshot)
    check("口径闸门：平台集合 Jaccard ≥ 0.5", "len(a & b) / len(union) >= 0.5" in tr)
    check("样本量闸门：两天各 ≥3 条", "MIN_SAMPLES = 3" in tr)
    check("样本量量级相当（≤4 倍）", "min(a_n, b_n) * 4" in tr)
    check("不可比时不出涨跌幅（宁可缺不可错）", "if not comparable:" in tr and "pct = None" in tr)
    # ⚠️ 断言**赋值语句本身**，不能只断言它的组成部分 ——
    #    变异只替换这一行、保留 Jaccard 那行时，只查组成部分的断言会漏掉
    #    （实测：这条守卫一度是虚的）。
    check("三道闸门必须**同时**生效（same_channels + enough + balanced）",
          "comparable = same_channels and enough and balanced" in tr)
    check("abs 与 change_pct 同生共死", "comparable and last is not None" in tr)

    cs = inspect.getsource(trend._compute_market_series)
    check("序列记录每天的平台集合", 'series["platform_ids"]' in cs)
    check("序列记录每天的样本条数", 'series["sample_counts"]' in cs)
    check("只回填**前导** None（不能回填中间缺口和末尾 —— 那是错数据）",
          "for i, v in enumerate(seq):\n                if v is not None:\n                    break" in cs)


def test_latest_quote_fallback() -> None:
    """「最新有效报价兜底」：今天没轮巡到的型号，回退展示它最近一次的报价。

    为什么需要（2026-09-23）：整点分批轮巡 130 个型号，一天里前几轮跑不到的
    型号在界面上是空白。用户看到"没有数据"会以为系统坏了，其实是**还没轮到**。

    改造要点（两个都不做就会出问题）：
      1. 从末尾往前找**第一个有值**的日期 —— 不能直接取 `low[-1]`：
         `dates` 是该型号自己有数据的日期，但按 basis=new/used 切时，
         某天可能只有二手没有全新，那个位置就是 None，
         直接取末位会把"有数据的型号"误判成空白。
      2. 涨跌基准跟着**数据日期**走，不是跟着"今天"走 ——
         否则拿昨天的价跟"今天减 7 天"比，区间口径就错了。
    """
    import inspect
    import pathlib

    from app.services import trend

    src = inspect.getsource(trend.build_snapshot)
    check("返回 captured_date（这条数据是哪天的）", '"captured_date"' in src)
    check("返回 is_today（是否今日数据）", '"is_today"' in src)
    check("返回 stale_days（过期天数）", '"stale_days"' in src)
    check("从末尾往前找第一个有值的日期（不是直接取 low[-1]）",
          "for i in range(len(low) - 1, -1, -1)" in src and "low[i] is not None" in src)
    # ⚠️ 断言要精确到**那一行赋值**。只查子串 `last_date - timedelta(days=period)`
    #    会被同文件的 `prev_idx = _lookup_idx(...)` 那一行喂饱，
    #    变异改掉 prev 那行时守卫照样通过（实测踩过）。
    check("涨跌基准跟数据日期走，不是跟今天走",
          "prev = _lookup(dates, low, last_date - timedelta(days=period))" in src)
    check("全 None 时跳过该型号（不产出空行）", "if idx is None:" in src)

    # 前端：徽标函数必须存在且两个页面都在用
    web = pathlib.Path(__file__).resolve().parent.parent / "web" / "js"
    common = (web / "common.js").read_text(encoding="utf-8")
    check("前端有 freshBadge 徽标函数", "function freshBadge" in common)
    check("今日数据不加视觉噪音（返回空串）",
          "if (r.is_today) return '';" in common)
    check("「从没采到过」与「回退到历史」区分开（前者也返回空串）",
          "if (!r || !r.captured_date) return '';" in common)
    for page in ("products.js", "index.js"):
        t = (web / page).read_text(encoding="utf-8")
        check(f"{page} 使用了 freshBadge", "freshBadge(" in t)

    css = (pathlib.Path(__file__).resolve().parent.parent / "web" / "css" / "app.css").read_text(encoding="utf-8")
    check("徽标样式 .stale 已定义", ".stale {" in css)


def test_watchdog_group_broadcast() -> None:
    """看门狗必须**按进程组广播**，不能退回单 PID。

    这是 shell 层的机制，Python 单测覆盖不到，所以用一条**文本断言**守住。
    为什么值得守（2026-09-21 实测）：只对主进程 PID 发信号是**失败过**的 ——
    17:00 那轮主进程对 SIGTERM 无响应，衍生的 Chrome 与 Playwright 的
    node 驱动更是没人管，全部变成孤儿，卡了 3 小时 27 分，
    连带 18/19/20 点三轮全被单实例锁挡掉。

    实测组广播有效：组内 10 个进程 → TERM 后剩 2 个（正是无响应那类）
    → KILL 后**组完全清空**。
    """
    from app.cli import _own_process_group

    script_path = Path(__file__).resolve().parent / "collect_scheduled.sh"
    script = script_path.read_text(encoding="utf-8")

    check("看门狗向进程组广播 SIGTERM",
          'kill -TERM -- "-$pgid"' in script)
    check("看门狗有 5 秒缓冲后升级为 SIGKILL",
          'kill -KILL -- "-$pgid"' in script)
    check("PGID 在**超时后现读**（沿用启动时的旧组会误杀自己）",
          'pgid="$(ps -o pgid= -p "$COLLECT_PID"' in script)
    check("有「绝不杀自己所在组」的保护",
          "shell_pgid" in script and '"$pgid" = "$shell_pgid"' in script)
    check("进程组未隔离时退回单 PID 并记日志（不静默）",
          "进程组未隔离" in script)

    with mock.patch.dict(os.environ, {"DIYPRICE_OWN_PROCESS_GROUP": "0"}):
        check("DIYPRICE_OWN_PROCESS_GROUP=0 时不新建进程组（手动跑保留 Ctrl-C）",
              _own_process_group() is False)


def test_anti_popup_config() -> None:
    """防弹窗夺焦：离屏坐标 + 按 PID 隐藏 + **有条件**归还焦点。

    2026-09-21 实测过"窗口在屏幕外 ≠ 不抢焦点"：
        启动前前台 = Electron
        启动后立刻 = Google Chrome，并**一直持有到采集结束（113 秒）**
    真正的修法是三步（见 session.py），任何一步退回都会让弹窗回来。
    """
    import inspect

    from app.services import session
    from app.services.session import (
        DEFAULT_PROFILE,
        build_launch_args,
        find_browser,
    )

    args = build_launch_args(find_browser(), DEFAULT_PROFILE, 9222)
    pos = next((a for a in args if a.startswith("--window-position=")), "")
    check("离屏：启动参数带 --window-position", bool(pos), pos or "缺失")
    if pos:
        x, y = (int(v) for v in pos.split("=", 1)[1].split(","))
        check("离屏：坐标远在屏幕可视区之外", x < -1000 and y > 1000, f"({x},{y})")
    check("离屏：带 --window-size", any(a.startswith("--window-size=") for a in args))

    login_args = build_launch_args(find_browser(), DEFAULT_PROFILE, 9222, park_window=False)
    check("登录流程**不**离屏（否则用户看不到二维码）",
          not any(a.startswith("--window-position=") for a in login_args))

    hide_src = inspect.getsource(session.hide_browser_app)
    # ⚠️ 只断言**脚本模板**本身，别断言"源码里不出现某字符串" ——
    #    docstring 里的警告恰好会写出那个被禁止的写法，负向检查会误报。
    check("隐藏脚本按 **unix id** 定位目标进程（按应用名会连用户自己的浏览器一起藏掉）",
          "whose unix id is" in hide_src)
    check("隐藏脚本不按应用名定位（`whose name is \"Google Chrome\"` 这种写法会误伤）",
          'whose name is "Google Chrome"' not in hide_src)
    check("归还焦点拒绝激活 Google Chrome（那可能是用户自己的窗口）",
          'name == "Google Chrome"' in inspect.getsource(session.restore_front_app))
    check("后台保持线程的归还焦点是**有条件**的（只在最前台确实是 Chrome 时才还）",
          "frontmost_app_name()" in inspect.getsource(session._keep_hidden))
    check("启动前记录最前台应用（归还焦点的前提）",
          "frontmost_app_name" in inspect.getsource(session.hide_browser_app_soon))


def test_request_slimming_rules() -> None:
    """资源拦截：**图片必须放行** —— 它是滑块验证码的载体。

    实测（2026-09-21，闲鱼同一次搜索「RTX 5070 12G」，含基线复测）：

        现状            33 张商品卡    峰值 ~1009 MB
        --headless=new   3 张（-91%）  且页面命中「非法访问」
        拦图片           3 张（-91%）  内存只省约 10%（在噪声范围内）
        关 site isolation 33 张        内存反而 +6%
        限 V8 堆 256MB   33 张        内存 -1%（噪声内）

    结论：拦图片/开 headless 都会把数据打到 1/10，**代价与收益严重不成比例**。
    这条断言守住这两条红线。
    """
    from app.services.browser_worker import should_block

    for name in ("a.png", "b.jpg", "c.jpeg", "d.webp", "e.gif", "f.svg"):
        check(f"图片必须放行：{name}", should_block(f"https://cdn.example.com/{name}", "image") is False)

    check("拦媒体（media）", should_block("https://cdn.example.com/v.mp4", "media") is True)
    check("拦字体（font）", should_block("https://cdn.example.com/f.woff2", "font") is True)
    check("字体扩展名兜底（resource_type 被归成 other 时）",
          should_block("https://cdn.example.com/f.woff2", "other") is True)

    for name in ("punish.js", "rgv587.js", "risk.js", "anti.js", "um.js", "geetest.js"):
        check(f"平台安全 SDK / 验证码必须放行：{name}",
              should_block(f"https://cdn.example.com/{name}", "script") is False)

    for rt in ("document", "script", "xhr", "fetch", "stylesheet"):
        check(f"取数必需的资源类型不碰：{rt}",
              should_block("https://cdn.example.com/anything", rt) is False)


# ====================================================================== 主流程

def main() -> int:
    tests = (
        test_ranking_direction,
        test_ranking_empty,
        test_request_slimming,
        test_policy_delays,
        test_rate_limit_detection,
        test_breaker,
        test_backoff_reset_rule,
        test_error_severity,
        test_soft_cap,
        test_soft_trip_end_to_end,
        test_record_success_verified,
        test_probe_gates,
        test_db_pragmas,
        test_stale_log_threshold,
        test_wall_clock_limit,
        test_wall_clock_guard_absolute_time,
        test_sleep_resistance_and_lock,
        test_latest_quote_fallback,
        test_market_hygiene,
        test_scope_gpu_cpu_only,
        test_browser_launch_defense,
        test_schedule_avoids_commute,
        test_pdd_home_warmup,
        test_lifecycle_source_routing,
        test_queue_route_filter,
        test_subcategory_matrix,
        test_watchdog_group_broadcast,
        test_anti_popup_config,
        test_request_slimming_rules,
        test_alias_variants,
        test_capacity_disambiguation,
        test_clean_noise_filters,
        test_zero_yield_reason,
    )
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            # 单个用例崩掉不该掩盖其余断言 —— 否则无法一次看清所有回归点
            FAILED.append(f"{fn.__name__} 抛异常：{type(exc).__name__}: {exc}")

    for name in PASSED:
        print(f"  ✅ {name}")
    for name in FAILED:
        print(f"  ❌ {name}")
    print()
    print(f"通过 {len(PASSED)} / 共 {len(PASSED) + len(FAILED)}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
