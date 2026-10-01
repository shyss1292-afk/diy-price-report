"""拼多多搜索适配器 —— 需要登录态。

与京东的差异（实测）：
  · 未登录访问搜索页会被**强制重定向**到 login.html（京东只是返回拦截页）
  · 页面是移动端 H5，需要移动 UA + 窄视口
  · 商品类名是纯哈希（`rjNMXsUm` / `_2OP4vH_6`），完全不可依赖
  · 好消息：价格是**文本**而非图片（`券后` / `¥` / `6893` 分行渲染）

因此解析策略用「文本锚点」而不是「CSS 选择器」：
  找到所有文本恰为 `¥` 的叶子节点 → 向上扩张容器，直到容器里出现第二个 `¥` 为止，
  此时容器正好是单个商品卡片；卡片首行是标题，`¥` 的下一行是价格。
这样即使拼多多整站重写类名，提取逻辑也不会失效。

关于限流：实测连续 6 次搜索未被拦，比京东宽松。但节奏规律化仍会积累风险，
所以同样接入拟人化随机间隔与游标轮转。
"""
from __future__ import annotations

import logging
import os
import urllib.parse
from datetime import date

from . import normalize
from . import policy
from .base import BaseCollector, Quote, page_dead, run_browser_batch
from .registry import register

logger = logging.getLogger("diyprice.collector.pdd")

# 首页。搜索前必须先落地这里 —— 直接深链搜索 URL 会触发安全验证，
# 成因与实测见 `_search()` 里的预热注释。
HOME_URL = "https://mobile.yangkeduo.com/"
SEARCH_URL = "https://mobile.yangkeduo.com/search_result.html?search_key={kw}"

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

# 纯文本锚点提取，不依赖任何类名（类名每次发版都变）
_EXTRACT_JS = r"""() => {
    const out = [];
    document.querySelectorAll('*').forEach(el => {
        if (el.children.length !== 0) return;
        if ((el.textContent || '').trim() !== '¥') return;

        // 向上扩张，直到容器里出现第二个 ¥ —— 此时 card 正好是单个商品卡片
        let card = el;
        while (card.parentElement) {
            const parent = card.parentElement;
            const text = parent.innerText || '';
            if ((text.match(/¥/g) || []).length !== 1) break;
            card = parent;
        }

        const lines = (card.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
        const idx = lines.lastIndexOf('¥');
        if (idx < 0) return;

        // 价格可能被**拆成多行**渲染。2026-10-01 实测闲鱼 RTX 4090：
        //     ¥ ⏎ 2 ⏎ .30 ⏎ 万      （2.30万 = 23000）
        //     ¥ ⏎ 18 ⏎ .88           （18.88）
        //     ¥ ⏎ 4030               （4030）
        // 旧实现只取「¥ 的下一行」→ 把 2.30万 记成了 2，
        // 净效果是所有万元级二手报价被静默丢弃。
        // 这里只**原样取片段**，拼装与判读交给 Python（那边能写单测）。
        const seg = [];
        for (let k = idx + 1; k < lines.length && seg.length < 4; k++) {
            const t = lines[k];
            if (seg.length === 0) {
                // 首段：整数或小数（允许 .88 这种省略整数部分的写法）
                if (!/^(\d[\d,]*|\d+\.\d+|\.\d+)$/.test(t)) break;
                seg.push(t);
            } else if (/^\.\d+$/.test(t) || t === '万') {
                // 后续段：只接受小数部分或单位，遇到别的（如「56人想拼」）就停
                seg.push(t);
            } else {
                break;
            }
        }
        if (!seg.length) return;

        out.push({ title: lines[0] || '', priceSeg: seg });
    });
    return out;
}"""


