"""抓取有效性抽检：三个平台各跑一个单型号，同时看 DOM 与提取结果。

目的：把"数据少"归因到具体环节 —— 是页面没渲染出来（拦截导致）、
被限流、Cookie 失效，还是抓到了但解析不出来。

用**生产采集器自己的 `_search`**，所以结果就是生产的真实行为。

用法：python -m scripts.probe_fetch
"""
from __future__ import annotations

import os
import sys
import time
from datetime import date

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

MODEL = "RTX 4060 Ti 8G"      # 一个确定有货的型号

DOM_JS = """() => {
    const body = (document.body ? document.body.innerText : '') || '';
    const links = document.querySelectorAll('a[href]').length;
    const imgs = document.querySelectorAll('img').length;
    const svgs = document.querySelectorAll('svg').length;
    // 各平台商品卡片的通用特征：带价格的链接块
    const priceLike = (body.match(/[¥￥]\\s*\\d{2,6}/g) || []).length;
    return {
        title: document.title,
        bodyLen: body.length,
        links, imgs, svgs, priceLike,
        bodyHead: body.slice(0, 260).replace(/\\s+/g, ' '),
        markers: ['验证', '滑动', '非法访问', 'punish', '访问频繁', '系统繁忙',
                  '请稍后再试', '登录', '安全验证'].filter(k => body.includes(k)),
    };
}"""


def main() -> int:
    from app.collectors.registry import get_collectors
    from app.services.session import close_browser, launch_browser

    collectors = {c.code: c for c in get_collectors()}
    targets = [("xianyu", "闲鱼"), ("jd", "京东"), ("pdd", "拼多多")]

    class P:                      # `_search` 只用到 product.model
        model = MODEL

    print("=" * 82)
    print(f"抓取有效性抽检 · 单型号「{MODEL}」· 用生产采集器")
    print("=" * 82)

    close_browser()
    time.sleep(1)
    if not launch_browser():
        print("  ❌ 浏览器启动失败")
        return 1

    from playwright.sync_api import sync_playwright

    rows = []
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp("http://127.0.0.1:9222", timeout=15000)
        ctx = b.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        for code, label in targets:
            src = collectors.get(code)
            print(f"\n── {label} ──")
            if src is None:
                print("   采集器未注册")
                continue

            t0 = time.monotonic()
            err = ""
            try:
                quotes = src._search(page, P())
            except Exception as exc:  # noqa: BLE001
                quotes, err = [], f"{type(exc).__name__}: {str(exc)[:70]}"
            elapsed = time.monotonic() - t0

            try:
                dom = page.evaluate(DOM_JS)
            except Exception as exc:  # noqa: BLE001
                dom = {"title": f"<读取失败 {exc}>", "bodyLen": 0, "links": 0,
                       "imgs": 0, "svgs": 0, "priceLike": 0, "bodyHead": "", "markers": []}

            print(f"   耗时 {elapsed:.1f}s ｜ 提取到 {len(quotes)} 条报价" + (f" ｜ ⚠️ {err}" if err else ""))
            print(f"   DOM：标题={dom['title'][:40]!r}  正文 {dom['bodyLen']} 字符  "
                  f"链接 {dom['links']}  图片 {dom['imgs']}  SVG {dom['svgs']}  "
                  f"价格样式 {dom['priceLike']}")
            if dom["markers"]:
                print(f"   ⚠️ 命中关键词：{dom['markers']}")
            if dom["bodyHead"]:
                print(f"   正文开头：{dom['bodyHead'][:130]}")
            for q in quotes[:3]:
                print(f"     ¥{q.price:<9.0f} {q.title_raw[:56]}")
            rows.append((label, len(quotes), dom, err))

    close_browser()

    print("\n" + "=" * 82)
    print(f"  {'平台':<8}{'提取条数':>8}  DOM 正文  {'链接':>6}{'图片':>6}  关键词")
    for label, n, dom, err in rows:
        print(f"  {label:<8}{n:>8}  {dom['bodyLen']:>8}  {dom['links']:>6}{dom['imgs']:>6}  "
              f"{','.join(dom['markers']) or '无'}")
    print("=" * 82)
    return 0


if __name__ == "__main__":
    sys.exit(main())
