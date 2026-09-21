"""采集器共用的 HTTP 抓取工具。

国内报价站普遍用 GBK 编码，且多为服务端渲染的静态页 ——
标准库 urllib 足够，不为采集引入额外依赖。

⚠️ 当前**没有调用方**：唯一的两个用户（ZOL、太平洋电脑网）已于 2026-09-17 停用，
现役的京东/拼多多/闲鱼都需要 Playwright + 登录态。保留此文件是因为它是
「静态 HTTP 源」的现成模板 —— 将来接入静态报价站时直接复用，不必重写编码兜底。
"""
from __future__ import annotations

import gzip
import urllib.parse
import urllib.request

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def fetch_html(
    url: str,
    *,
    referer: str | None = None,
    encodings: tuple[str, ...] = ("utf-8", "gbk"),
    timeout: int = 20,
) -> str:
    """抓取页面并按候选编码依次尝试解码（自动处理 gzip）。"""
    headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Connection": "close",
    }
    if referer:
        headers["Referer"] = referer

    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()

    if raw[:2] == b"\x1f\x8b":  # 服务端强制 gzip 时兜底
        raw = gzip.decompress(raw)

    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode(encodings[-1], errors="ignore")


def absolute(base: str, href: str) -> str:
    """补全 //host/path 与 /path 形式的链接。"""
    if href.startswith("//"):
        return "https:" + href
    return urllib.parse.urljoin(base, href)
