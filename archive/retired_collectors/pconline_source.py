"""太平洋电脑网（PConline）报价适配器。

与 ZOL 同类的服务端渲染静态站：GBK 编码、无需登录、无需浏览器。
作为第二个真实源，接入后可形成「多平台真实比价」——这是纯 Mock 数据给不了的价值。

实测确认的页面结构（2026-09）：
    <div class="item-detail">
      <div class="item-title">
        <h3><a href="//product.pconline.com.cn/vga/asus/2610999.html"
               class="item-title-name"
               title="华硕DUAL GeForce RTX 5060 Ti O16G">…</a></h3>
        <span class="item-title-des">显卡类型：…；芯片型号：NVIDIA GeForce RTX 5060 Ti；…</span>
      </div>
    </div>
    <div class="item-sales">
      <div class="price price-now">
        <a href="…_price.html" title="…报价">￥3599</a>   ← 价格包在 <a> 里
      </div>
    </div>

注意：价格节点是 `<div class="price price-now">` **里面再套 `<a>`**，
正则必须匹配到内层文本，且用负向断言防止跨条目错配。

同样没有历史价格接口，回填过去日期直接跳过。
"""
from __future__ import annotations

import logging
import re
import time as _time
import urllib.error
from datetime import date

from .base import BaseCollector, Quote
from .fetcher import absolute, fetch_html
from .registry import register

logger = logging.getLogger("diyprice.collector.pconline")

_BASE = "https://product.pconline.com.cn"
# 品类 → 太平洋分类路径（已在真实站点确认）
CATEGORY_PATHS: dict[str, str] = {
    "gpu": "vga",
    "cpu": "cpu",
    "ram": "memory",
    "mb": "mb",
    "ssd": "dianziyingpan",
    "psu": "power",
    "cooler": "sanre",
    "case": "case",
}

REQUEST_INTERVAL = 0.6

# (?:(?!item-title-name).)*? 保证不越过下一条目，避免无价格条目“借用”下一条的价格
_ITEM_RE = re.compile(
    r'<a[^>]*class="item-title-name"[^>]*title="([^"]+)"[^>]*>'
    r'(?:(?!item-title-name).)*?'
    r'class="price price-now"[^>]*>\s*<a[^>]*>\s*[￥¥]\s*([\d,]+)',
    re.S,
)


@register
class PconlineCollector(BaseCollector):
    """太平洋电脑网报价采集器。"""

    code = "pconline"
    name = "太平洋电脑网"

    def __init__(self) -> None:
        self._cache: dict[str, list[Quote]] = {}

    @property
    def supported_platforms(self) -> list[str]:
        return ["pconline"]

    def collect(self, platform, products: list, day: date) -> list[Quote]:
        if platform.code != "pconline":
            return []
        if day != date.today():  # 无历史价接口
            return []

        categories = sorted({p.category for p in products})
        cache_key = ",".join(categories)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        quotes: list[Quote] = []
        for category in categories:
            path = CATEGORY_PATHS.get(category)
            if not path:
                logger.warning("太平洋未配置品类路径：%s", category)
                continue
            url = f"{_BASE}/{path}/"
            try:
                html = fetch_html(url, referer=f"{_BASE}/", encodings=("gbk", "utf-8"))
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                logger.warning("太平洋抓取失败 %s：%s", url, exc)
                continue
            found = self._parse(html, category)
            logger.info("太平洋 %s（/%s/）解析到 %d 条报价", category, path, len(found))
            quotes.extend(found)
            _time.sleep(REQUEST_INTERVAL)

        self._cache[cache_key] = quotes
        return quotes

    @staticmethod
    def _parse(html: str, category: str | None = None) -> list[Quote]:
        """解析品类列表页，category 写入 extra 供归一化做品类隔离。"""
        quotes: list[Quote] = []
        for title, price_text in _ITEM_RE.findall(html):
            name = title.strip()
            if not name:
                continue
            try:
                price = float(price_text.replace(",", ""))
            except ValueError:
                continue
            if price <= 0:
                continue
            quotes.append(
                Quote(
                    platform_code="pconline",
                    title_raw=name,
                    price=price,
                    condition="全新",
                    url=f"{_BASE}/",
                    seller="太平洋参考报价",
                    extra={"category": category},
                )
            )
        return quotes
