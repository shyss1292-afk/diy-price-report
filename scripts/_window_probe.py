"""窗口探测（子进程用）—— 打印当前所有调试浏览器窗口的边界。

单独成文件是因为 Playwright 的同步 API **不能在已持有事件循环的进程里嵌套**：
worker 自己就在用 sync_playwright，主进程里再起一个会直接报
"It looks like you are using Playwright Sync API inside the asyncio loop"。
所以窗口探测必须放到独立进程（见 scripts/focus_check.py 与临时诊断）。
"""
from __future__ import annotations

import sys


def probe() -> list[str]:
    from playwright.sync_api import sync_playwright

    out: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0]
        seen: set[int] = set()
        for page in ctx.pages:
            try:
                session = ctx.new_cdp_session(page)
                info = session.send("Browser.getWindowForTarget")
                session.detach()
            except Exception:  # noqa: BLE001
                continue
            wid = info.get("windowId")
            if wid in seen:
                continue
            seen.add(wid)
            b = info.get("bounds", {})
            url = (getattr(page, "url", "") or "")[:30]
            out.append(
                f"win{wid} @({b.get('left')},{b.get('top')}) "
                f"{b.get('width')}x{b.get('height')} [{b.get('windowState')}] {url}"
            )
    return out or ["(无页面)"]


if __name__ == "__main__":
    try:
        for line in probe():
            print(line)
    except Exception as exc:  # noqa: BLE001
        print(f"err {str(exc)[:70]}")
        sys.exit(1)
