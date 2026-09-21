"""风控/内存对照实验：headless 能不能用、拦图片省多少。

这个脚本存在的唯一目的，是让"要不要开 headless""要不要拦图片"这两个
决策建立在**当场跑出来的数据**上，而不是历史结论或直觉。

三个变体各做一次真实的闲鱼搜索，报告：
  · 是否被风控拦（命中限流特征 / 页面异常）
  · 抓到多少商品卡片（拦了就一定是 0）
  · 峰值内存

⚠️ 会真的发平台请求（每个变体 1 次）。默认只测闲鱼 —— 它是当前唯一
   健康的源；京东/拼多多在冷却期内，拿它们做对照得不出结论。

用法：
    python -m scripts.stealth_eval
    python -m scripts.stealth_eval --model "RTX 5070 12G"
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

from app.services.session import close_browser, launch_browser  # noqa: E402

SEARCH_URL = "https://www.goofish.com/search?q={kw}"

# (标签, 追加启动参数, 是否拦图片)
#
# ⚠️ 末尾必须有**基线复测**：三个变体是连续三次相同的搜索，平台可能因为
#    "短时间重复搜索"而软限流，导致后跑的那个看起来"被搞坏了"。
#    没有复测就分不清"是拦图片的锅"还是"是第 3 次请求的锅" ——
#    这个坑实测踩过（拦图片那轮卡片数 33→3，差点被当成拦图片的结论）。
VARIANTS: list[tuple[str, str, bool]] = [
    ("① 现状（离屏有头）", "", False),
    ("② --headless=new", "--headless=new", False),
    ("③ 现状 + 拦图片", "", True),
    ("④ 现状复测（对照）", "", False),
    ("⑤ 关 site isolation", "--disable-features=IsolateOrigins,site-per-process", False),
    ("⑥ 限 V8 堆 256MB", "--js-flags=--max-old-space-size=256", False),
    ("⑦ 现状再复测（对照）", "", False),
]

_BLOCK_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")


def chrome_rss_mb() -> float:
    from app.services.session import DEFAULT_PROFILE

    out = subprocess.run(["ps", "-eo", "rss=,command="], capture_output=True, text=True).stdout
    total = 0
    for line in out.splitlines():
        if str(DEFAULT_PROFILE) not in line:
            continue
        try:
            total += int(line.split(None, 1)[0])
        except (ValueError, IndexError):
            continue
    return total / 1024.0


def run_variant(label: str, extra: str, block_images: bool, model: str) -> dict:
    from playwright.sync_api import sync_playwright

    close_browser()
    time.sleep(1)
    if extra:
        os.environ["DIYPRICE_EXTRA_LAUNCH_ARGS"] = extra
    try:
        ok = launch_browser()
    finally:
        os.environ.pop("DIYPRICE_EXTRA_LAUNCH_ARGS", None)

    result = {"label": label, "launched": ok, "cards": 0, "blocked": None,
              "title": "", "peak_mb": 0.0, "error": ""}
    if not ok:
        result["error"] = "浏览器启动失败"
        return result

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            blocked_count = 0
            if block_images:
                def _handler(route):
                    nonlocal blocked_count
                    url = (route.request.url or "").lower()
                    if any(url.endswith(e) or e + "?" in url for e in _BLOCK_EXT):
                        blocked_count += 1
                        route.abort()
                    else:
                        route.continue_()

                ctx.route("**/*", _handler)

            page.goto(SEARCH_URL.format(kw=model.replace(" ", "%20")),
                      wait_until="domcontentloaded", timeout=40000)
            time.sleep(6)
            result["peak_mb"] = chrome_rss_mb()
            result["title"] = (page.title() or "")[:60]
            body = (page.inner_text("body") or "")[:4000]
            result["cards"] = page.locator("a[href*='item']").count()

            # 风控特征（与 collectors/policy.py 的判据保持一致的关键词）
            for marker in ("验证", "滑动", "非法访问", "punish", "访问频繁", "系统繁忙"):
                if marker in body:
                    result["blocked"] = marker
                    break
            result["images_blocked"] = blocked_count
            try:
                ctx.unroute("**/*", _handler)
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)[:110]

    result["final_mb"] = chrome_rss_mb()
    close_browser()
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="headless / 拦图片 对照实验")
    ap.add_argument("--model", default="RTX 5070 12G")
    args = ap.parse_args()

    print("=" * 78)
    print(f"对照实验（闲鱼搜索「{args.model}」，每变体 1 次真实请求）")
    print("=" * 78)

    rows = [run_variant(label, extra, blk, args.model)
            for label, extra, blk in VARIANTS]

    print(f"\n{'变体':<22}{'启动':>5}{'卡片':>6}{'拦图':>6}{'峰值MB':>9}  风控/错误")
    for r in rows:
        print(f"  {r['label']:<20}{'✅' if r['launched'] else '❌':>4}"
              f"{r['cards']:>6}{r.get('images_blocked', 0):>6}"
              f"{r['peak_mb']:>9.0f}  {r['blocked'] or r['error'] or '无'}")
        if r["title"]:
            print(f"      页面标题：{r['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
