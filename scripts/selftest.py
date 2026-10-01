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

    def on(self, *_a, **_k):
        """响应监听 —— 真实 Playwright 页面必有；假页面空实现即可。"""
        return self

    def remove_listener(self, *_a, **_k):
        return None

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
        # ⚠️ 这条用例原来写的是「正文含裸 40001 → 期望命中 '40001'」，
        #    **它把 bug 钉死了**：正文里出现 40001 恰恰是误判的来源。
        #    2026-10-01 改为真实形态 —— 错误码出现在 URL 上。
        ("pdd", _FakePage("https://mobile.yangkeduo.com/psnl_verification.html?error_code=40001"),
         "error_code=40001", "拼多多验证页（错误码在 URL 上）"),
        ("pdd", _FakePage("https://mobile.yangkeduo.com/x", "", '{"ret":"FAIL","error_code=40001"}'),
         "error_code=40001", "拼多多 JSON 错误码（带键名）"),
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
        # 🐞 2026-10-01 真实误熔断的原文（data/collect.log 23:31:33）：
        #    正文里 "RTXA400016G"（显卡型号 RTX A4000 16G）包含子串 "40001"，
        #    被当成了拼多多错误码 → 整批中止 + 推进退避阶梯。
        #    这是一页**正常搜索结果**（有价格、有"56人想拼"），绝不能判限流。
        ("pdd", _FakePage("https://mobile.yangkeduo.com/search_result.html?search_key=i9-14900K",
                          "拼多多",
                          "想P3图形工作站台式机升级i9-14900K/128G/1TSSD /RTXA400016G 仅剩5件 "
                          "24小时内发货 假一赔十 ¥ 35000 56人想拼 "
                          "英特尔(Intel)酷睿14代 i9处理器14900K 24核32线程 五年质保")),
        ("xianyu", _FakePage("https://www.goofish.com/search?q=A4000", "闲鱼",
                             "RTX A4000 16G 专业卡 ¥3999 56人想要 广州 全新")),
    ]
    false_alarms = []
    for code, page in negatives:
        hit = policy.detect_rate_limit(page, code)
        if hit:
            false_alarms.append(f"{code}: 误判为「{hit[0]}」")
    check("真阴性：正常商品页不会被误判为限流", not false_alarms, "; ".join(false_alarms))

    # ---- 结构守卫：裸数字特征一律不允许（钉住**这一类**，不只是一个 PDD）----
    #     真正的闸门在 ThrottlePolicy.__post_init__，这里验证那道闸门会拦。
    bad_digits = [
        (code, ind)
        for code, pol in policy.PLATFORM_THROTTLE_CONFIG.items()
        for ind in pol.rate_limit_indicators
        if ind.isdigit()
    ]
    check("没有任何平台使用「纯数字」限流特征（会被商品型号撞上）",
          not bad_digits, f"实际 {bad_digits}")

    try:
        policy.ThrottlePolicy(code="t", label="测试", rate_limit_indicators=("40001",))
        guard_raised = False
    except ValueError:
        guard_raised = True
    check("构造期就拒绝裸数字特征（把 bug 挡在入口，而不是等它误熔断）", guard_raised)

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
    # 第 1 级原值 30 分钟，但**短于轮次间隔（1 小时）** —— 调度上等于不存在
    # （fast_fail 在下一轮开始时才评估冷却，那时早已过期），所以被抬到
    # 最短生效时长。阶梯原值 vs 生效时长的区别见 test_backoff_decay_and_floor。
    check("trip 走阶梯第 1 级，并被抬到最短生效时长（> 1 小时）",
          breaker.cooldown_remaining("jd") > 3600.0,
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
    check("冷却过期后 record_success 被接受（余量 1 → 清掉）",
          breaker.record_success("jd") is True)
    check("清零后连续次数归零", breaker.consecutive_trips("jd") == 0)
    check("清零后不再处于冷却", breaker.is_cooling("jd") is False)
    check("无记录时 record_success 返回 False（不白写盘）",
          breaker.record_success("xianyu") is False)

    breaker.clear()
    breaker.trip("jd", reason="访问频繁")
    check("余量减到 0 后再次熔断，从阶梯第 1 级重新开始",
          3600 < breaker.cooldown_remaining("jd") <= breaker.min_cooldown() + 5
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
    """软风控 trip：走**独立**阶梯 —— 连续被拦时逐级放大，且第 3 级短于硬阶梯。

    ⚠️ 两个关键点：
      · 第 2 次必须升到 2 小时 —— 旧逻辑下第 2 次仍是 15 分钟，
        于是"退避到期即再拦"可以无限循环。
      · 软/硬的区分点**不在第 1 级**：第 1 级软(15分)/硬(30分)都短于轮次间隔，
        在每小时一轮的调度上等于不存在，统一被 min_cooldown() 抬到 65 分钟 ——
        所以第 1 级两者相同。真正的区分从第 3 级开始（软 4 小时 vs 硬 6 小时）。
    """
    from app.services import breaker

    tmp = Path(tempfile.mkdtemp(prefix="diyprice_softcap_"))
    original = breaker.BREAKER_FILE
    breaker.BREAKER_FILE = tmp / "breaker.json"
    try:
        breaker.clear()
        breaker.trip("pdd", reason="系统繁忙")
        entry = breaker.entry_of("pdd")
        check("软风控第 1 次 trip 被抬到最短生效时长（原值 15 分钟在调度上空转）",
              breaker.cooldown_remaining("pdd") > 3600,
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

        breaker.trip("pdd", reason="系统繁忙")
        check("软风控第 3 次 → 4 小时（软阶梯末级）",
              14000 < breaker.cooldown_remaining("pdd") <= 14400,
              f"实际 {breaker.cooldown_remaining('pdd'):.0f}s")
        check("软风控确实没套用硬阶梯（第 3 次若是硬阶梯会是 6 小时）",
              breaker.cooldown_remaining("pdd") < 21600)

        # 硬阶梯的区分点在第 3 级：6 小时 vs 软风控的 4 小时
        breaker.clear()
        for _ in range(3):
            breaker.trip("jd", reason="访问频繁")
        check("硬拦截走硬阶梯（第 3 次 = 6 小时，与软风控的 4 小时不同）",
              21500 < breaker.cooldown_remaining("jd") <= 21600,
              f"实际 {breaker.cooldown_remaining('jd'):.0f}s")
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


def test_report_sections() -> None:
    """日报板块化：品类 × 厂商 × 品相各自成块，**统计与涨跌榜只在块内**。

    ⚠️ 这里测的是**口径**不是排版：混排会让「涨跌榜」失去解释力
       （A卡的涨幅把 N卡的行情顶掉、闲鱼二手盖过京东新品）。
       所以断言写成"板块里的行必须同品牌" + "涨跌榜只来自喂进去的那个子池"，
       而不是"页面上有几个 div"。
    """
    from app.services.report import (
        SECTION_BASIS,
        _day_gap,
        change_basis_label,
        platforms_for_basis,
        section_movers,
        section_watch,
        split_sections,
    )

    check("板块品相维度是 全新 + 二手 两档",
          [b for b, _ in SECTION_BASIS] == ["new", "used"])

    # ---- 契约①：全新 / 二手 的**数据源隔离** ------------------------------
    # 这是本次分板块重构的核心契约：全新只吃京东+拼多多，二手只吃闲鱼。
    # 抽成纯函数就是为了能在这里直接断言，而不是"看代码觉得对"。
    class Plat:
        def __init__(self, code, kind):
            self.code, self.kind = code, kind

    pool = [Plat("jd", "new"), Plat("pdd", "new"), Plat("tmall", "new"),
            Plat("xianyu", "used"), Plat("zhuanzhuan", "used")]
    sel_new = platforms_for_basis(pool, "new")
    sel_used = platforms_for_basis(pool, "used")
    check("全新口径只留 kind=new 的平台",
          {p.code for p in sel_new} == {"jd", "pdd", "tmall"},
          f"实际 {[p.code for p in sel_new]}")
    check("二手口径只留 kind=used 的平台",
          {p.code for p in sel_used} == {"xianyu", "zhuanzhuan"},
          f"实际 {[p.code for p in sel_used]}")
    check("全新与二手平台集合**不相交**（数据源隔离的硬约束）",
          not ({p.code for p in sel_new} & {p.code for p in sel_used}))
    check("全市场口径不过滤", len(platforms_for_basis(pool, "all")) == len(pool))
    check("未知品相按全市场处理（宽松兜底，不会把日报整个断掉）",
          len(platforms_for_basis(pool, "weird")) == len(pool))

    def cell(change, name="京东", real=True, gap=1, suspect=False):
        return {"name": name, "is_real": real, "change": change,
                "gap_days": gap, "suspect": suspect, "price": 1000.0, "has_data": True,
                "change_basis": change_basis_label(gap)}

    def row(pid, model, brand, changes, **extra):
        base = {"product_id": pid, "model": model, "short_model": model, "brand": brand,
                "brand_code": brand.lower(),
                "day_low": 1000.0, "day_avg": 1010.0, "day_samples": 3,
                "day_change": None, "day_change_pct": None,
                "hist_low": None, "value_index": 1.0,
                "value_index_prev": None, "value_index_change": None,
                "avg_trend": {"dates": ["2026-09-23", "2026-09-24"], "avg": [1000.0, 1010.0],
                              "low": [980.0, 1000.0], "samples": [2, 3]},
                "has_real": True, "captured_date": "2026-09-24", "is_today": True,
                "stale_days": 0, "cheapest_platform": {"name": "京东"}, "platforms": changes}
        base.update(extra)
        return base

    rows = [
        row(1, "RTX 5090 32G", "NVIDIA", [cell(300.0)]),
        row(2, "RTX 5070 12G", "NVIDIA", [cell(-120.0)]),
        row(3, "RX 9070 XT 16G", "AMD", [cell(9999.0)]),          # 涨幅远大于 N 卡
        row(4, "Arc B580 12G", "Intel", [cell(-5.0)]),
        row(5, "神秘卡 X", "S3", [cell(50.0)]),                    # 未登记的厂商
    ]
    subs = [
        {"code": "NVIDIA", "label": "N卡", "hint": "NVIDIA GeForce"},
        {"code": "AMD", "label": "A卡", "hint": "AMD Radeon"},
        {"code": "Intel", "label": "I卡", "hint": "Intel Arc"},
    ]

    secs = split_sections("gpu", "new", rows, subs)
    by_key = {s["key"]: s for s in secs}
    check("板块数 = 子分类数 + 未登记厂商兜底块", len(secs) == 4, f"实际 {[s['title'] for s in secs]}")

    for s in secs:
        bad = [r["model"] for r in s["rows"] if r["brand"] != s["brand"]]
        check(f"板块「{s['title']}」只含本品牌的行", not bad, f"混入：{bad}")

    n_sec = by_key["gpu|NVIDIA|new"]
    check("N卡板块拿到 2 行", len(n_sec["rows"]) == 2)
    check("N卡板块标题带品相", n_sec["title"] == "N卡 · 全新在售", n_sec["title"])
    check("板块带小写厂商码（nvidia / amd / intel）",
          n_sec["brand_code"] == "nvidia" and by_key["gpu|AMD|new"]["brand_code"] == "amd")

    # ---- 契约②：N/A/I 三个阵营**互不穿透** --------------------------------
    ids = {s["brand"]: {r["product_id"] for r in s["rows"]} for s in secs}
    nv, am, it = ids.get("NVIDIA", set()), ids.get("AMD", set()), ids.get("Intel", set())
    check("N卡 / A卡 / I卡 的型号集合两两不相交（互不穿透）",
          not (nv & am) and not (nv & it) and not (am & it),
          f"N∩A={nv & am} N∩I={nv & it} A∩I={am & it}")
    check("每行只归属一个板块（不丢行、不重复计入）",
          sum(len(v) for v in ids.values()) == len(rows),
          f"板块合计 {sum(len(v) for v in ids.values())} / 原始 {len(rows)}")

    # ---- 契约③：卡片必须透出的字段齐全 ------------------------------------
    need = ("brand_code", "day_low", "day_avg", "day_samples",
            "day_change", "day_change_pct", "value_index", "value_index_change", "avg_trend")
    missing = [k for k in need if k not in n_sec["rows"][0]]
    check("行带齐「最低价 / 均价 / 样本数 / 日环比额幅 / 性价比异动 / 均价走势」字段",
          not missing, f"缺：{missing}")

    # ⚠️ 核心：涨跌榜只在该板块的子池里排。AMD 那条 +9999 绝不能出现在 N 卡榜上。
    up_models = [m["model"] for m in n_sec["movers"]["up"]]
    check("N卡涨跌榜不含 A卡型号（不跨品牌混排）",
          "RX 9070 XT 16G" not in up_models, f"实际 {up_models}")
    check("N卡涨跌榜就是自己的 +300", up_models == ["RTX 5090 32G"], f"实际 {up_models}")
    check("N卡跌榜是自己那条 -120",
          [m["model"] for m in n_sec["movers"]["down"]] == ["RTX 5070 12G"])

    a_sec = by_key["gpu|AMD|new"]
    check("A卡板块的涨幅榜是自己的 +9999（不是被 N 卡压掉）",
          [m["model"] for m in a_sec["movers"]["up"]] == ["RX 9070 XT 16G"])

    other = [s for s in secs if s["brand"] == "S3"]
    check("未登记厂商不被静默丢弃（兜底成独立板块）", len(other) == 1 and len(other[0]["rows"]) == 1)

    # 空板块也要保留（前端才能如实说"本期无数据"，而不是装作没有这一块）。
    # 注意：没有数据时也就不会有"未登记厂商"的兜底块，所以这里只有子分类那 3 块。
    empty_secs = split_sections("gpu", "used", [], subs)
    check("无数据的板块仍然返回（标 empty 而不是消失）",
          len(empty_secs) == len(subs) and all(s["empty"] for s in empty_secs),
          f"实际 {[(s['title'], s['empty']) for s in empty_secs]}")

    # ---- 涨跌榜口径：跨天 / 标疑 一律不上榜，但**如实报出条数**
    mixed = [
        row(10, "跨天卡", "NVIDIA", [cell(-5000.0, gap=7)]),
        row(11, "标疑卡", "NVIDIA", [cell(-8000.0, suspect=True)]),
        row(12, "正常卡", "NVIDIA", [cell(-90.0, gap=1)]),
        row(13, "模拟卡", "NVIDIA", [cell(-7777.0, name="模拟", real=False)]),
    ]
    mv = section_movers(mixed)
    downs = [m["model"] for m in mv["down"]]
    check("跨天比较不上涨跌榜", "跨天卡" not in downs, f"实际 {downs}")
    check("标疑变动不上涨跌榜", "标疑卡" not in downs, f"实际 {downs}")
    check("模拟平台不上涨跌榜", "模拟卡" not in downs, f"实际 {downs}")
    check("正常变动照常上榜", downs == ["正常卡"], f"实际 {downs}")
    check("被排除的条数如实报出（不静默丢）",
          mv["stale_excluded"] == 1 and mv["suspect_excluded"] == 1,
          f"stale={mv['stale_excluded']} suspect={mv['suspect_excluded']}")
    check("榜上条目带平台名与比较基准",
          mv["down"][0]["platform"] == "京东" and mv["down"][0]["change_basis"] == "日间")
    # ---- 重点观察：按型号去重、有名额上限、每条带 reason
    w = section_watch(mixed, limit=4)
    ids = [x["product_id"] for x in w]
    check("重点观察按型号去重", len(ids) == len(set(ids)), f"{ids}")
    check("重点观察不超过名额上限", len(w) <= 5)
    check("重点观察每条都带 reason（不做黑箱推荐）",
          all(x.get("reason") for x in w), f"{[x.get('reason') for x in w]}")

    # 性价比异动是**独立指标**：性价比 = 跑分÷日低价，日低价跌 → 性价比升，
    # 符号与价格涨跌相反，不能拿涨跌幅代替。
    #
    # ⚠️ 数据要设计成"性价比异动的那条不是涨跌榜首" —— 否则它会被涨跌规则
    #    先挑走，去重之后性价比规则就没机会，测试会假绿（实测踩过一次）。
    vi_rows = [
        row(30, "性价比异动卡", "NVIDIA", [cell(-5.0)],
            value_index=2.5, value_index_prev=1.0, value_index_change=1.5),
        row(31, "涨幅榜首卡", "NVIDIA", [cell(50.0)],
            value_index=1.2, value_index_prev=1.19, value_index_change=0.01),
        row(32, "跌幅榜首卡", "NVIDIA", [cell(-500.0)],
            value_index=1.0, value_index_prev=1.0, value_index_change=0.0),
    ]
    w3 = section_watch(vi_rows, limit=5)
    check("「性价比异动」按 |变化| 挑（不是按涨跌幅）",
          any(x["reason"] == "性价比异动" and x["product_id"] == 30 for x in w3),
          f"{[(x['reason'], x['product_id']) for x in w3]}")
    check("涨跌两端与性价比异动可以共存（不互相挤掉）",
          {"涨幅最大", "跌幅最大", "性价比异动"} <= {x["reason"] for x in w3},
          f"{[x['reason'] for x in w3]}")

    # 贴近史低最多占 2 个名额，否则会把涨跌两端挤光
    low_rows = [
        row(20, f"低价卡{i}", "NVIDIA", [cell(0.0)])
        for i in range(4)
    ]
    for r in low_rows:
        r["hist_low"] = 1000.0        # day_low == hist_low → 全部"贴近史低"
    w2 = section_watch(low_rows, limit=4)
    near = [x for x in w2 if x["reason"] == "贴近史低"]
    check("「贴近史低」最多占 2 个名额（否则挤掉涨跌两端）",
          len(near) <= 2, f"实际 {len(near)}")

    # ---- 比较跨度标注：日间 / 批次 / 跨N天
    check("同日 → 0 天", _day_gap("2026-09-24", "2026-09-24") == 0)
    check("隔一天 → 1 天", _day_gap("2026-09-24", "2026-09-23") == 1)
    check("跨 6 天（旧的「日间」标签会骗人）", _day_gap("2026-09-22", "2026-09-16") == 6)
    check("缺日期不炸", _day_gap(None, "2026-09-23") is None
          and _day_gap("2026-09-24", None) is None)
    check("日期串畸形不炸（返回 None 而不是抛异常）",
          _day_gap("not-a-date", "2026-09-23") is None)
    check("比较基准标签：同批次 → 「批次」", change_basis_label(0) == "批次")
    check("比较基准标签：隔一天 → 「日间」", change_basis_label(1) == "日间")
    check("比较基准标签：跨 6 天 → 「跨6天」（不再冒充日间）",
          change_basis_label(6) == "跨6天")
    check("比较基准标签：无基准 → None", change_basis_label(None) is None)


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


def test_alerting_rules() -> None:
    """告警规则与抑制。

    为什么必须守：2026-09-29 全天 15 轮 0 条、覆盖 0/130，跑了一整天**没人知道** ——
    项目当时只有 collect.log，没有任何主动通知。告警是新加的关键机制，
    它自己失效的话，下次照样是"挂了没人知道"。

    守三件事：
      1. 该报的报（0 条 / 单源连续失败 / 全源不可用）
      2. 不该报的不报（有数据、熔断跳过）
      3. 抑制有效（否则持续故障每轮一条，人会对告警脱敏，比不报还糟）
    """
    from app.services import alerting, healthcheck

    import json  # 本模块顶部没导 json（各测试按需导入）

    rules = {"zero_yield_rounds": 1, "source_fail_rounds": 3, "model_stale_days": 7}

    def src(code, status, items=0):
        return {"source": code, "status": status, "items": items}

    # ---- ① 本轮 0 条 → critical ----
    a, zs, f = healthcheck.evaluate_round(
        {"listings": 0, "sources": [src("jd", "failed"), src("xianyu", "failed")]},
        zero_streak=0, fails={}, rules=rules)
    check("本轮 0 条 → 报 critical",
          any(x["level"] == "critical" and x["key"] == "zero-yield" for x in a),
          f"{[x['key'] for x in a]}")
    check("0 条时连续计数 +1", zs == 1, f"实际 {zs}")
    check("0 条告警的标题带「连续 N 轮」（否则第 1 轮和第 10 轮长得一样）",
          any("连续 1 轮" in x["title"] for x in a), f"{[x['title'] for x in a]}")

    # ---- ② 有数据 → 不报，且连续计数归零 ----
    a2, zs2, f2 = healthcheck.evaluate_round(
        {"listings": 42, "sources": [src("jd", "success", 12), src("xianyu", "success", 30)]},
        zero_streak=5, fails={"jd": 9}, rules=rules)
    check("有数据时不报任何警", not a2, f"{[x['key'] for x in a2]}")
    check("有数据时连续 0 条计数归零", zs2 == 0, f"实际 {zs2}")
    check("源真采到数据后失败计数归零", f2.get("jd") == 0, f"实际 {f2.get('jd')}")

    # ---- ③ 熔断跳过：既不 +1 也不归零 ----
    # 不 +1：熔断是设计行为，不是新的失败
    # 不归零：熔断恰恰是「撞到风控」的证据，归零会把问题掩盖掉
    a3, _, f3 = healthcheck.evaluate_round(
        {"listings": 10, "sources": [src("pdd", "skipped"), src("jd", "success", 10)]},
        zero_streak=0, fails={"pdd": 2}, rules=rules)
    check("熔断跳过不给失败计数 +1", f3.get("pdd") == 2, f"实际 {f3.get('pdd')}")
    check("熔断跳过不报警", not any(x["key"].startswith("source-fail") for x in a3),
          f"{[x['key'] for x in a3]}")

    # ---- ④ 单源连续 N 轮失败 → warn（第 N 轮才报，不是第 1 轮）----
    f_state: dict = {}
    fired_at = None
    for i in range(1, 5):
        ai, _, f_state = healthcheck.evaluate_round(
            {"listings": 30, "sources": [src("jd", "failed"), src("xianyu", "success", 30)]},
            zero_streak=0, fails=f_state, rules=rules)
        if any(x["key"] == "source-fail:jd" for x in ai):
            fired_at = i
            break
    check("单源连续失败到第 3 轮才报警（不是第 1 轮就吵）", fired_at == 3, f"实际第 {fired_at} 轮")

    # ---- ⑤ 全部源都 skipped → critical ----
    a5, _, _ = healthcheck.evaluate_round(
        {"listings": 0, "sources": [src("jd", "skipped"), src("pdd", "skipped")]},
        zero_streak=0, fails={}, rules=rules)
    check("全部源都在熔断退避 → 报 critical",
          any(x["key"] == "all-sources-down" for x in a5), f"{[x['key'] for x in a5]}")

    # ---- ⑥ 抑制：同一 key 在窗口内只发一次 ----
    # ⚠️ 用临时文件，别碰真实告警状态（自检不该有副作用）
    import tempfile
    from pathlib import Path as _P
    with tempfile.TemporaryDirectory() as td:
        orig_state, orig_cfg = alerting.STATE_FILE, alerting.CONFIG_FILE
        alerting.STATE_FILE = _P(td) / "alert_state.json"
        alerting.CONFIG_FILE = _P(td) / "alert_config.json"
        alerting.CONFIG_FILE.write_text(json.dumps({
            "enabled": True,
            "macos_notification": False,     # 自检不能真的弹通知
            "webhook": {"enabled": False},
            "cooldown_seconds": 3600,
        }), encoding="utf-8")
        try:
            alerting.clear_suppression()
            check("首个同 key 告警不被抑制", not alerting._suppressed("k", 3600))
            alerting._mark_sent("k")
            check("同 key 在冷却期内被抑制", alerting._suppressed("k", 3600))
            check("冷却期为 0 时不抑制", not alerting._suppressed("k", 0))
            check("不同 key 互不影响", not alerting._suppressed("other", 3600))
            alerting.clear_suppression()
            check("清抑制后立刻可再发", not alerting._suppressed("k", 3600))
        finally:
            alerting.STATE_FILE, alerting.CONFIG_FILE = orig_state, orig_cfg

    # ---- ⑩ 登录态失效 vs 限流（2026-10-01 PDD 卡死的守卫）----
    # 熔断器只看「命中限流特征」，而 `login.html` 既是它的限流特征、
    # 也是登录失效的证据。混为一谈的后果：PDD 跳 login.html → 熔断 1 天 →
    # 到期重试 → 还是 login.html → 熔断更久 → **无限循环**，
    # 而且看起来像在"正常退避"。必须单独识别并告诉用户「去重新扫码」。
    le = healthcheck.evaluate_login_expired

    hit = le({"pdd": {"reason": "login.html", "trips": 5, "remaining_text": "23小时"}})
    check("login.html 被识别为登录态失效",
          any(x["key"] == "login-expired:pdd" for x in hit), f"{[x['key'] for x in hit]}")
    check("登录失效告警是 critical（等待没用，必须人工介入）",
          all(x["level"] == "critical" for x in hit))
    check("登录失效告警里给出**具体命令**（否则用户不知道怎么办）",
          any("app.cli login --site pdd" in x["body"] for x in hit),
          f"{[x['body'][:60] for x in hit]}")
    check("登录失效告警说明「等待没用」（点破退避死循环）",
          any("等待退避没有用" in x["body"] for x in hit))

    for reason in ("访问频繁", "系统繁忙", "Too Many Requests", "429"):
        check(f"「{reason}」不该被误判成登录失效",
              not le({"jd": {"reason": reason, "trips": 2}}), reason)
    check("空熔断状态不崩也不报", not le({}) and not le({"sources": {}}))
    check("嵌套形状 {sources:{...}} 也兼容（文件形状与 snapshot 形状不同）",
          bool(le({"sources": {"pdd": {"reason": "login.html"}}})))

    # ---- ⑪ 默认配置的规则键齐全（缺了会让规则静默失效）----
    for k in ("zero_yield_rounds", "source_fail_rounds", "model_stale_days"):
        check(f"默认配置含规则 {k}", k in alerting.DEFAULT_CONFIG["rules"])
    check("告警默认开启（否则新装机器上等于没做）",
          alerting.DEFAULT_CONFIG["enabled"] is True)
    check("macOS 通知默认开启（零配置通道，装了就能响）",
          alerting.DEFAULT_CONFIG["macos_notification"] is True)

    # ---- ⑧ 系统代理解析（2026-09-29 故障的守卫）----
    # 那天系统代理开着指向 127.0.0.1:7897 但 Clash 没跑，Chromium 继承死代理，
    # 15 轮全 0 条。预检要能**认出「代理开着」这件事**，否则早退逻辑不触发。
    parse = healthcheck.parse_scutil_proxy
    off = parse("""<dictionary> {
  FTPPassive : 1
  HTTPEnable : 0
  HTTPSEnable : 0
  SOCKSEnable : 0
}""")
    check("代理全关时 enabled=False", off["enabled"] is False, f"{off}")

    on = parse("""<dictionary> {
  FTPPassive : 1
  HTTPEnable : 1
  HTTPPort : 7897
  HTTPProxy : 127.0.0.1
  HTTPSEnable : 1
  HTTPSPort : 7897
  HTTPSProxy : 127.0.0.1
  SOCKSEnable : 1
  SOCKSPort : 7897
  SOCKSProxy : 127.0.0.1
}""")
    check("代理开启时 enabled=True", on["enabled"] is True, f"{on}")
    check("代理 host 解析正确", on["host"] == "127.0.0.1", f"{on['host']}")
    check("代理 port 解析正确", on["port"] == 7897, f"{on['port']}")

    only_socks = parse("HTTPEnable : 0\nSOCKSEnable : 1\nSOCKSPort : 1080\nSOCKSProxy : 10.0.0.1\n")
    check("只开 SOCKS 也认（Chromium 同样会继承）",
          only_socks["enabled"] and only_socks["port"] == 1080, f"{only_socks}")
    check("空输入不崩", parse("")["enabled"] is False)

    # ---- ⑨ 陈旧计数保护：断档后不该误报「连续 N 轮失败」----
    # 机器关机一天后恢复，`source_fails` 里还留着几天前的计数。
    # 直接沿用的话，恢复后的第一轮就会误报 —— 那个「连续」横跨了断档，不成立。
    import tempfile as _tf
    from pathlib import Path as _P2
    with _tf.TemporaryDirectory() as td:
        orig = healthcheck.STATE_FILE
        healthcheck.STATE_FILE = _P2(td) / "health_state.json"
        try:
            # 造一份「8 小时前的状态，jd 已连续失败 3 轮」
            stale_at = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 8 * 3600)
            )
            healthcheck.STATE_FILE.write_text(json.dumps({
                "zero_streak": 5,
                "source_fails": {"jd": 3},
                "last_round": {"at": stale_at, "listings": 0},
            }), encoding="utf-8")

            alerts = healthcheck.check_round({
                "listings": 30,
                "sources": [{"source": "jd", "status": "success", "items": 30}],
            })
            check("断档超阈值后不误报「连续 N 轮失败」",
                  not any(x["key"].startswith("source-fail") for x in alerts),
                  f"{[x['key'] for x in alerts]}")
            st = healthcheck.status()
            check("断档后连续计数被清掉", st.get("zero_streak") == 0, f"{st.get('zero_streak')}")

            # 对照：刚刚跑过的状态不该被清
            fresh_at = time.strftime("%Y-%m-%d %H:%M:%S")
            healthcheck.STATE_FILE.write_text(json.dumps({
                "zero_streak": 0,
                "source_fails": {"jd": 2},
                "last_round": {"at": fresh_at, "listings": 10},
            }), encoding="utf-8")
            healthcheck.check_round({
                "listings": 10,
                "sources": [{"source": "jd", "status": "failed", "items": 0}],
            })
            st2 = healthcheck.status()
            check("刚跑过时计数**不**被清（2 失败 → 3）",
                  (st2.get("source_fails") or {}).get("jd") == 3,
                  f"{st2.get('source_fails')}")
        finally:
            healthcheck.STATE_FILE = orig


class _StubPage:
    """只实现 `_search` 在 navigate **之前**会碰到的接口。

    为什么需要它：`_search` 现在会先挂响应监听（`page.on("response")`），
    位置必须在 navigate 之前 —— 否则接不到搜索接口的响应。
    所以测试替身不能再传 `page=None`：该跟上真实接口的是替身，
    而不是让生产代码为测试让路。
    """

    def on(self, *_a, **_k):
        return self

    def remove_listener(self, *_a, **_k):
        return None


def test_page_dead_not_swallowed() -> None:
    """「页面/浏览器已关闭」不能被吞成「搜索失败」。

    为什么必须守
    ------------
    2026-10-01 20:30 实测：闲鱼一轮 15 个型号只采到 1 个就"判定已被限流"提前结束。
    真凶不是限流，是**浏览器实例挂了**：

        RX 6400 4G  : Page.wait_for_timeout: Target page...has been closed
                      → 被识别为页面失效 → mark_dirty + 重建 ✅
        RX 6600 XT  : Page.goto: Target page...has been closed
                      → 被采集器内部的 `except Exception` 吞成"搜索失败"
                      → 上层当成"搜索无结果"累加 empty_streak
                      → 连续 3 次 → **误判为被限流，提前结束本轮** ❌

    三个采集器（jd / xianyu / pdd）都有这个模式。被吞掉的代价是
    整轮剩余型号全部放弃 —— 覆盖率直接掉一截，而日志上看起来像"平台限流"，
    方向完全指反。
    """
    from types import SimpleNamespace

    from app.collectors import get_collectors, policy
    from app.collectors.base import page_dead

    # ---- ① page_dead 的判定范围 ----
    for msg in (
        "Target page, context or browser has been closed",
        "Page.goto: Target page, context or browser has been closed",
        "Page.wait_for_timeout: Target page, context or browser has been closed",
        "page crashed",
        "Target closed",
    ):
        check(f"page_dead 认得：{msg[:42]}", page_dead(Exception(msg)))

    for msg in ("Timeout 30000ms exceeded", "net::ERR_INTERNET_DISCONNECTED", "元素未找到"):
        check(f"page_dead 不误判：{msg[:32]}", not page_dead(Exception(msg)))

        # ---- ② 三个采集器都必须上抛页面死亡 ----
    collectors = {c.code: c for c in get_collectors(None)}
    orig_navigate = policy.navigate
    prod = SimpleNamespace(model="RTX 5070 12G")

    try:
        for code in ("xianyu", "jd", "pdd"):
            c = collectors.get(code)
            if c is None:
                continue

            # 页面死亡 → 必须上抛
            def _dead(*a, **k):
                raise Exception(
                    "Page.goto: Target page, context or browser has been closed"
                )

            policy.navigate = _dead
            raised = False
            try:
                c._search(page=_StubPage(), product=prod)
            except Exception:
                raised = True
            check(f"{code}：页面死亡必须上抛（否则会被误判为限流）", raised,
                  "" if raised else "被吞成了空列表")

            # 普通失败 → 仍应吞成空列表（别改过头，否则单个型号超时就中断整轮）
            def _timeout(*a, **k):
                raise Exception("Timeout 30000ms exceeded")

            policy.navigate = _timeout
            try:
                r = c._search(page=_StubPage(), product=prod)
                check(f"{code}：普通超时仍吞成空列表（不上抛）", r == [],
                      "" if r == [] else f"返回 {r!r}")
            except Exception as e:
                check(f"{code}：普通超时仍吞成空列表（不上抛）", False, f"上抛了 {e}")

        # ---- ③ 解析层同样不能吞掉页面死亡 ----
        # 取数阶段（page.evaluate / page.locator）也有 `except Exception → return []`。
        # 同样的道理：页面在这时挂了，吞掉就会累加 empty_streak → 误判限流。
        class _DeadPage:
            def evaluate(self, *a, **k):
                raise Exception(
                    "Page.evaluate: Target page, context or browser has been closed"
                )

            def on(self, *_a, **_k):
                """响应监听 —— 真实 Playwright 页面必有；假页面空实现即可。"""
                return self

            def remove_listener(self, *_a, **_k):
                return None

            def locator(self, *a, **k):
                raise Exception(
                    "Page.locator: Target page, context or browser has been closed"
                )

        # 让导航与行为模拟变成空操作，把流程推到取数阶段。
        # ⚠️ 必须把**改过的每一个**都存下来恢复 —— 漏一个就会污染后续测试
        #    （踩过：漏了 assert_not_rate_limited，把限流检测那条测试搞挂了）。
        _patched = ("navigate", "behave", "settle", "assert_not_rate_limited")
        _saved = {n: getattr(policy, n) for n in _patched}
        policy.navigate = lambda *a, **k: None
        policy.behave = lambda *a, **k: None
        policy.settle = lambda *a, **k: None
        policy.assert_not_rate_limited = lambda *a, **k: None
        try:
            for code in ("xianyu", "jd", "pdd"):
                c = collectors.get(code)
                if c is None:
                    continue
                raised = False
                try:
                    c._search(page=_DeadPage(), product=prod)
                except Exception:
                    raised = True
                check(f"{code}：取数阶段页面死亡也要上抛", raised,
                      "" if raised else "被吞成了空列表")
        finally:
            for n, fn in _saved.items():
                setattr(policy, n, fn)
    finally:
        policy.navigate = orig_navigate


def test_network_error_not_counted_as_empty() -> None:
    """网络故障不能被算成「搜索无结果」。

    为什么必须守（归因正确性）
    ------------------------
    2026-10-01 实测：`ERR_INTERNET_DISCONNECTED` 出现 **131 次**，是所有浏览器
    错误里最大的一头（远超 `ERR_PROXY_CONNECTION_FAILED` 的 51 次，那全在 09-29）。

    但 `run_browser_batch` 原本把**所有**非限流异常都算进 `empty_streak`，
    于是连续 3 次网络错误就打出「判定已被限流，提前结束本轮」——
    日志里明明写着 `ERR_INTERNET_DISCONNECTED`。

    方向指反的代价：去查平台风控、调退避阶梯，全是在治错的病。
    所以网络错误必须走**独立计数** `net_streak`，不碰 `empty_streak`。
    """
    from app.collectors.base import NET_STREAK_LIMIT, network_dead

    # ---- ① 认得「网络不可用」类错误 ----
    for msg in (
        "Page.goto: net::ERR_INTERNET_DISCONNECTED at https://www.goofish.com/search",
        "net::ERR_PROXY_CONNECTION_FAILED at https://search.jd.com/Search",
        "net::ERR_NAME_NOT_RESOLVED at https://mobile.yangkeduo.com/",
        "net::ERR_NETWORK_CHANGED",
        "net::ERR_ADDRESS_INVALID",
        "net::ERR_SOCKET_NOT_CONNECTED",
    ):
        check(f"network_dead 认得：{msg[:44]}", network_dead(Exception(msg)))

    # ---- ② 平台侧错误不能被误判成网络故障 ----
    # 这些是平台在拦你 / 页面问题，归到网络会让人去查 Wi-Fi 而错过真因。
    for msg in (
        "Timeout 30000ms exceeded",
        "Page.goto: Timeout 40000ms exceeded",
        "Target page, context or browser has been closed",
        "元素未找到",
    ):
        check(f"network_dead 不误判：{msg[:36]}", not network_dead(Exception(msg)))

    # ⚠️ ERR_CONNECTION_CLOSED 故意**不**算网络故障：
    #    既可能是本地网络，也可能是平台主动掐断（风控），归哪边都会误导。
    check("ERR_CONNECTION_CLOSED 故意不归类（归哪边都会误导）",
          not network_dead(Exception("net::ERR_CONNECTION_CLOSED")))

    # ---- ③ 两套阈值必须是独立的常量 ----
    check("NET_STREAK_LIMIT 存在且为正", isinstance(NET_STREAK_LIMIT, int) and NET_STREAK_LIMIT > 0,
          f"{NET_STREAK_LIMIT}")

    # ---- ④ 源码层守：网络分支不能去动 empty_streak ----
    # 这是结构性约束，只能查源码 —— 但查的是**正向**（网络分支里不出现
    # empty_streak），不是"某字符串不存在"那种脆弱的负向检查。
    import inspect

    from app.collectors import base as base_mod

    src = inspect.getsource(base_mod.run_browser_batch)
    net_branch_start = src.find("if network_dead(exc):")
    check("run_browser_batch 里有网络分支", net_branch_start > 0)
    if net_branch_start > 0:
        # 取网络分支到下一个 `continue` 之间
        seg = src[net_branch_start:net_branch_start + 900]
        dirty = "empty_streak" in seg.split("continue")[0]
        check("网络分支不碰 empty_streak（否则会误报限流）", not dirty,
              "网络分支里出现了 empty_streak" if dirty else "")
        check("网络分支用的是独立计数 net_streak", "net_streak" in seg)


def test_coverage_scope_matches_collection() -> None:
    """覆盖检查的「型号范围」必须和采集范围一致。

    为什么必须守
    ------------
    项目里有**两套覆盖统计**，口径不同：

      · `app/services/healthcheck.check_coverage()` —— 只算 gpu+cpu，用于自动告警
      · `scripts/coverage.py`                       —— 全品类，用于人工查看

    这两套数字**本来就不该相等**。危险在于：哪天有人把采集范围从
    gpu,cpu 扩到别的品类（`collect_scheduled.sh` 里的
    `DIYPRICE_FOCUS_CATEGORY`），却忘了改 healthcheck 里的
    `category in ('gpu','cpu')` —— 自动告警就会**静默漏报新品类**，
    而人工查看那边是正常的，于是没人会发现。
    """
    from sqlalchemy import text

    from app.db import session_scope
    from app.services import healthcheck

    r = healthcheck.check_coverage(days=7)
    check("check_coverage 返回在追型号总数", isinstance(r.get("total_models"), int),
          "" if isinstance(r.get("total_models"), int) else f"{r.get('total_models')}")
    too_wide = r["total_models"] >= 300
    check("check_coverage 只算 gpu+cpu（采集范围），不是全品类", not too_wide,
          f"返回 {r['total_models']} —— 若已接近全品类数（351），"
          f"说明范围写错了或采集范围扩了但这里没跟着改" if too_wide else "")

    # 与 DB 直查对照：确认就是 gpu+cpu 的活跃型号数
    with session_scope() as s:
        expect = s.execute(text(
            "select count(*) from products "
            "where is_active=1 and category in ('gpu','cpu')"
        )).scalar()
    check("与 DB 直查的 gpu+cpu 活跃型号数一致",
          r["total_models"] == int(expect or 0),
          f"检查={r['total_models']} DB={expect}")

    check("返回结构含 never_collected / stale_models",
          "never_collected" in r and "stale_models" in r)


def test_alert_config_update() -> None:
    """告警配置的局部更新（`alert --set-webhook` / `--off` / `--set-cooldown`）。

    为什么要守
    ----------
    这些命令是**写配置文件**的。写坏的后果特别隐蔽：`load_config()` 遇到
    损坏的 JSON 会**回退默认值**，于是告警静默失效 —— 用户以为配好了，
    实际一条都收不到，直到出事才发现。

    所以两个方向都要守：
      1. 局部更新**不能把没改的键抹掉**（改 webhook 不能顺手关了 macOS 通知）
      2. 写出来的必须是**合法 JSON**，且能被 load_config 读回来
    """
    import json
    import tempfile
    from pathlib import Path as _P

    from app.services import alerting

    with tempfile.TemporaryDirectory() as td:
        orig_cfg, orig_state = alerting.CONFIG_FILE, alerting.STATE_FILE
        alerting.CONFIG_FILE = _P(td) / "alert_config.json"
        alerting.STATE_FILE = _P(td) / "alert_state.json"
        try:
            # ---- ① 从零配置开始：局部更新不能抹掉其他默认键 ----
            cfg = alerting.update_config(webhook={
                "enabled": True, "url": "https://example.com/hook", "type": "dingtalk",
            })
            check("update_config 返回合并后的配置",
                  cfg["webhook"]["enabled"] is True and cfg["webhook"]["type"] == "dingtalk")
            check("改 webhook 不会关掉 macOS 通知（默认值仍在）",
                  cfg.get("macos_notification") is True, f"{cfg.get('macos_notification')}")
            check("改 webhook 不会丢掉 rules",
                  set(cfg.get("rules", {})) >= {"zero_yield_rounds", "source_fail_rounds"})

            # ---- ② 写出来的必须能读回来（合法 JSON）----
            on_disk = json.loads(alerting.CONFIG_FILE.read_text(encoding="utf-8"))
            check("配置文件是合法 JSON 且落盘了", on_disk["webhook"]["enabled"] is True)
            reread = alerting.load_config()
            check("load_config 能读回刚写的值",
                  reread["webhook"]["url"] == "https://example.com/hook",
                  f"{reread['webhook'].get('url')}")

            # ---- ③ 只改一个键，不能影响别的 ----
            alerting.update_config(cooldown_seconds=60)
            after = alerting.load_config()
            check("改 cooldown 不影响 webhook 配置",
                  after["webhook"]["enabled"] is True
                  and after["webhook"]["url"] == "https://example.com/hook")
            check("cooldown 已生效", after["cooldown_seconds"] == 60,
                  f"{after['cooldown_seconds']}")

            # ---- ④ 关 webhook 只关它自己 ----
            alerting.update_config(webhook={"enabled": False})
            off = alerting.load_config()
            check("--off 只关 webhook，不动 macOS 通知",
                  off["webhook"]["enabled"] is False
                  and off["macos_notification"] is True)
            check("--off 保留 url（下次开不用重填）",
                  off["webhook"]["url"] == "https://example.com/hook",
                  f"{off['webhook'].get('url')}")
        finally:
            alerting.CONFIG_FILE, alerting.STATE_FILE = orig_cfg, orig_state


def test_stall_guard_config() -> None:
    """停滞看门狗：阈值关系与心跳语义。

    为什么必须守
    ------------
    2026-10-01 实测：断网时 `page.goto(timeout=40000)` **不返回**
    （Playwright 的 timeout 在网络栈卡住时不生效），一轮卡到 35 分钟的
    墙钟兜底才被杀。192 轮里 **77 轮（40%）** 是这么死的，
    被杀的轮次中位数 44.6 分钟，而正常轮次 P50 只有 11.4 分钟。

    停滞看门狗是**更早**的那道防线。它要生效，必须满足：

      1. 阈值**远小于**墙钟上限 —— 否则永远轮不到它，等于没装
      2. 阈值**大于**单个型号的正常耗时 —— 否则会把慢型号误判成卡死
      3. 心跳语义正确 —— 它是「慢」和「卡死」的唯一区分依据

    ⚠️ 这条断言守的是**配置关系**，不是实现细节 —— 哪天有人把阈值调到
       比墙钟还大（或改小到低于单个型号耗时），自检必须立刻发现。
    """
    import time as _t

    from app.services import heartbeat, pipeline

    # ---- ① 阈值关系 ----
    stall = pipeline._STALL_LIMIT_SECONDS
    wall = pipeline.wall_clock_limit()
    check("停滞阈值 > 0（0 等于关掉）", stall > 0, f"{stall}")
    check("停滞阈值必须**小于**墙钟上限（否则永远轮不到它）",
          stall < wall, f"停滞 {stall:.0f}s vs 墙钟 {wall:.0f}s")

    # 单个型号正常 30~40 秒；阈值要留足余量，别把慢型号当卡死
    check("停滞阈值 ≥ 120s（给单个型号留足余量，别误杀慢型号）",
          stall >= 120, f"{stall:.0f}s")
    # 但也不能太宽松，否则又回到"卡 35 分钟"
    check("停滞阈值 ≤ 600s（太大就失去意义，退化成墙钟兜底）",
          stall <= 600, f"{stall:.0f}s")

    # ---- ② 心跳语义 ----
    heartbeat.note()
    check("note() 后停滞时间归零", heartbeat.stalled_seconds() < 0.5,
          f"{heartbeat.stalled_seconds():.2f}s")
    _t.sleep(0.25)
    check("停滞时间随时间增长", heartbeat.stalled_seconds() >= 0.2,
          f"{heartbeat.stalled_seconds():.2f}s")
    heartbeat.note()
    check("再次 note() 又归零", heartbeat.stalled_seconds() < 0.5,
          f"{heartbeat.stalled_seconds():.2f}s")
    heartbeat.reset()
    check("reset() 等价于 note()", heartbeat.stalled_seconds() < 0.5)

    # ---- ③ 采集循环里必须真的调心跳 ----
    # 不调的话看门狗永远看到「刚有进展」，卡死时也不会触发 —— 装了等于没装。
    import inspect

    from app.collectors import base as base_mod

    src = inspect.getsource(base_mod.run_browser_batch)
    check("run_browser_batch 里调了心跳（否则看门狗形同虚设）",
          "note()" in src and "_heartbeat" in src,
          "没找到心跳调用")


def test_profile_crash_flag_reset() -> None:
    """强杀后要复位 profile 的「异常退出」标记。

    为什么必须守（用户反馈："关掉谷歌浏览器总是弹未正确关闭"）
    ----------------------------------------------------------
    本项目的浏览器**经常被强杀**：

      · 墙钟兜底 `os._exit(3)`（pipeline）
      · 停滞看门狗 `os._exit(4)`（2026-10-01 加）
      · shell 看门狗 `kill -KILL`（collect_scheduled.sh）

    强杀后 Chrome 在 `Preferences` 里写下 `profile.exit_type = "Crashed"`，
    下次启动就弹「Chrome 未正确关闭 / 是否恢复标签页」——
    采集在后台跑，没人去点那个气泡，它就一直挂着。

    两道保险都要守：
      1. 启动参数里的 `--hide-crash-restore-bubble`（挡气泡，但不保证所有分支都认）
      2. 启动前复位 `exit_type`（直接改文件，必定生效）
    """
    import json
    import tempfile
    from pathlib import Path as _P

    from app.services.session import _normalize_profile_exit_type as norm

    with tempfile.TemporaryDirectory() as td:
        d = _P(td) / "Default"
        d.mkdir(parents=True)
        pref = d / "Preferences"

        # ---- ① Crashed 必须被复位，且不能丢掉别的键 ----
        pref.write_text(json.dumps({
            "profile": {"exit_type": "Crashed", "other": 1},
            "keep": "x",
        }), encoding="utf-8")
        before = norm(td)
        after = json.loads(pref.read_text(encoding="utf-8"))
        check("Crashed 被复位", after["profile"]["exit_type"] == "Normal",
              f"{after['profile']['exit_type']}")
        check("返回复位前的值（便于日志）", before == "Crashed", f"{before!r}")
        check("**不丢其他键**（Preferences 里还有登录态相关字段）",
              after["profile"].get("other") == 1 and after.get("keep") == "x")
        check("同时置 exited_cleanly", after["profile"].get("exited_cleanly") is True)

        # ---- ② 已经是 Normal 时不该写盘（省 I/O，也别无谓改动文件）----
        pref.write_text(json.dumps({"profile": {"exit_type": "Normal"}}), encoding="utf-8")
        m1 = pref.stat().st_mtime_ns
        norm(td)
        check("已是 Normal 时不写盘", pref.stat().st_mtime_ns == m1)

        # ---- ③ 容错：文件不存在 / JSON 损坏都不能崩 ----
        check("profile 不存在时返回空串不崩", norm(_P(td) / "nope") == "")
        pref.write_text("{ 坏 json", encoding="utf-8")
        check("Preferences 损坏时不崩", norm(td) == "")

    # ---- ④ 启动参数里必须有防气泡开关 ----
    from app.services import session as sess

    args = sess.build_launch_args("/usr/bin/true", "/tmp/p", 9222, park_window=False)
    joined = " ".join(args)
    check("启动参数含 --hide-crash-restore-bubble（挡「未正确关闭」气泡）",
          "--hide-crash-restore-bubble" in joined,
          "缺这个开关，强杀后下次启动会弹气泡")
    # 防气泡开关不能顺手把抗指纹的底线弄丢 —— 这是加参数时最容易踩的坑
    check("加防气泡开关后仍保留 --user-data-dir（物理隔离底线）",
          any(a.startswith("--user-data-dir=") for a in args))
    check("加防气泡开关后仍禁用同步（不关联用户账号）",
          "--disable-sync" in joined)


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

def test_browser_proxy_policy() -> None:
    """采集浏览器必须**不继承系统代理**。

    守住的是 2026-09-29（开代理但 Clash 没运行 → 15 轮全 0 条，跑了一整天没人知道）
    与 2026-10-01 22:30（代理节点死 → 前置检查拦下整轮）这两次真实损失。
    """
    from app.services import healthcheck
    from app.services import session as sess

    # ---- 启动参数 ----
    with mock.patch.dict(os.environ, {"DIYPRICE_BROWSER_PROXY": ""}, clear=False):
        os.environ.pop("DIYPRICE_BROWSER_PROXY", None)
        args = sess.build_launch_args("/x/Chrome", "/tmp/p", 9222)
        check("默认给采集浏览器加 --no-proxy-server（不继承系统代理）",
              "--no-proxy-server" in args,
              f"实际 {[a for a in args if 'proxy' in a]}")
        check("默认不加 --proxy-server",
              not any(a.startswith("--proxy-server=") for a in args))
        check("默认代理描述为直连", "直连" in sess.browser_proxy_label())

    # ---- 显式指定代理时（可覆盖）----
    with mock.patch.dict(os.environ, {"DIYPRICE_BROWSER_PROXY": "http://127.0.0.1:7897"}):
        args2 = sess.build_launch_args("/x/Chrome", "/tmp/p", 9222)
        check("设了 DIYPRICE_BROWSER_PROXY 时用 --proxy-server",
              "--proxy-server=http://127.0.0.1:7897" in args2)
        check("设了代理时不再加 --no-proxy-server",
              "--no-proxy-server" not in args2)

    # ---- 前置检查：系统代理开着但已死时，**不能**再拦轮次（22:30 的真实场景）----
    dead_proxy = {"enabled": True, "host": "127.0.0.1", "port": 7897}
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("DIYPRICE_BROWSER_PROXY", None)
        with mock.patch.object(healthcheck, "_system_proxy", return_value=dead_proxy), \
             mock.patch.object(healthcheck, "_tcp_ok", return_value=True), \
             mock.patch.object(healthcheck, "_http_ok", return_value=True):
            r = healthcheck.preflight()
        check("系统代理开着但已死 + 浏览器直连 → 前置检查**放行**（不再白丢整轮）",
              r["ok"] is True and not r["problems"],
              f"ok={r['ok']} problems={r['problems']}")
        check("系统代理状态仍被记录（供排查）",
              r.get("system_proxy") == "127.0.0.1:7897"
              and r.get("browser_path") == "direct")
        check("标记了「系统代理已被忽略」",
              r.get("system_proxy_ignored") is True)

        # ---- 直连真的不通时，仍然要拦 ----
        with mock.patch.object(healthcheck, "_system_proxy", return_value=dead_proxy), \
             mock.patch.object(healthcheck, "_tcp_ok", return_value=True), \
             mock.patch.object(healthcheck, "_http_ok", return_value=False):
            r2 = healthcheck.preflight()
        check("直连真的上不了网 → 仍然拦下（误放行也不行）",
              r2["ok"] is False
              and any("直连上不了网" in p for p in r2["problems"]),
              f"ok={r2['ok']} problems={r2['problems']}")

    # ---- 显式配了代理 → 探的就是那个代理 ----
    with mock.patch.dict(os.environ, {"DIYPRICE_BROWSER_PROXY": "http://127.0.0.1:7897"}):
        with mock.patch.object(healthcheck, "_system_proxy",
                               return_value={"enabled": False, "host": "", "port": 0}), \
             mock.patch.object(healthcheck, "_tcp_ok", return_value=False), \
             mock.patch.object(healthcheck, "_http_ok", return_value=True):
            r3 = healthcheck.preflight()
        check("显式配的代理连不上 → 拦下并指明是 DIYPRICE_BROWSER_PROXY",
              r3["ok"] is False
              and any("DIYPRICE_BROWSER_PROXY" in p for p in r3["problems"]),
              f"ok={r3['ok']} problems={r3['problems']}")


def test_price_normalization() -> None:
    """价格与链接归一化 —— 样本全部来自**线上探针实测的卡片原文**。

    守住的 bug（2026-10-01）：闲鱼把 `2.30万` 拆成三行渲染
    （`¥` ⏎ `2` ⏎ `.30` ⏎ `万`），旧提取器只取「¥ 的下一行」→ 记成 ¥2.00，
    导致**所有万元级二手报价被数据质量闸门当垃圾丢掉**
    （症状：闲鱼库里价格上限恰好卡在 10000，RTX 4090 的 max 只有 9998）。
    """
    from app.collectors import normalize as N

    # (片段, 期望值, 说明) —— 前 4 条是探针抓到的**真实卡片形态**
    split_cases = [
        (["2", ".30", "万"], 23000.0, "RTX4090 猛禽 2.30万（探针原文）"),
        (["2", ".39", "万"], 23900.0, "ROG 4090 白色 2.39万（探针原文）"),
        (["2", ".29", "万"], 22900.0, "ROG 4090 猛禽oc 2.29万（探针原文）"),
        (["18", ".88"], 18.88, "5090 空盒子 18.88（探针原文）"),
        (["4030"], 4030.0, "技嘉魔鹰坏卡"),
        (["6299"], 6299.0, "微星超龙"),
        (["1", "万"], 10000.0, "整万"),
        (["168"], 168.0, "单片段"),
        # ⚠️ 过度拼接的防守：后续段若不是「小数部分」或「万」就必须停。
        #    否则「¥ 2688」后面跟一行「56人想拼」会被拼成 268856。
        (["2688", "56"], 2688.0, "后续段不是小数/万 → 停在第 1 段"),
        (["4030", "14"], 4030.0, "降价百分比行不能被拼进来"),
        (["2", ".30", "万", "1", "人想要"], 23000.0, "万之后的内容一律忽略"),
        (["人想要"], None, "首段不是数字 → 整条丢弃"),
        # ⚠️ 上面那条**区分不出**「首段校验」是否存在：即使放开校验，
        #    parse_price("人想要") 也找不到数字、照样返回 None。
        #    这一条能区分 —— 去掉校验会得到 2.3 而不是 None。
        (["¥2", ".30"], None, "首段带货币符号 → 必须整条拒绝（区分度用例）"),
        (["2万"], None, "万跟着数字挤在一段里 → 同样拒绝"),
        # ⚠️ 上面那条**区分不出**「首段校验」是否存在：即使放开校验，
        #    parse_price("人想要") 也找不到数字、照样返回 None。
        #    这一条能区分 —— 去掉校验会得到 2.3 而不是 None。
        (["¥2", ".30"], None, "首段带货币符号 → 必须整条拒绝（区分度用例）"),
        (["2万"], None, "万跟着数字挤在一段里 → 同样拒绝"),
        ("4599.00", 4599.0, "传入字符串也能解析"),
        # 单位倍率：删掉 _UNIT_MULTIPLIERS 里任一项都会被这几条抓住
        ("1.2w", 12000.0, "w 单位（小写）"),
        ("1.2W", 12000.0, "W 单位（大写）"),
        ("3.5k", 3500.0, "k 单位"),
        ("2.5万", 25000.0, "万单位（字符串形态）"),
        # 单位倍率：删掉 _UNIT_MULTIPLIERS 里任一项都会被这几条抓住
        ("1.2w", 12000.0, "w 单位（小写）"),
        ("1.2W", 12000.0, "W 单位（大写）"),
        ("3.5k", 3500.0, "k 单位"),
        ("2.5万", 25000.0, "万单位（字符串形态）"),
        ([], None, "空片段"),
        (None, None, "缺失"),
    ]
    bad = []
    for seg, expect, label in split_cases:
        got = N.parse_split_price(seg)
        ok = (got == expect) if expect is not None else (got is None)
        if not ok:
            bad.append(f"{label}: 期望 {expect} 实际 {got}")
    check(f"拆分价格拼装（{len(split_cases)} 例，含探针原文）", not bad, "; ".join(bad))

    # 京东形态（价格在一个节点里）+ 必须拒绝的噪声
    text_cases = [
        ("¥1,234.56", 1234.56, "千分位 + 小数"),
        ("4599.00", 4599.0, "纯数字"),
        ("¥ 18999", 18999.0, "带货币符号与空格"),
        ("万图师", None, "品牌名里的「万」不能当单位"),
        ("面议", None, "没有数字"),
        ("¥", None, "只有符号"),
        ("", None, "空串"),
    ]
    bad = []
    for text, expect, label in text_cases:
        got = N.parse_price(text)
        ok = (got == expect) if expect is not None else (got is None)
        if not ok:
            bad.append(f"{label}: 期望 {expect} 实际 {got}")
    check(f"价格文本解析（{len(text_cases)} 例）", not bad, "; ".join(bad))

    # 链接归一化
    check("闲鱼私有协议换成 https（否则库里存的链接点不开）",
          N.normalize_url("fleamarket://item?id=123") == "https://www.goofish.com/item?id=123",
          N.normalize_url("fleamarket://item?id=123"))
    check("协议相对链接补 https",
          N.normalize_url("//www.goofish.com/item?id=1") == "https://www.goofish.com/item?id=1")
    check("去掉跟踪参数但保留商品 id",
          N.normalize_url("https://www.goofish.com/item?id=9&spm=a1z&utm_source=x") ==
          "https://www.goofish.com/item?id=9",
          N.normalize_url("https://www.goofish.com/item?id=9&spm=a1z&utm_source=x"))
    check("搜索页不能当商品身份（链接键必须为空）",
          N.link_key("https://www.goofish.com/search?q=RTX+4090") == "")
    check("商品链接能给出稳定键",
          N.link_key("https://www.goofish.com/item?id=9&spm=a1z") ==
          N.link_key("https://www.goofish.com/item?id=9&from=share"))

    # ---- 采集器接线：**结构化**断言，不用子串 ----
    #
    # ⚠️ 这里踩过坑：第一版写成 `"priceSeg" in src` —— 把 JS 改成
    #    `priceSeg: seg.slice(0, 1)`（退回只取第一段）它照样通过，
    #    因为 Python 侧那句 `row.get("priceSeg")` 也含这个词。
    #    源码断言用子串匹配一定不可靠（项目教训 #1），必须锚定到**具体行**。
    import pathlib as _pl

    srcs = {n: _pl.Path(f"app/collectors/{n}_source.py").read_text(encoding="utf-8")
            for n in ("jd", "pdd", "xianyu")}

    # (a) 三个采集器都必须调用 normalize 的解析器（锚定到调用形式）
    CALL_JD = "normalize.parse_price(text)"
    CALL_SPLIT = 'parse_split_price(row.get("priceSeg"))'
    CALL_DOM = 'normalize.parse_split_price(r.get("priceSeg"))'   # DOM 兜底路径
    bad = [n for n in ("jd", "pdd", "xianyu")
           if (CALL_JD if n == "jd" else
               (CALL_SPLIT if n == "pdd" else CALL_DOM)) not in srcs[n]]
    check("三个采集器的价格解析都调用 normalize（不允许各写一套）",
          not bad, f"未接入：{bad}")

    # (b) 拼多多/闲鱼的 JS 必须**原样**产出完整 priceSeg
    PUSH = "priceSeg: seg });"          # 注意：slice 版本不含这个串
    GUARD = "test(t) || t === '万'"       # 「小数部分或万」的延续判据
    for name in ("pdd", "xianyu"):
        src = srcs[name]
        check(f"{name}：JS 原样产出完整 priceSeg（不是只取第一段）",
              PUSH in src and ".slice(0, 1)" not in src,
              "出现了 seg.slice 或未找到完整 push —— 会把 2.30万 记成 2")
        check(f"{name}：JS 保留「小数/万」延续判据",
              GUARD in src,
              "延续判据缺失 —— 可能把「56人想拼」拼进价格")

def test_xianyu_api_parse() -> None:
    """闲鱼搜索接口解析 —— 样本结构取自 2026-10-02 线上实捕的 300 KB 响应。

    这条路的价值：不再依赖渲染结果（类名改版不影响），并拿到 DOM 上看不到的
    字段（发布时间 / 地区 / 商品链接）。第三条断言是**回落保障** ——
    接口一旦失效必须能自动退回 DOM，不能整源归零。
    """
    from app.collectors import normalize as N
    from app.collectors import normalize as N
    from app.collectors import xianyu_source as X

    def node(title, price, url="fleamarket://item?id=1", publish="1790871637000",
             area="广东"):
        return {
            "type": "item",
            "data": {
                "item": {
                    "main": {
                        "exContent": {"title": title, "area": area, "itemId": "1",
                                      "picUrl": "//img/a.jpg"},
                        "clickParam": {"args": {"price": price, "displayPrice": price,
                                                "publishTime": publish, "id": "1"}},
                        "targetUrl": url,
                    }
                }
            },
        }

    payload = {"ret": ["SUCCESS::调用成功"], "data": {"resultList": [
        node("华硕TUF RTX4090 24G OC Gaming显卡 成色99新", "20499"),
        node("微星4090超龙 原盒原码", "23000", area="山西",
             url="fleamarket://item?id=999&referPageArgs=RTX+4090"),
    ]}}

    rows = X.parse_search_payload(payload)
    check("闲鱼接口：解析出 2 条", len(rows) == 2, f"实际 {len(rows)}")
    if rows:
        r = rows[0]
        check("闲鱼接口：价格取**接口定价**（不是渲染文本，无需拆行拼装）",
              r["price"] == 20499.0, f"实际 {r['price']}")
        check("闲鱼接口：标题正确", r["title"].startswith("华硕TUF RTX4090"), r["title"][:30])
        check("闲鱼接口：fleamarket:// 归一到 https",
              r["item_url"].startswith("https://www.goofish.com/"), r["item_url"][:60])
        check("闲鱼接口：拿到发布时间（DOM 上取不到）",
              r["publish_time"] == "1790871637000", r["publish_time"])
        check("闲鱼接口：拿到地区（DOM 上取不到）", r["area"] == "广东", r["area"])
        check("链接归一：商品页只保留 id（非跟踪参数也要去掉）",
          N.normalize_url("https://www.goofish.com/item?id=999&foo=bar") ==
          "https://www.goofish.com/item?id=999",
          N.normalize_url("https://www.goofish.com/item?id=999&foo=bar"))

    check("闲鱼接口：跟踪参数被去掉但保留 id",
              "id=999" in rows[1]["item_url"] and "referPageArgs" not in rows[1]["item_url"],
              rows[1]["item_url"])

    # ---- 脏数据 / 边界：单条坏数据不能毁掉整页 ----
    dirty = {"data": {"resultList": [
        None,
        "not a dict",
        {"data": {"item": {"main": {"exContent": {}, "clickParam": {"args": {}}}}}},
        node("正常的一条", "1200"),
        {"data": {"item": {"main": {"exContent": {"title": "没有价格"},
                                    "clickParam": {"args": {}}}}}},
        # ⚠️ 下面这条是**区分度用例**：没有标题但有合法价格。
        #    没有它，标题守卫被去掉也测不出来（上面那条既没标题也没价格，
        #    会被价格守卫顺手挡掉）。
        {"data": {"item": {"main": {
            "exContent": {"title": "  ", "area": "北京"},
            "clickParam": {"args": {"price": "1234", "id": "7"}},
            "targetUrl": "fleamarket://item?id=7",
        }}}},
    ]}}
    rows2 = X.parse_search_payload(dirty)
    check("闲鱼接口：脏数据被跳过、好数据保留", len(rows2) == 1,
          f"实际 {len(rows2)} 条")

    # ⚠️ 实测：同一份响应里既有 fleamarket:// 也有**加密的不透明串**。
    #    拿不到合法 URL 时必须用 item_id 自己拼，否则链接不可用、去重键被污染。
    opaque = {"data": {"resultList": [
        node("加密链接的商品", "8888", url="z9xpXnwzz6eu3JHQFPK2nb4PAGLvMlrcMmtt="),
        node("链接为空但有 id 的商品", "6666", url=""),
    ]}}
    rows3 = X.parse_search_payload(opaque)
    check("闲鱼接口：加密 targetUrl 不原样当链接用",
          all("z9xpXnwzz" not in r["item_url"] for r in rows3),
          str([r["item_url"] for r in rows3]))
    check("闲鱼接口：拿不到 URL 时用 item_id 拼出商品页链接",
          len(rows3) == 2 and all(
              r["item_url"] == "https://www.goofish.com/item?id=1" for r in rows3),
          str([r["item_url"] for r in rows3]))

    check("链接归一：非 URL 一律返回空串（不透明串不能当链接）",
          N.normalize_url("z9xpXnwzz6eu3JHQFPK2nb4PAGLvMlrcMmtt=") == ""
          and N.normalize_url("") == "" and N.normalize_url(None) == "")

    # ⚠️ 实测：同一份响应里既有 fleamarket:// 也有**加密的不透明串**。
    #    拿不到合法 URL 时必须用 item_id 自己拼，否则链接不可用、去重键被污染。
    opaque = {"data": {"resultList": [
        node("加密链接的商品", "8888", url="z9xpXnwzz6eu3JHQFPK2nb4PAGLvMlrcMmtt="),
        node("链接为空但有 id 的商品", "6666", url=""),
    ]}}
    rows3 = X.parse_search_payload(opaque)
    check("闲鱼接口：加密 targetUrl 不原样当链接用",
          all("z9xpXnwzz" not in r["item_url"] for r in rows3),
          str([r["item_url"] for r in rows3]))
    check("闲鱼接口：拿不到 URL 时用 item_id 拼出商品页链接",
          len(rows3) == 2 and all(
              r["item_url"] == "https://www.goofish.com/item?id=1" for r in rows3),
          str([r["item_url"] for r in rows3]))

    check("链接归一：非 URL 一律返回空串（不透明串不能当链接）",
          N.normalize_url("z9xpXnwzz6eu3JHQFPK2nb4PAGLvMlrcMmtt=") == ""
          and N.normalize_url("") == "" and N.normalize_url(None) == "")

    check("闲鱼接口：空/残缺载荷返回空表而不是抛异常",
          X.parse_search_payload(None) == []
          and X.parse_search_payload({}) == []
          and X.parse_search_payload({"data": {}}) == []
          and X.parse_search_payload({"data": {"resultList": None}}) == [])

    # ---- 回落保障：接口路径与 DOM 路径必须**同时存在** ----
    import pathlib as _pl
    src = _pl.Path("app/collectors/xianyu_source.py").read_text(encoding="utf-8")
    check("闲鱼：调用接口解析（主路径）", "parse_search_payload(captured" in src)
    check("闲鱼：保留 DOM 兜底（接口失效时不至于整源归零）",
          "def _rows_from_dom" in src and "page.evaluate(_EXTRACT_JS)" in src)
    check("闲鱼：确实挂了响应监听（否则截获形同虚设）",
          'page.on("response", _on_response)' in src)
    check("闲鱼：监听回调按接口名过滤（不能什么响应都当搜索结果）",
          "SEARCH_API_MARK not in resp.url" in src)
    check("闲鱼：排除 `.shade` 装饰接口（它没有 resultList，会覆盖真结果）",
          "SEARCH_API_MARK + \".shade\"" in src)
    check("闲鱼：响应监听器用完即摘（否则多型号会重复解析 300KB）",
          "remove_listener" in src)
    check("闲鱼：渲染价格拼装仍在兜底路径里（2.30万 不会被记成 2）",
          "normalize.parse_split_price" in src)


def test_dedupe_identity() -> None:
    """去重身份键：优先商品链接，拿不到链接必须**回落**。

    守住的坑：
      · 卖家改标题 → 同一件商品被当新条目（链接能解决）
      · 反过来更危险：京东/拼多多的 url 是**搜索页 URL**，若不给回落，
        整个型号的 30 条报价会被合并成 1 条 —— 灾难级错误。
    """
    from app.collectors.base import Quote, dedupe_quotes, quote_identity

    ITEM_A = "https://www.goofish.com/item?id=111"
    ITEM_B = "https://www.goofish.com/item?id=222"
    SEARCH = "https://www.goofish.com/search?q=RTX+4090"

    def q(title, price, url, plat="xianyu"):
        return Quote(platform_code=plat, title_raw=title, price=price, url=url)

    # ---- ① 链接优先：同链接但标题/价格不同 → 仍视为同一件商品 ----
    same_item = [
        q("华硕4090 猛禽", 23000, ITEM_A),
        q("华硕4090 猛禽 包邮 可小刀", 23000, ITEM_A),   # 卖家改了标题
        q("华硕RTX4090", 22900, ITEM_A),                  # 卖家改了价
    ]
    check("同一商品链接 → 只保留 1 条（改标题/改价都算同一件）",
          len(dedupe_quotes(same_item)) == 1,
          f"实际 {len(dedupe_quotes(same_item))} 条")

    # ---- ② 不同链接即使标题价格都一样 → 是两件商品，都要留 ----
    diff_items = [q("全新 4090", 19999, ITEM_A), q("全新 4090", 19999, ITEM_B)]
    check("不同商品链接 → 保留 2 条（同价同标题也是两件货）",
          len(dedupe_quotes(diff_items)) == 2,
          f"实际 {len(dedupe_quotes(diff_items))} 条")

    # ---- ③ 回落：搜索页 URL（京东/拼多多现状）→ 行为与改前一致 ----
    fallback_same = [
        q("七彩虹 5070", 4599, SEARCH),
        q("七彩虹 5070", 4599, SEARCH),
    ]
    check("无商品链接时回落文本键 → 完全重复的仍被去掉",
          len(dedupe_quotes(fallback_same)) == 1)

    fallback_diff = [
        q("七彩虹 5070", 4599, SEARCH),
        q("影驰 5070", 4599, SEARCH),
        q("七彩虹 5070", 4699, SEARCH),
    ]
    check("无商品链接时**不会误合并**不同标题/价格（否则整个型号被并成 1 条）",
          len(dedupe_quotes(fallback_diff)) == 3,
          f"实际 {len(dedupe_quotes(fallback_diff))} 条")

    # ---- ④ 身份键本身：两类前缀必须可区分 ----
    check("身份键：有链接用 link: 前缀",
          quote_identity("xianyu", "t", 1, ITEM_A).startswith("link:"))
    check("身份键：无链接用 text: 前缀（且含平台，避免跨平台误合并）",
          quote_identity("jd", "t", 1, SEARCH).startswith("text:jd|"))
    check("身份键：同链接不同输入产出同一键",
          quote_identity("xianyu", "a", 1, ITEM_A + "&spm=x") ==
          quote_identity("xianyu", "b", 2, ITEM_A))

    # ---- ⑤ 规则只有一份实现（不允许两处各写一套）----
    import pathlib as _pl
    base_src = _pl.Path("app/collectors/base.py").read_text(encoding="utf-8")
    tq_src = _pl.Path("app/services/task_queue.py").read_text(encoding="utf-8")
    check("落盘读回与内存去重共用同一身份键实现",
          "quote_identity(" in base_src and "quote_identity(" in tq_src)
    check("task_queue 从 base 导入该实现（而不是自己再写一份）",
          "from ..collectors.base import Quote, quote_identity" in tq_src)


def test_stage_counts() -> None:
    """轮次分段计数 —— 让「某源今天数据少」能定位到具体哪一段。

    各段失败**表现完全一样**（都是 0 条），但处置方式不同：
    网络没到 / 页面没就绪 / 接口没命中 / 解析失败 / 入库被过滤。
    借鉴自参考项目用三个分段计数区分「没推到」与「推到了没解开」。
    """
    from app.collectors import base as B

    B.take_stage_counts()   # 归零
    check("分段计数：初始为空", B.stage_counts() == {})
    check("分段计数：空表格式化不产生噪声", B.format_stages() == "")

    B.note_stage("接口命中")
    B.note_stage("接口命中")
    B.note_stage("回落DOM")
    B.note_stage("有结果型号", 3)
    counts = B.stage_counts()
    check("分段计数：按名字累加（含 n 参数）",
          counts == {"接口命中": 2, "回落DOM": 1, "有结果型号": 3}, str(counts))
    check("分段计数：格式化含全部键值",
          B.format_stages().count("·") == 2 and "接口命中 2" in B.format_stages(),
          B.format_stages())

    taken = B.take_stage_counts()
    check("分段计数：取走返回累计值", taken == counts, str(taken))
    check("分段计数：取走即清空（数字不串轮）", B.stage_counts() == {})

    # ---- 接线：打点必须真的在（不然计数永远是空的）----
    import pathlib as _pl
    src = _pl.Path("app/collectors/base.py").read_text(encoding="utf-8")
    check("批次开头清空计数（否则上一轮数字串进来）",
          "take_stage_counts()  # 新一轮" in src)
    for stage in ("有结果型号", "空结果型号", "限流中止", "异常型号"):
        check(f"打点存在：{stage}", f'note_stage("{stage}")' in src)
    check("结束日志带上分段计数（否则要翻数据库才知道）",
          'logger.info("%s 分段计数：%s", label, format_stages(stages))' in src)

    xy = _pl.Path("app/collectors/xianyu_source.py").read_text(encoding="utf-8")
    check("闲鱼：接口命中与 DOM 回落分别打点（这才是最有区分度的一段）",
          'note_stage("接口命中")' in xy and 'note_stage("回落DOM")' in xy)

    pl = _pl.Path("app/services/pipeline.py").read_text(encoding="utf-8")
    check("分段计数进 summary（调用方不必翻日志）",
          '"stages": take_stage_counts(),' in pl)

    # ⚠️ 上面全是**文本**断言 —— 它们抓不到"少写了一个 import"。
    #    实测踩到过：源文件里明明有 note_stage("接口命中")，但模块没导入它，
    #    真实采集时报 NameError，而自检全绿。所以这里必须**真的导进来试试**。
    from app.collectors import xianyu_source as _xy_mod
    from app.collectors import base as _base_mod
    check("打点用的符号必须真的能解析（文本断言抓不到缺导入）",
          callable(getattr(_xy_mod, "note_stage", None))
          and callable(getattr(_base_mod, "note_stage", None))
          and callable(getattr(_base_mod, "take_stage_counts", None)))


def test_backoff_decay_and_floor() -> None:
    """退避阶梯的两个「实际生效」前提（2026-10-02 加）。

    它们解决同一类失效：**阶梯写在那里，但在真实调度上不起作用**。

      ① 最短生效时长 —— 第 1 级（硬 30 分钟 / 软 15 分钟）都**短于轮次间隔（1 小时）**，
         而 `fast_fail` 是在**下一轮开始时**才评估冷却的，到那时冷却早已过期。
         于是「第 1 级退避」一轮都没跳过，每小时照样去撞。
      ② 递减而非清零 —— 原来一次成功就把余量清零。平台「一小时被拦、下一小时
         侥幸采到」时台阶每次都被重置回第 1 级，**永远升不到第 2 级**
         （京东 2026-10-01 正是这个形态：下午被拦、晚上正常）。
    """
    from app.services import breaker

    # ---- 阶梯本身没动值（生效时长只在 trip() 里抬，纯函数保持原值）----
    check("backoff_for 仍是阶梯原值（抬升只发生在 trip 里）",
          [breaker.backoff_for(n) for n in (1, 2, 3, 4)] == [1800.0, 7200.0, 21600.0, 86400.0],
          f"实际 {[breaker.backoff_for(n) for n in (1, 2, 3, 4)]}")

    check("默认最短生效时长 ≥ 1 小时（否则第 1 级在每小时一轮的调度上空转）",
          breaker.min_cooldown() >= 3600.0, f"实际 {breaker.min_cooldown()}")

    tmp = Path(tempfile.mkdtemp(prefix="diyprice_breaker_eff_"))
    original = breaker.BREAKER_FILE
    breaker.BREAKER_FILE = tmp / "breaker.json"
    try:
        # ---- ① 第 1 级被抬到 > 1 小时 ----
        breaker.clear()
        breaker.trip("jd", reason="访问频繁")
        left = breaker.cooldown_remaining("jd")
        check("硬拦截第 1 级被抬到 > 1 小时（阶梯原值只有 30 分钟）",
              left > 3600.0, f"实际 {left:.0f}s")
        # 真跑一次第 2 级，确认下限不会把更长的级**压短**
        breaker.trip("jd", reason="访问频繁")
        check("下限不会把更长的级压短（第 2 级仍是 2 小时）",
              7180 < breaker.cooldown_remaining("jd") <= 7200,
              f"实际 {breaker.cooldown_remaining('jd'):.0f}s")

        # ---- 软风控第 1 级（15 分钟）原本同样是空转 ----
        breaker.clear()
        breaker.trip("pdd", reason="系统繁忙")
        soft_left = breaker.cooldown_remaining("pdd")
        check("软风控第 1 级被抬到 > 1 小时（原值 15 分钟）",
              soft_left > 3600.0, f"实际 {soft_left:.0f}s")
        check("软风控仍受总上限约束（上限优先于下限）",
              soft_left <= breaker.soft_cap(), f"{soft_left:.0f} > {breaker.soft_cap():.0f}")

        # ---- 显式传 seconds 的路径**不**套下限（单测/排障要能设秒级）----
        breaker.clear()
        breaker.trip("jd", 2.0, "短冷却")
        exp_left = breaker.cooldown_remaining("jd")
        check("显式传 seconds 时不套最短生效时长（排障要能设秒级）",
              1.5 < exp_left <= 2.0, f"实际 {exp_left:.2f}s")

        # ---- ② 递减而不是清零 ----
        breaker.clear()
        for _ in range(3):
            breaker.trip("jd", 0.05, "短冷却")      # 显式传值 → 冷却立即过期
        check("连续 3 次 → 余量 3", breaker.consecutive_trips("jd") == 3)
        time.sleep(0.12)
        check("冷却过期后 record_success 被接受", breaker.record_success("jd") is True)
        check("成功一轮只**减一级**（不是清零）—— 一次侥幸不该抹掉整条阶梯",
              breaker.consecutive_trips("jd") == 2,
              f"实际 {breaker.consecutive_trips('jd')}")
        check("递减后不再处于冷却（余量保留、冷却作废）",
              breaker.is_cooling("jd") is False and breaker.snapshot() == {})

        breaker.trip("jd", reason="访问频繁")
        check("余量 2 → 再熔断走到第 3 级（21600s），不是从第 1 级重来",
              breaker.consecutive_trips("jd") == 3
              and breaker.cooldown_remaining("jd") > 21000.0,
              f"trips={breaker.consecutive_trips('jd')} "
              f"left={breaker.cooldown_remaining('jd'):.0f}s")
        breaker.clear()

        # ---- 减到 0 才整条清掉 ----
        for _ in range(2):
            breaker.trip("jd", 0.05, "短冷却")
        time.sleep(0.12)
        check("余量 2 → 1",
              breaker.record_success("jd") is True
              and breaker.consecutive_trips("jd") == 1)
        check("余量 1 → 0（记录整条清掉，下次从第 1 级开始）",
              breaker.record_success("jd") is True
              and breaker.consecutive_trips("jd") == 0
              and breaker.entry_of("jd") == {})

        # ---- 探针 verified=True 仍然一次清掉 ----
        breaker.clear()
        for _ in range(4):
            breaker.trip("jd", 3600, "访问频繁")
        check("余量 4 且冷却中",
              breaker.consecutive_trips("jd") == 4 and breaker.is_cooling("jd"))
        check("探针 verified=True 一次清掉（那是证据，不是侥幸）",
              breaker.record_success("jd", verified=True) is True
              and breaker.consecutive_trips("jd") == 0)

        # ---- 下限可覆盖 / 可关掉 ----
        with mock.patch.dict(os.environ, {"DIYPRICE_BREAKER_MIN_COOLDOWN": "120"}):
            check("DIYPRICE_BREAKER_MIN_COOLDOWN 覆盖生效",
                  breaker.min_cooldown() == 120.0, f"实际 {breaker.min_cooldown()}")
            breaker.clear()
            breaker.trip("jd", reason="访问频繁")
            over_left = breaker.cooldown_remaining("jd")
            check("下限压到 120s 后第 1 级回到阶梯原值 1800s",
                  1790 < over_left <= 1800, f"实际 {over_left:.0f}s")
        with mock.patch.dict(os.environ, {"DIYPRICE_BREAKER_MIN_COOLDOWN": "0"}):
            check("设为 0 可关掉下限",
                  breaker.min_cooldown() == 0.0, f"实际 {breaker.min_cooldown()}")
        with mock.patch.dict(os.environ, {"DIYPRICE_BREAKER_MIN_COOLDOWN": "abc"}):
            check("非法值回落到默认（不炸）",
                  breaker.min_cooldown() == 3900.0, f"实际 {breaker.min_cooldown()}")

        # ---- 接线：采集侧日志必须报出余量变化（否则"减一级"看不出来）----
        import pathlib as _pl
        bsrc = _pl.Path("app/collectors/base.py").read_text(encoding="utf-8")
        check("采集侧报出余量变化（before → after）",
              '"%s 本轮正常采到 %d 条且未触发限流，退避阶梯余量 %d → %d%s"' in bsrc)
        check("采集侧先读 before 再调 record_success（顺序反了会永远报 0）",
              bsrc.index("before = breaker.consecutive_trips(code)")
              < bsrc.index("if breaker.record_success(code) and before:"))
    finally:
        breaker.BREAKER_FILE = original
        shutil.rmtree(tmp, ignore_errors=True)


def test_registry_is_clean() -> None:
    """元守卫：测试注册表与定义必须一一对应。

    为什么需要它（2026-10-01 实际踩到两次）：
      1. 补丁脚本被重复执行 → 同一个测试函数被插入两次，注册表里也出现两遍；
         "通过 N/N" 里 N 虚增，而**没有任何断言失效**，看起来一切正常。
      2. 用块替换法改文件时，把夹在中间的某个 test_* 定义删掉了，但注册表
         还留着它的名字 → 自检直接 NameError 崩掉，**所有反向验证都会假阳性**
         （注入什么都"被拦住"，因为基线根本没跑起来）。

    所以这里同时钉三件事：注册表无重复、定义无重复、定义与注册一一对应。
    """
    import pathlib as _pl
    import re as _re

    src = _pl.Path(__file__).read_text(encoding="utf-8")

    block = _re.search(r"tests = \((.*?)\n    \)", src, _re.S)
    check("能定位到测试注册表", block is not None)
    if block is None:
        return

    names = [ln.strip().rstrip(",") for ln in block.group(1).splitlines() if ln.strip()]
    dupes = sorted({n for n in names if names.count(n) > 1})
    check("测试注册表无重复项", not dupes, f"重复：{dupes}")

    defs = _re.findall(r"^def (test_\w+)", src, _re.M)
    dup_defs = sorted({d for d in defs if defs.count(d) > 1})
    check("没有重复定义的测试函数", not dup_defs, f"重复定义：{dup_defs}")

    missing = sorted(set(names) - set(defs))
    check("注册表里的每个测试都有定义（否则自检直接崩，反向验证会假阳性）",
          not missing, f"有注册无定义：{missing}")

    unregistered = sorted(set(defs) - set(names) - {"test_registry_is_clean"})
    check("每个 test_* 函数都已注册（否则写了等于没写）",
          not unregistered, f"未注册：{unregistered}")


def main() -> int:
    tests = (
        test_backoff_decay_and_floor,
        test_stage_counts,
        test_dedupe_identity,
        test_xianyu_api_parse,
        test_registry_is_clean,
        test_price_normalization,
        test_browser_proxy_policy,
        test_ranking_direction,
        test_ranking_empty,
        test_request_slimming,
        test_alerting_rules,
        test_page_dead_not_swallowed,
        test_network_error_not_counted_as_empty,
        test_coverage_scope_matches_collection,
        test_alert_config_update,
        test_stall_guard_config,
        test_profile_crash_flag_reset,
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
        test_report_sections,
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
