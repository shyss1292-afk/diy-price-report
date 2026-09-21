"""无干扰版焦点验证 —— 采集与探测**完全分离到两个进程**。

为什么需要这个版本：`scripts/focus_check.py` 在父进程里用 Playwright 采样窗口，
而父进程同时还在跑采集子进程。Playwright 的 CDP 连接、线程内调用等都可能
引入副作用，导致"测到的现象"其实来自测量工具本身。

这个脚本把探测彻底外置：
    · 进程 A：跑真实采集
    · 进程 B：只做 osascript 取前台应用（零 CDP、零 Playwright）
    · 进程 C：只做 CDP 窗口探测（独立进程，每次重新连接）

三者互不干扰。判定：
    1) 前台应用是否出现过 "Google Chrome"（出现即说明抢了焦点）
    2) 是否出现过非停靠位置的窗口（停靠位置 = 负坐标）
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

PARKED_LEFT = -1000          # 停靠时 left 应该是很大的负数


def front() -> str:
    out = subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to get name of first application '
         'process whose frontmost is true'],
        capture_output=True, text=True, timeout=5,
    )
    return out.stdout.strip()


def app_visible() -> bool | None:
    """受管 Chrome 这个**应用**当前是否可见。

    为什么必须查它：窗口坐标在屏内 ≠ 用户看得见。我们会在启动后把整个应用
    隐藏（`hide_browser_app`），此时窗口的坐标**仍然是屏内坐标**，但系统
    根本不绘制它。只看坐标会把这种情况误报成"窗口闪现"。
    """
    from app.services.session import BROWSER_PROFILE, main_browser_pid

    pid = main_browser_pid(BROWSER_PROFILE)
    if not pid:
        return None
    script = (
        'tell application "System Events" to get visible of '
        f'(first application process whose unix id is {pid})'
    )
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=6)
    out = r.stdout.strip().lower()
    if out == "true":
        return True
    if out == "false":
        return False
    return None


def windows() -> list[str]:
    r = subprocess.run(
        [sys.executable, "-m", "scripts._window_probe"],
        capture_output=True, text=True, cwd=PROJ, timeout=30,
    )
    return [l for l in r.stdout.strip().splitlines()
            if l.startswith("win") or l.startswith("(无页面")]


def main() -> int:
    limit = sys.argv[1] if len(sys.argv) > 1 else "4"
    print("=" * 76)
    print("无干扰版焦点验证（采集 / 前台探测 / 窗口探测 三者分进程）")
    print("=" * 76)

    baseline = front()
    print(f"\n采集前最前台应用：{baseline!r}")

    env = dict(os.environ)
    env["DIYPRICE_XIANYU_LIMIT"] = limit
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.cli", "collect", "--sources", "xianyu"],
        cwd=PROJ, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )

    t0 = time.monotonic()
    app_log: list[tuple[float, str]] = []
    win_log: list[tuple[float, list[str]]] = []
    chrome_seen = 0
    off_park: list[tuple[float, str]] = []

    while proc.poll() is None:
        t = time.monotonic() - t0
        app = front()
        app_log.append((t, app))
        if app == "Google Chrome":
            chrome_seen += 1
        for line in windows():
            win_log.append((t, [line]))
            try:
                left = int(line.split("@(")[1].split(",")[0])
            except Exception:  # noqa: BLE001
                continue
            vis = app_visible()
            if left > PARKED_LEFT and vis is not False:
                off_park.append((t, f"{line}   app可见={vis}"))
        time.sleep(1)

    out, _ = proc.communicate()
    print(f"\n[1] 前台应用采样 {len(app_log)} 次，其中 'Google Chrome' 出现 {chrome_seen} 次")
    changes = []
    for t, name in app_log:
        if not changes or changes[-1][1] != name:
            changes.append((t, name))
    for t, name in changes:
        mark = "  ← 浏览器抢了焦点" if name == "Google Chrome" else ""
        print(f"      +{t:>5.0f}s  {name!r}{mark}")

    print(f"\n[2] 窗口采样 {len(win_log)} 次")
    uniq = sorted({l for _, ls in win_log for l in ls})
    for line in uniq:
        print(f"      {line}")
    if off_park:
        print(f"\n    ❌ 出现 {len(off_park)} 次**用户可见的屏内窗口**：")
        for t, line in off_park[:6]:
            print(f"        +{t:.0f}s  {line}")
    else:
        print("\n    ✅ 未出现用户可见的屏内窗口（屏内坐标 + 应用隐藏不算）")

    print("\n[3] 采集输出尾部")
    for line in out.strip().splitlines()[-3:]:
        print("   ", line[:130])

    print("\n" + "=" * 76)
    ok = chrome_seen == 0 and not off_park
    if ok:
        print("✅ 通过：全程未抢焦点、无屏内窗口")
    else:
        if chrome_seen:
            print(f"❌ 浏览器在采集期间成为前台应用 {chrome_seen} 次 —— 会打断打字")
        if off_park:
            print(f"❌ 出现过 {len(off_park)} 次屏内窗口")
    print("=" * 76)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
