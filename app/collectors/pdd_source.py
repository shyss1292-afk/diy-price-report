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

from . import policy
from .base import BaseCollector, Quote, run_browser_batch
from .registry import register

logger = logging.getLogger("diyprice.collector.pdd")

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
        if (idx < 0 || !lines[idx + 1]) return;
        const price = lines[idx + 1].replace(/,/g, '');
        if (!/^\d+(\.\d+)?$/.test(price)) return;

        out.push({ title: lines[0] || '', price: price });
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
        try:
            # navigate 内按策略严格等到 networkidle（或核心商品节点出现），
            # 再额外留 700ms 给前端 JS 完成 anti-content 签名运算 ——
            # 早于它取数只能拿到未签名的空壳 DOM。
            # 被重定向到验证页 / error_code=40001 会抛 RateLimitError（熔断信号）。
            policy.navigate(page, url, self.code, timeout=self.page_timeout)
        except policy.RateLimitError:
            raise
        except Exception as exc:
            logger.warning("拼多多搜索失败 %s：%s", product.model, exc)
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
            logger.warning("拼多多解析失败 %s：%s", product.model, exc)
            return []

        quotes: list[Quote] = []
        for row in rows or []:
            try:
                price = float(row.get("price"))
            except (TypeError, ValueError):
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
