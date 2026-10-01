"""窗口被隐藏时的**计时器保真度**对照实验（零平台请求）。

为什么不能复用 browser_flags_eval.py
------------------------------------
那个脚本测的是**空载内存 + 指纹**，它回答不了本实验的问题。而我们要评估的三个
开关（`--disable-background-timer-throttling` / `--disable-renderer-backgrounding` /
`--disable-backgrounding-occluded-windows`）影响的是**页面被判定为不可见之后，
Chrome 是否降频它的计时器与渲染** —— 收益体现在"时间"上，不在"内存"上。

为什么本项目特别需要它
----------------------
生产环境做了一件**主动让页面变成后台**的事：

  1. `--window-position=-3000,5000`  窗口在屏幕外
  2. `hide_browser_app()` 用 osascript 把整个 App 设为 `visible = false`

Chromium 对"不可见窗口"的标准行为就是节流：后台页的 `setTimeout` 被降到
最低约 1 次/秒，渲染进程被降优先级。若真的生效，SPA 的懒加载、轮询、
React 渲染节奏都会变慢 —— 表现为"单型号耗时偏高"，而且**看起来像网络慢**。

实验设计
--------
不碰任何平台。加载一个本地 HTML，在里面跑两段可计数的时序探针：

  · 20ms 的 `setTimeout` 链跑 4 秒 → 数触发次数（正常约 200 次；
    若被节流到 1s，只剩约 4 次 —— 差异是数量级的，不受噪声影响）
  · `requestAnimationFrame` 跑 4 秒 → 数帧数

同时记录探针的**页面内耗时**与**墙钟耗时** —— 两者背离说明渲染进程被挂起过。

用法：
    python -m scripts.throttle_eval
"""

from __future__ import annotations

import os
import sys
import time

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

from app.services.session import (  # noqa: E402
    close_browser,
    find_browser,
    hide_browser_app,
    launch_browser,
)

# 待评估的三个开关。它们**只改进程/计时模型，页面内不可见** ——
# 与 --disable-gpu 那种"削弱渲染能力"是两回事。
NO_THROTTLE = [
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
]

VARIANTS: list[tuple[str, list[str]]] = [
    ("现状（不加）", []),
    ("只加 timer-throttling", ["--disable-background-timer-throttling"]),
    ("三个都加", NO_THROTTLE),
    ("现状复测（防顺序偏差）", []),
]

# 探针页：两段时序测量，结果挂在 window.__probe（返回 Promise）
PROBE_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<script>
window.__probe = (async () => {
  const out = {};
  const pageStart = performance.now();

  // ① setTimeout(20ms) 链，跑 4 秒
  const gaps = [];
  let last = performance.now();
  const t0 = last;
  await new Promise(res => {
    function step() {
      const now = performance.now();
      gaps.push(now - last); last = now;
      if (now - t0 < 4000) setTimeout(step, 20); else res();
    }
    setTimeout(step, 20);
  });
  out.timerTicks = gaps.length;
  const g = gaps.slice().sort((a, b) => b - a);
  out.timerMaxGap = Math.round(g[0] * 10) / 10;
  out.timerP95Gap = Math.round(g[Math.floor(g.length * 0.05)] * 10) / 10;

  // ② requestAnimationFrame 跑 4 秒
  let frames = 0;
  const fg = [];
  let flast = performance.now();
  const f0 = flast;
  await new Promise(res => {
    function loop() {
      const now = performance.now();
      fg.push(now - flast); flast = now; frames++;
      if (now - f0 < 4000) requestAnimationFrame(loop); else res();
    }
    requestAnimationFrame(loop);
  });
  out.rafFrames = frames;
  out.rafMaxGap = Math.round(fg.slice().sort((a, b) => b - a)[0] * 10) / 10;

  out.pageElapsedMs = Math.round(performance.now() - pageStart);
  out.visibility = document.visibilityState;
  out.hasFocus = document.hasFocus();
  return out;
})();
</script></body></html>"""


def run_probe(html_path: str) -> dict:
    """用 CDP 连上去、加载本地页、等探针 Promise 返回；同时量墙钟耗时。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("file://" + html_path, wait_until="domcontentloaded")
        t0 = time.monotonic()
        result = page.evaluate("window.__probe")
        wall_ms = round((time.monotonic() - t0) * 1000)
        result["wallMs"] = wall_ms
        return result


def main() -> int:
    if not find_browser():
        print("找不到 Chrome")
        return 1

    html = os.path.join("/tmp", "diyprice_throttle_probe.html")
    with open(html, "w", encoding="utf-8") as fh:
        fh.write(PROBE_HTML)

    print("=" * 92)
    print("窗口隐藏状态下的计时器保真度对照（本地页，零平台请求）")
    print("  每个组合：启动 → 停靠窗口（屏幕外）→ 隐藏 App（与生产一致）→ 跑 8 秒探针")
    print("=" * 92)

    rows = []
    for label, extra in VARIANTS:
        close_browser(wait_release=True)
        time.sleep(1.5)

        os.environ["DIYPRICE_EXTRA_LAUNCH_ARGS"] = " ".join(extra)
        try:
            ok = launch_browser(park=True)
        finally:
            os.environ.pop("DIYPRICE_EXTRA_LAUNCH_ARGS", None)
        if not ok:
            print(f"\n[{label}] 启动失败，跳过")
            continue

        # 与生产一致：把整个 App 隐藏（这是会触发 Chromium 后台判定的那一步）
        hidden = hide_browser_app()
        time.sleep(2.5)

        try:
            r = run_probe(html)
        except Exception as exc:  # noqa: BLE001
            print(f"\n[{label}] 探针失败：{type(exc).__name__}: {str(exc)[:70]}")
            continue

        print(f"\n[{label}]  额外参数: {extra or '（无）'}   隐藏App={hidden}")
        print(f"     setTimeout(20ms) 触发 {r['timerTicks']:>4} 次  "
              f"最大间隔 {r['timerMaxGap']:>7.1f}ms  P95 {r['timerP95Gap']:>6.1f}ms")
        print(f"     rAF 帧数 {r['rafFrames']:>4}  最大间隔 {r['rafMaxGap']:>7.1f}ms")
        print(f"     页面内耗时 {r['pageElapsedMs']:>6}ms  墙钟 {r['wallMs']:>6}ms  "
              f"visibility={r['visibility']} focused={r['hasFocus']}")
        rows.append((label, r))

    close_browser(wait_release=True)

    print("\n" + "=" * 92)
    print("汇总")
    print("=" * 92)
    print(f"  {'组合':<24}{'定时器触发':>10}{'最大间隔ms':>12}{'rAF帧':>8}{'墙钟ms':>9}  判定")
    base = rows[0][1] if rows else None
    for label, r in rows:
        if base and r["timerTicks"] >= 100:
            verdict = "✅ 未节流（触发次数正常）"
        elif base and r["timerTicks"] < 30:
            verdict = "❌ 被节流（20ms 链退化成约 1 次/秒）"
        else:
            verdict = "⚠️ 部分节流"
        print(f"  {label:<24}{r['timerTicks']:>10}{r['timerMaxGap']:>12.1f}"
              f"{r['rafFrames']:>8}{r['wallMs']:>9}  {verdict}")

    if base:
        print()
        print(f"  ⚠️ 判定说明：4 秒 / 20ms ≈ 200 次为正常；若被降到最低档（1 次/秒）")
        print(f"     只剩约 4 次。这个差异是数量级的，不受 RSS 那种 ±25% 噪声影响。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
