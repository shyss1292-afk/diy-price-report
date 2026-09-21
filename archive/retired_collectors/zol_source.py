"""中关村在线（ZOL）报价适配器 —— 首个真实数据源。

接入成本最低的真实源：品类报价列表页是服务端渲染的静态 HTML（GBK 编码），
无需登录、无需浏览器渲染，单次请求即可拿到整页型号与参考报价。

实测确认的页面结构（2026-09）：
    <li data-follow-id="p2118113">
      <a href="/vga/index2118113.shtml" class="pic"><img alt="NVIDIA RTX 5090"></a>
      <h3><a href="/vga/index2118113.shtml" title="NVIDIA RTX 5090">…</a></h3>
      <div class="price-row">
        <span class="price price-normal">
          <b class="price-sign">￥</b><b class="price-type">24399</b>
        </span>
      </div>
    </li>

实测发现的两个坑：
  1. 固态硬盘页面的价格是**区间格式**（"319-1899"），不是单一数字，
     这里取区间下限作为参考价，区间本身记入 extra；
  2. 部分条目是"暂无报价"（class 为 price-neg），正则天然跳过。

重要限制：
  ZOL 只提供**当前**参考报价，没有历史价格接口。
  因此回填过去日期时本适配器直接跳过 —— 历史曲线只能靠每日采集逐步积累。
"""
from __future__ import annotations

import gzip
import logging
import re
import time as _time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

from .base import BaseCollector, Quote
from .registry import register

logger = logging.getLogger("diyprice.collector.zol")

_BASE = "https://detail.zol.com.cn"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# 品类之间的请求间隔（秒），一次采集共 8 个品类页
REQUEST_INTERVAL = 0.6

# 品类 → ZOL 分类路径（均已在真实站点验证可访问且含报价）
CATEGORY_PATHS: dict[str, str] = {
    "gpu": "vga",
    "cpu": "cpu",
    "ram": "memory",
    "mb": "motherboard",
    "ssd": "solid_state_drive",
    "psu": "power",
    "cooler": "cooling_product",
    "case": "case",
}

# 解析分两步：先把页面切成「单个条目」，再在每个条目内部找标题与价格。
#
# 为什么不能一条正则搞定：列表里存在「暂无报价」的条目，如果用一个带 re.S 的
# 正则从头匹配，.*? 会**越过 <li> 边界**去下一个条目里找价格 —— 于是把下一个
# 商品的价格配给了当前标题（实测把 Ryzen 9 9950X 配成了 ¥538）。
# 先切分再各自匹配，结构上杜绝跨条目取价。
_ITEM_SPLIT = re.compile(r'<li data-follow-id="(p\d+)"\s*>')
_LINK_RE = re.compile(r'<a href="([^"]+)"[^>]*class="pic".*?alt="([^"]*)"', re.S)
_TITLE_RE = re.compile(r"<h3>\s*<a[^>]*title=\"([^\"]*)\"")
# 价格可能是单一数字（24399）或区间（319-1899 / 319~1899）
_PRICE_RE = re.compile(r'<b class="price-type">([\d,~\-]+)</b>')

_RANGE_SPLIT = re.compile(r"[-~]")


def fetch_html(url: str, timeout: int = 20) -> str:
    """抓取页面并解码（ZOL 使用 GBK）。"""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": f"{_BASE}/",
            "Connection": "close",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    if raw[:2] == b"\x1f\x8b":  # 服务端强制 gzip 时兜底
        raw = gzip.decompress(raw)
    for encoding in ("gbk", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("gbk", errors="ignore")


def parse_price(text: str) -> tuple[float, float] | None:
    """解析价格文本，返回 (最低, 最高)；无法解析返回 None。"""
    cleaned = text.replace(",", "").strip()
    if not cleaned:
        return None
    parts = [p for p in _RANGE_SPLIT.split(cleaned) if p]
    values: list[float] = []
    for part in parts:
        try:
            values.append(float(part))
        except ValueError:
            return None
    if not values:
        return None
    return min(values), max(values)


@register
class ZolCollector(BaseCollector):
    """ZOL 参考报价采集器。"""

    code = "zol"
    name = "中关村在线"

    def __init__(self) -> None:
        # 同一次运行内按需抓取，避免多日循环重复请求
        self._cache: dict[str, list[Quote]] = {}

    @property
    def supported_platforms(self) -> list[str]:
        return ["zol"]

    def collect(self, platform, products: list, day: date) -> list[Quote]:
        if platform.code != "zol":
            return []

        # ZOL 无历史价接口：回填过去日期直接跳过（否则会重复抓同一份当前报价）
        if day != date.today():
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
                logger.warning("ZOL 未配置品类路径：%s", category)
                continue
            url = f"{_BASE}/{path}/"
            try:
                html = fetch_html(url)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                logger.warning("ZOL 抓取失败 %s：%s", url, exc)
                continue
            found = self._parse(html, category)
            logger.info("ZOL %s（/%s/）解析到 %d 条报价", category, path, len(found))
            quotes.extend(found)
            _time.sleep(REQUEST_INTERVAL)  # 控制频率，避免给站点造成压力

        self._cache[cache_key] = quotes
        return quotes

    @staticmethod
    def _parse(html: str, category: str | None = None) -> list[Quote]:
        """解析品类列表页。

        category 会写入 Quote.extra，供归一化阶段做品类隔离，
        避免"支持5090显卡"这类机箱标题被误判成显卡型号。
        """
        quotes: list[Quote] = []
        # 按 <li data-follow-id="pXXXX"> 切成单个条目（split 会丢掉分隔符本身，
        # 所以用 finditer 记录每个条目的起点，再逐段切片）
        marks = list(_ITEM_SPLIT.finditer(html))
        for index, mark in enumerate(marks):
            start = mark.end()
            end = marks[index + 1].start() if index + 1 < len(marks) else len(html)
            chunk = html[start:end]

            # 标题与价格必须来自同一个条目 —— 任缺其一就跳过，
            # 绝不去别的条目里借价格（这正是 ¥538 错配的来源）
            m_title = _TITLE_RE.search(chunk)
            m_price = _PRICE_RE.search(chunk)
            if m_title is None or m_price is None:
                continue

            m_link = _LINK_RE.search(chunk)
            href = m_link.group(1) if m_link else ""
            alt = m_link.group(2) if m_link else ""

            name = (m_title.group(1) or alt).strip()
            if not name:
                continue
            parsed = parse_price(m_price.group(1))
            if parsed is None:
                continue
            low, high = parsed
            if low <= 0:
                continue
            quotes.append(
                Quote(
                    platform_code="zol",
                    title_raw=name,
                    price=low,  # 区间价取下限作为参考价
                    condition="全新",
                    url=urllib.parse.urljoin(_BASE, href) if href else "",
                    seller="ZOL 参考报价",
                    extra={
                        "zol_id": mark.group(1),
                        "category": category,
                        "price_range": [low, high] if high > low else None,
                    },
                )
            )
        return quotes