@register
class PddCollector(BaseCollector):
    """拼多多在售价格采集器（依赖登录态）。"""

    code = "pdd"
    name = "拼多多"

    default_limit = 6        # 单轮最多采几个型号（DIYPRICE_PDD_LIMIT 可覆盖）
    # ⚠️ 请求间隔与页面等待**不在这里配** —— 统一由
    # `collectors/policy.PLATFORM_THROTTLE_CONFIG["pdd"]` 提供
    # （等待 networkidle + 700ms 签名窗口、行为模拟、单会话任务上限）。
    # 原来这里另有一组 interval_min/max，两套配置迟早互相矛盾。
    scroll_pixels = 1200     # 功能性滚动：触发懒加载（与拟人化滚动是两回事）
    max_empty_streak = 3     # 连续这么多次空结果就判定被风控
    page_timeout = 35000

    @property
    def supported_platforms(self) -> list[str]:
        return ["pdd"]

    def collect(self, platform, products: list, day: date) -> list[Quote]:
        if platform.code != "pdd":
            return []
        if day != date.today():  # 搜索页只反映当前在售价，没有历史
            return []

        from ..services.session import load_session

        if load_session("pdd") is None:
            logger.warning("拼多多未登录，跳过。先执行：python -m app.cli login --site pdd")
            return []

        # 移动端 H5 的两件必需品：窄视口 + 移动 UA，交给 run_browser_batch 统一设置。
        #
        # 视口仍走 page.set_viewport_size：worker 已经把窗口放在屏幕外，
        # 缩小窗口尺寸影响不到用户；而改用 setDeviceMetricsOverride 会让
        # window.innerWidth 与真实窗口尺寸脱钩，反而多一层可识别的矛盾。
        return run_browser_batch(
            self,
            products,
            day,
            site="pdd",
            limit=int(os.getenv("DIYPRICE_PDD_LIMIT", str(self.default_limit))),
            search_fn=self._search,
            viewport={"width": 430, "height": 900},
            user_agent=MOBILE_UA,
            empty_streak_limit=self.max_empty_streak,
        )

    # ------------------------------------------------------------------ 内部

    def _search(self, page, product) -> list[Quote]:
        url = SEARCH_URL.format(kw=urllib.parse.quote(product.model))

        # 预热：先落到首页，再进搜索页。
        #
        # 为什么需要（2026-09-24 实测）
        # ----------------------------
        # **直接深链到搜索 URL 会被重定向到 `psnl_verification.html`（安全验证）**，
        # 采集器把它判成限流 → 整轮 0 条 → 连续几次就吃满退避阶梯。
        # 今早 08:01 那次「命中 login.html → 24 小时退避」有一部分就是这个成因。
        #
        # 先访问一次首页拿到会话上下文再搜索就正常 —— 实测连抓 3 个型号
        # **3/3 成功、每个 20 条**（RTX 5090 ¥49830 / RTX 5080 ¥18300 /
        # RTX 5070 Ti ¥9389），耗时 ~8.7s/型号。
        #
        # 判据用「当前 URL 不含 search_result」而不是「只做一次」：
        # 这样被验证页劫持之后（URL 变成 psnl_verification.html）下一轮
        # 会自动重新预热，**能自愈**，不用重启 Worker。
        try:
            if "search_result" not in (page.url or ""):
                page.goto(HOME_URL, wait_until="domcontentloaded", timeout=self.page_timeout)
                page.wait_for_timeout(1200)
        except Exception:  # noqa: BLE001 —— 预热失败不致命，继续走正常流程
            pass

        try:
            # navigate 内按策略严格等到 networkidle（或核心商品节点出现），
            # 再额外留 700ms 给前端 JS 完成 anti-content 签名运算 ——
            # 早于它取数只能拿到未签名的空壳 DOM。
            # 被重定向到验证页 / error_code=40001 会抛 RateLimitError（熔断信号）。
            policy.navigate(page, url, self.code, timeout=self.page_timeout)
        except policy.RateLimitError:
            raise
        except Exception as exc:
            # ⚠️ 「页面/浏览器已关闭」**不能**被吞成"搜索失败"。
            # 吞掉之后 run_browser_batch 会把它当成"搜索无结果"累加 empty_streak，
            # 连续 3 次就**误判为被限流**并提前结束本轮 —— 实测 2026-10-01 20:30
            # 闲鱼一轮 15 个型号只采到 1 个就"判定限流"退出，真凶其实是浏览器实例挂了。
            # 原样上抛，让上层走 mark_dirty + 重建实例（重建只要约 3 秒）。
            if page_dead(exc):
                raise
            logger.warning("%s搜索失败 %s：%s", "拼多多", product.model, exc)
            return []

        try:  # 功能性滚动一屏，触发懒加载
            page.evaluate(f"() => window.scrollTo(0, {self.scroll_pixels})")
            page.wait_for_timeout(1800)
        except Exception:
            pass

        policy.behave(page, self.code)
        policy.settle(page, self.code)
        policy.assert_not_rate_limited(page, self.code)

        try:
            rows = page.evaluate(_EXTRACT_JS)
        except Exception as exc:
            # ⚠️ 和 _search 同理：页面/浏览器已关闭不能被吞成"解析失败"。
            # 吞掉会累加 empty_streak，连续 3 次就误判为限流 —— 方向指反。
            if page_dead(exc):
                raise
            logger.warning("拼多多解析失败 %s：%s", product.model, exc)
            return []

        quotes: list[Quote] = []
        for row in rows or []:
            # 价格在页面上可能被拆成多行（¥ / 2 / .30 / 万），
            # 由 priceSeg 拼装 —— 拼装逻辑在 collectors/normalize.py（可单测）
            price = normalize.parse_split_price(row.get("priceSeg"))
            if price is None:
                continue
            title = (row.get("title") or "").strip()
            if price <= 0 or not title:
                continue
            quotes.append(
                Quote(
                    platform_code="pdd",
                    title_raw=title[:200],
                    price=price,
                    condition="全新",
                    url=url,
                    seller="拼多多",
                    extra={"category": product.category, "keyword": product.model},
                )
            )
        return quotes
