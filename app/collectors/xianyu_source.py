"""闲鱼（Goofish）搜索适配器 —— 需要登录态。

闲鱼是这几个电商里技术最特殊的：
  · 前后端完全分离，商品数据走 `h5api.m.goofish.com` 的 **mtop 接口**
  · 接口带 `sign=` 签名参数，逆向签名算法成本很高
  · 但签名是**浏览器端 mtop.js 自己算的** —— 所以这里不逆向签名，
    而是用真实浏览器加载页面，直接读它自己发出去、拿回来的数据

实测要点（2026-09）：
  · 未登录时搜索接口返回空（157B），必须登录。
    登录后新增的关键 Cookie：`unb`（淘宝用户 ID）、`tracknick`、`sgcookie`
  · PC 网页版 `www.goofish.com` 就够用，不需要移动 UA（与拼多多不同）
  · 价格是**文本**而非图片，但 `¥` 与数字之间**有换行**（`¥\\n3900`）——
    这是最初误判"拿不到数据"的原因，解析必须按行处理

解析用文本锚点（同拼多多）：找文本恰为 `¥` 的叶子节点，
向上扩张容器直到出现第二个 `¥` 为止，此时容器正好是单个商品卡片。

注意：站点 code 是 `goofish`（会话文件），平台 code 是 `xianyu`（platforms 表）。
"""
from __future__ import annotations

import logging
import os
import urllib.parse
from datetime import date

from . import policy
from .base import BaseCollector, Quote, page_dead, run_browser_batch
from .registry import register

logger = logging.getLogger("diyprice.collector.xianyu")

SEARCH_URL = "https://www.goofish.com/search?q={kw}"

# 站点 code（用于读写登录会话）与平台 code（用于入库）不同名，这里显式声明
SITE_CODE = "goofish"
PLATFORM_CODE = "xianyu"

# 纯文本锚点提取，不依赖任何类名
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

_NEW_KEYWORDS = ("全新", "未拆封", "未拆", "仅拆封", "全新未使用")


def _condition_of(title: str) -> str:
    """闲鱼以二手为主，但不少标着"全新未拆封"，按标题粗略区分。"""
    return "全新" if any(k in title for k in _NEW_KEYWORDS) else "二手"


@register
class XianyuCollector(BaseCollector):
    """闲鱼在售价格采集器（依赖登录态）。"""

    code = PLATFORM_CODE
    name = "闲鱼"

    @property
    def browser_site(self) -> str:
        """闲鱼是唯一 code 与浏览器站点不同名的源（会话文件叫 goofish）。"""
        return SITE_CODE

    default_limit = 6        # 单轮最多采几个型号（DIYPRICE_XIANYU_LIMIT 可覆盖）
    page_wait_ms = 8000      # 闲鱼首屏渲染偏慢
    scroll_pixels = 1200     # 滚动触发懒加载
    # ⚠️ 请求间隔**不在这里配** —— 见 collectors/policy.PLATFORM_THROTTLE_CONFIG["xianyu"]
    max_empty_streak = 3
    page_timeout = 40000

    @property
    def supported_platforms(self) -> list[str]:
        return [PLATFORM_CODE]

    def collect(self, platform, products: list, day: date) -> list[Quote]:
        if platform.code != PLATFORM_CODE:
            return []
        if day != date.today():  # 搜索页只反映当前在售价，没有历史
            return []

        from ..services.session import load_session

        if load_session(SITE_CODE) is None:
            logger.warning(
                "闲鱼未登录，跳过。先执行：python -m app.cli login --site goofish"
            )
            return []

        # 注意 site 与 code 不同名：site=goofish 用于读登录会话，
        # collector.code=xianyu 用于队列与入库（见文件头说明）。
        # PC 网页版就够用，不需要移动 UA（与拼多多不同）。
        return run_browser_batch(
            self,
            products,
            day,
            site=SITE_CODE,
            limit=int(os.getenv("DIYPRICE_XIANYU_LIMIT", str(self.default_limit))),
            search_fn=self._search,
            viewport={"width": 1440, "height": 900},
            empty_streak_limit=self.max_empty_streak,
        )

    # ------------------------------------------------------------------ 内部

    def _search(self, page, product) -> list[Quote]:
        url = SEARCH_URL.format(kw=urllib.parse.quote(product.model))
        try:
            # navigate 按平台策略导航并先查一次限流（闲鱼惩罚页是 `punish` 页）。
            # RateLimitError 是熔断信号，原样上抛，不在这里吞掉。
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
            logger.warning("%s搜索失败 %s：%s", "闲鱼", product.model, exc)
            return []

        # 闲鱼是单页应用，没有可靠的服务端渲染信号，保留一个显式首屏等待
        page.wait_for_timeout(self.page_wait_ms)
        try:  # 滚动触发懒加载
            page.evaluate(f"() => window.scrollTo(0, {self.scroll_pixels})")
            page.wait_for_timeout(2500)
        except Exception:
            pass

        # 行为模拟：闲鱼对"鼠标是否有真实位移"较敏感，
        # 光标从头到尾停在 (0,0) 本身就是脚本特征
        policy.behave(page, self.code)
        policy.settle(page, self.code)
        policy.assert_not_rate_limited(page, self.code)

        try:
            rows = page.evaluate(_EXTRACT_JS)
        except Exception as exc:
            logger.warning("闲鱼解析失败 %s：%s", product.model, exc)
            return []

        quotes: list[Quote] = []
        for row in rows or []:
            try:
                price = float(row.get("price"))
            except (TypeError, ValueError):
                continue
            title = (row.get("title") or "").strip()
            if price <= 0 or len(title) < 4:
                continue
            quotes.append(
                Quote(
                    platform_code=PLATFORM_CODE,
                    title_raw=title[:200],
                    price=price,
                    condition=_condition_of(title),
                    url=url,
                    seller="闲鱼卖家",
                    extra={"category": product.category, "keyword": product.model},
                )
            )
        return quotes
