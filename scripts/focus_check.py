"""焦点/弹窗干扰验证 —— 回答"后台采集时会不会打断你打字"。

判定标准（两条都要过）：
  1. **前台应用全程不变**：采集期间不断采样系统最前台的应用，如果它变成
     浏览器，就说明窗口抢了焦点。
     ⚠️ 不能只看"最前台是不是 Chrome" —— 你日常浏览器的进程名也是
     "Google Chrome"，会把你自己开着的浏览器误判成我们弹的窗。
     所以判据是**变化**：采集前后最前台应用应当完全一致。
  2. **调试窗口始终在屏幕外**：用 CDP `Browser.getWindowForTarget` 取窗口
     真实边界（权威数据），计算它在屏幕内露出的面积占比。
     目标：≤0.5%（macOS 会把窗口位置夹住，保证留一小条在屏内，无法为 0）。

用法：
    python -m scripts.focus_check                 # 跑一轮真实采集并全程监控
    python -m scripts.focus_check --limit 4
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)


def frontmost_app() -> str:
    """当前最前台的应用名（失败返回空串，不抛）。"""
    try:
        out = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first application '
             'process whose frontmost is true'],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def screen_size() -> tuple[int, int]:
    out = subprocess.run(
        ["osascript", "-e", 'tell application "Finder" to get bounds of window of desktop'],
        capture_output=True, text=True,
    ).stdout.strip()
    try:
        parts = [int(x.strip()) for x in out.split(",")]
        return parts[2], parts[3]
    except Exception:  # noqa: BLE001
        return 1680, 1050


def all_window_bounds() -> list[dict]:
    """**枚举所有窗口**的边界（CDP 权威数据）。

    ⚠️ 只取 `ctx.pages[0]` 会漏 —— 采集时 `new_page()` 可能新建的是**另一个
    窗口**（Chrome 以 `--no-startup-window` 启动时就会这样，而 `--window-position`
    对"后创建的窗口"不生效，于是它落在 macOS 默认层叠位置 (0,73)）。
    只测 pages[0] 会得出"窗口在屏幕外"的错误结论。

    必须走 CDP：`osascript` 的 `process "Google Chrome"` 会把**用户日常
    浏览器**的窗口一起算进来，之前因此误判过"窗口弹到屏幕内"。
    """
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
            ctx = browser.contexts[0]
            windows: dict[int, dict] = {}
            for page in ctx.pages:
                try:
                    session = ctx.new_cdp_session(page)
                    info = session.send("Browser.getWindowForTarget")
                    session.detach()
                except Exception:  # noqa: BLE001
                    continue
                wid = info.get("windowId")
                if wid is not None and wid not in windows:
                    windows[wid] = dict(info.get("bounds", {}))
            return [{"windowId": wid, **b} for wid, b in windows.items()]
    except Exception:  # noqa: BLE001
        return []


def window_bounds() -> dict | None:
    """兼容旧调用：返回**屏内露出最多**的那个窗口。"""
    wins = all_window_bounds()
    if not wins:
        return None
    return max(wins, key=lambda b: (b.get("width") or 0) * (b.get("height") or 0))


def visible_ratio(bounds: dict, screen: tuple[int, int]) -> float | None:
    """窗口在屏幕内露出的面积占比。"""
    if not bounds or bounds.get("left") is None:
        return None
    sw, sh = screen
    left, top = bounds["left"], bounds["top"]
    right, bottom = left + bounds["width"], top + bounds["height"]
    vis_w = max(0, min(right, sw) - max(left, 0))
    vis_h = max(0, min(bottom, sh) - max(top, 0))
    return vis_w * vis_h / (sw * sh)


def main() -> int:
    ap = argparse.ArgumentParser(description="采集期间的焦点/弹窗验证")
    ap.add_argument("--source", default="xianyu")
    ap.add_argument("--limit", type=int, default=4)
    ap.add_argument("--interval", type=float, default=1.0)
    args = ap.parse_args()

    screen = screen_size()
    print("=" * 74)
    print(f"焦点/弹窗验证（屏幕 {screen[0]}×{screen[1]}）")
    print("=" * 74)

    before = frontmost_app()
    print(f"\n采集前最前台应用：{before!r}")
    if not before:
        print("  ⚠️ 取不到前台应用名，本次只能验证窗口位置")

    env = dict(os.environ)
    env[f"DIYPRICE_{args.source.upper()}_LIMIT"] = str(args.limit)
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.cli", "collect", "--sources", args.source],
        cwd=PROJ, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )

    apps: list[tuple[float, str]] = []
    windows: list[tuple[float, list[dict]]] = []
    stop = threading.Event()
    t0 = time.monotonic()

    def watch_apps() -> None:
        while not stop.is_set():
            apps.append((time.monotonic() - t0, frontmost_app()))
            time.sleep(args.interval)

    def watch_windows() -> None:
        while not stop.is_set():
            windows.append((time.monotonic() - t0, all_window_bounds()))
            time.sleep(2.5)

    ta = threading.Thread(target=watch_apps, daemon=True)
    tw = threading.Thread(target=watch_windows, daemon=True)
    ta.start()
    tw.start()
    out, _ = proc.communicate()
    stop.set()
    ta.join(timeout=6)
    tw.join(timeout=6)

    after = frontmost_app()
    print(f"采集后最前台应用：{after!r}")

    print(f"\n[1] 前台应用采样 {len(apps)} 次")
    distinct = []
    for t, name in apps:
        if not distinct or distinct[-1][1] != name:
            distinct.append((t, name))
    if len(distinct) <= 1:
        print(f"    全程只有 {distinct[0][1]!r}，**没有任何切换** ✅")
    else:
        print(f"    发生 {len(distinct) - 1} 次切换：")
        for t, name in distinct:
            print(f"      +{t:>5.0f}s  {name!r}")

    print(f"\n[2] 调试窗口采样 {len(windows)} 次（每轮枚举**所有**窗口）")
    worst_ratio = 0.0
    worst_at = 0.0
    worst_win: dict = {}
    seen_windows: dict[int, dict] = {}
    for t, wins in windows:
        for w in wins:
            seen_windows.setdefault(w.get("windowId", -1), w)
            r = visible_ratio(w, screen)
            if r is not None and r > worst_ratio:
                worst_ratio, worst_at, worst_win = r, t, w
    print(f"    出现过的窗口共 {len(seen_windows)} 个：")
    for wid, w in seen_windows.items():
        print(f"      windowId={wid}  {w.get('left')},{w.get('top')} "
              f"{w.get('width')}×{w.get('height')}  屏内 {visible_ratio(w, screen) or 0:.3%}")
    if worst_win:
        print(f"    最差时刻：+{worst_at:.0f}s  屏内露出 {worst_ratio * 100:.3f}%  {worst_win}")
    ok_win = worst_ratio <= 0.005
    print(f"    ≤0.5% 判定：{'✅ 通过' if ok_win else '❌ 未通过'}")

    print(f"\n[3] 采集输出尾部")
    for line in out.strip().splitlines()[-4:]:
        print("   ", line[:130])

    changed = len(distinct) > 1
    print("\n" + "=" * 74)
    if not before:
        print("⚠️ 取不到前台应用基准，本次只能验证窗口位置")
    elif changed:
        print(f"❌ 前台应用发生过 {len(distinct) - 1} 次切换 —— 有窗口抢了焦点")
    else:
        print("✅ 前台应用全程未变 —— 采集没有抢占焦点")
    print(f"{'✅' if ok_win else '❌'} 调试窗口屏内露出最大 {worst_ratio * 100:.3f}%"
          f"（阈值 0.5%）")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
