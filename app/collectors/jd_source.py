"""京东商品搜索适配器 —— 需要登录态。

与 ZOL / 太平洋这类静态报价站完全不同，京东是前后端分离 + 强反爬：
  · 未登录直接请求只返回 2.7KB 拦截页（实测）
  · 商品列表由 React 渲染，类名带构建哈希（如 `_price_9y3st_31`），
    哈希会随京东发版变化，不能写死

因此走「真实浏览器 + CDP」这条路：
  1. 用户执行 `python -m app.cli login --site jd` 扫码一次，
     登录态持久化在独立浏览器 profile 里（见 services/session.py）
  2. 采集时复用该浏览器，Playwright 通过 CDP 连上去逐型号搜索
  3. 解析只依赖**一个稳定的容器类名** `plugin_goodsCardWrapper`，
     价格与标题都从卡片文本里提取 —— 京东改版时不至于立刻失效

关于限流（实测数据）：
  · 触发条件不是"总次数"，而是**频率与节奏规律** —— 按固定 3 秒连搜 7~8 次必中
  · 被拦后并非全天不可用，**冷却约 20~25 分钟**即可恢复
  · 所以单轮采 6 个、隔一段时间再来一轮，一天足以覆盖整个型号库
  应对手段见下：拟人化随机节奏（humanize）+ 游标轮转（services/cursor）。
"""
from __future__ import annotations

import logging
import os
import re
import urllib.parse
from datetime import date

from . import policy
from .base import BaseCollector, Quote, page_dead, run_browser_batch
from .registry import register

logger = logging.getLogger("diyprice.collector.jd")

SEARCH_URL = "https://search.jd.com/Search?keyword={kw}&enc=utf-8"

# 唯一依赖的类名：京东给插件化卡片保留的可读类名，不带构建哈希
CARD_SELECTOR = '[class*="goodsCardWrapper"]'
PRICE_SELECTOR = '[class*="_price_"]'

_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _to_price(text: str) -> float | None:
    if not text:
        return None
    match = _NUM_RE.search(text.replace(",", ""))
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return value if value > 0 else None


@register
class JdCollector(BaseCollector):
    """京东在售价格采集器（依赖登录态）。"""

    code = "jd"
    name = "京东"

    # 京东限流比预想更严：实测「恢复」后也只能再搜几次就再次被拦
    # （17:40 探测正常 → 17:43 采集又中）。所以单轮量压到 2 个、间隔拉到 20~45 秒。
    #
    # 为什么是 2 而不是 4：实测每轮通常只有**第 1 个**型号能采到，之后连续空结果
    # 触发退避。留 4 个名额只是白等 2 分半钟（3 次空结果 × 各自的重试间隔），
    # 不会多拿到数据，还给账号添了一道访问记录。
    default_limit = 2        # 单轮最多采几个型号（环境变量 DIYPRICE_JD_LIMIT 可覆盖）
    # ⚠️ 请求间隔与页面等待**不在这里配** —— 统一由
    # `collectors/policy.PLATFORM_THROTTLE_CONFIG["jd"]` 提供
    # （高斯扰动 4~7s + 上下限夹取、就绪判据、行为模拟）。
    # 原来这里另有一组 interval_min/max，和策略各写一套迟早互相矛盾。
    max_empty_streak = 3     # 连续这么多次空结果就判定限流并提前结束，不去撞墙
    page_timeout = 30000

    @property
    def supported_platforms(self) -> list[str]:
        return ["jd"]

    def collect(self, platform, products: list, day: date) -> list[Quote]:
        if platform.code != "jd":
            return []
        if day != date.today():  # 搜索页只反映当前在售价，没有历史
            return []

        from ..services.session import load_session

        if load_session("jd") is None:
            logger.warning("京东未登录，跳过。先执行：python -m app.cli login --site jd")
            return []

        # 浏览器交给 browser_worker 管（短生命周期 + 按阈值回收），
        # 队列、增量落盘、断点续爬都在 base.run_browser_batch 里。
        #
        # 相比改造前删掉了两处：
        #   · 自己 launch_browser + connect_over_cdp + new_page
        #     → worker.page() 统一负责，并在借用前决定是否回收重启
        #   · 「本轮 0 条就 save_offset 回退游标」
        #     → 那个逻辑会把游标钉死在原地（同批型号→0条→回退→同批型号…），
        #       实测让京东空转了 3 个多小时。现在任务留在队列里下轮优先重做，
        #       游标继续前进。
        return run_browser_batch(
            self,
            products,
            day,
            site="jd",
            limit=int(os.getenv("DIYPRICE_JD_LIMIT", str(self.default_limit))),
            search_fn=self._search,
            empty_streak_limit=self.max_empty_streak,
        )

    # ------------------------------------------------------------------ 内部

    def _search(self, page, product) -> list[Quote]:
        url = SEARCH_URL.format(kw=urllib.parse.quote(product.model))
        try:
            # navigate 按平台策略等到"可以取数"（商品卡片出现），并**先查一次限流**。
            # 命中会抛 RateLimitError —— 那是熔断信号，必须原样上抛给
            # run_browser_batch，绝不能被下面的 except 吞成"搜索失败"：
            # 吞掉它就会变成"记一笔失败、继续拿下一个型号去撞"，正是要消灭的行为。
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
            logger.warning("%s搜索失败 %s：%s", "京东", product.model, exc)
            return []

        # 提取前行为：平滑向下滚动 300~600px → 悬停 1~2s，
        # 然后静默一段等 whwswswws 等埋点参数上报完再取数。
        policy.behave(page, self.code)
        policy.settle(page, self.code)
        # settle 期间页面也可能被替换成限流页，取数前再确认一次
        policy.assert_not_rate_limited(page, self.code)

        try:
            cards = page.locator(CARD_SELECTOR)
            count = cards.count()
        except Exception as exc:
            # ⚠️ 和 _search 同理：页面/浏览器已关闭不能被吞成"卡片定位失败"。
            # 吞掉会累加 empty_streak，连续 3 次就误判为限流 —— 方向指反。
            if page_dead(exc):
                raise
            logger.warning("京东卡片定位失败 %s：%s", product.model, exc)
            return []

        quotes: list[Quote] = []
        for i in range(count):
            card = cards.nth(i)
            try:
                price = _to_price(card.locator(PRICE_SELECTOR).first.inner_text(timeout=1200))
            except Exception:
                continue
            if price is None:
                continue

            title = self._title_of(card)
            if not title:
                continue

            quotes.append(
                Quote(
                    platform_code="jd",
                    title_raw=title,
                    price=price,
                    condition="全新",
                    url=url,
                    seller="京东",
                    extra={"category": product.category, "keyword": product.model},
                )
            )
        return quotes

    @staticmethod
    def _title_of(card) -> str:
        """标题取卡片文本的第一行有效内容（京东把标题渲染在卡片顶部）。"""
        try:
            text = card.inner_text(timeout=1200)
        except Exception:
            return ""
        for line in text.split("\n"):
            line = line.strip()
            if len(line) > 4 and line not in {"广告", "到手价", "自营"}:
                return line[:200]
        return ""
