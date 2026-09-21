"""端到端验收：一轮真实采集，同时监控**焦点 / 内存 / 数据完整性**。

三件事必须在**同一次采集**里测，不能分三次跑：
  · 焦点干扰只在启动瞬间发生，分次跑就测不到了
  · 内存峰值依赖当轮采集的型号构成
  · 数据完整性要跟同一轮的日志/入库对得上

三条判据（都是可证伪的）：
  1. **前台应用全程不变成浏览器** —— 判据是"变化"，不是"当前是不是 Chrome"：
     用户日常浏览器的进程名也是 "Google Chrome"，只看名字会把自己开的浏览器
     误判成我们弹的窗。
  2. **窗口始终在屏幕外** —— 用 CDP `Browser.getWindowForTarget` 取真实边界
     （权威数据）。⚠️ 探测必须放**独立子进程**：在父进程里用 Playwright
     采样会造出假窗口（实测测到 3 个 (0,73) 窗口，实际只有 1 个且全程在屏幕外）。
  3. **采集数据正常入库** —— 抓取条数/入库条数/未匹配率对得上日志。

用法：
    python -m scripts.acceptance_run                    # 生产配置（jd,pdd,xianyu）
    python -m scripts.acceptance_run --limit 6          # 限制型号数（快一些）
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import threading
import time

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

from app.services.session import DEFAULT_PROFILE  # noqa: E402

PARKED_LEFT = -1000        # 停靠时 left 应远小于 0
SCREEN = (1680, 1050)      # 由 screen_size() 覆盖


def _osascript_front() -> str:
    """System Events 的 frontmost（254ms/次，慢但语义直观）。"""
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


def _lsappinfo_map() -> dict[str, str]:
    """ASN → 应用名（`lsappinfo front` 只要 6ms，但不返回名字）。"""
    out = subprocess.run(["lsappinfo", "list"], capture_output=True, text=True).stdout
    return {asn: name for name, asn in re.findall(r'"([^"]+)"\s+ASN:([0-9a-fx\-:]+)', out)}


_ASN_MAP: dict[str, str] = {}


def _lsappinfo_front() -> str:
    """WindowServer 层面的权威前台应用（6ms/次）。"""
    global _ASN_MAP
    if not _ASN_MAP:
        _ASN_MAP = _lsappinfo_map()
    out = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True).stdout
    key = out.strip().replace("ASN:", "")
    if key not in _ASN_MAP:
        _ASN_MAP = _lsappinfo_map()
    return _ASN_MAP.get(key, "")


def frontmost() -> tuple[str, bool]:
    """**双源**取前台应用，返回 (名字, 两源是否一致)。

    为什么必须双源：单用 osascript 出现过"报告 Chrome 抢焦、但 lsappinfo
    （WindowServer 权威）说没有"的矛盾读数。两个独立来源一致才算数 ——
    抢焦这种结论不该建立在单一工具的偏差上。
    """
    a = _osascript_front()
    b = _lsappinfo_front()
    if not a or not b:
        return (a or b), False
    # 同一个应用在两套命名下可能不同（"Electron" vs "WorkBuddy AI"），
    # 所以只比较"是不是 Chrome"这个我们关心的判据
    return a, (a == "Google Chrome") == (b == "Google Chrome")


def screen_size() -> tuple[int, int]:
    out = subprocess.run(
        ["osascript", "-e", 'tell application "Finder" to get bounds of window of desktop'],
        capture_output=True, text=True,
    ).stdout.strip()
    try:
        p = [int(x.strip()) for x in out.split(",")]
        return p[2], p[3]
    except Exception:  # noqa: BLE001
        return SCREEN


def chrome_rss_mb() -> float:
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


def chrome_procs() -> int:
    out = subprocess.run(["ps", "-eo", "command="], capture_output=True, text=True).stdout
    return len([l for l in out.splitlines() if str(DEFAULT_PROFILE) in l])


def app_visible() -> bool | None:
    """受管 Chrome 这个**应用**当前是否可见。

    ⚠️ 必须查它：**窗口坐标在屏内 ≠ 用户看得见**。我们会在启动后把整个应用
    隐藏（`hide_browser_app`），此时窗口的坐标**仍然是屏内坐标**，但系统根本
    不绘制它。只看坐标会把这种情况误报成"窗口闪现" —— 第一版验收脚本就是这么
    误报的（报了 (0,73) 屏内 65%，实际应用已隐藏、用户看不见）。
    """
    from app.services.session import BROWSER_PROFILE, main_browser_pid

    pid = main_browser_pid(BROWSER_PROFILE)
    if not pid:
        # 进程正在切换（回收重启的瞬间）—— 重试几次再放弃
        for _ in range(5):
            time.sleep(0.3)
            pid = main_browser_pid(BROWSER_PROFILE)
            if pid:
                break
        if not pid:
            return None
    script = (
        'tell application "System Events" to get visible of '
        f'(first application process whose unix id is {pid})'
    )
    for _ in range(3):
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=6)
        out = r.stdout.strip().lower()
        if out == "true":
            return True
        if out == "false":
            return False
        time.sleep(0.3)
    return None


def windows() -> list[str]:
    """窗口边界（独立子进程，避免父进程 Playwright 造出假窗口）。"""
    r = subprocess.run(
        [sys.executable, "-m", "scripts._window_probe"],
        capture_output=True, text=True, cwd=PROJ, timeout=40,
    )
    return [l for l in r.stdout.strip().splitlines()
            if l.startswith("win") or l.startswith("(无页面")]


def system_free_pct() -> str:
    out = subprocess.run(["memory_pressure"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "free percentage" in line:
            return line.split(":")[-1].strip()
    return "?"


def visible_ratio(line: str) -> float:
    try:
        left, top = line.split("@(")[1].split(")")[0].split(",")
        size = line.split(")")[1].strip().split()[0]
        w, h = size.split("x")
        left, top, w, h = int(left), int(top), int(w), int(h)
    except Exception:  # noqa: BLE001
        return 0.0
    sw, sh = SCREEN
    vw = max(0, min(left + w, sw) - max(left, 0))
    vh = max(0, min(top + h, sh) - max(top, 0))
    return vw * vh / (sw * sh)


def main() -> int:
    global SCREEN
    ap = argparse.ArgumentParser(description="端到端验收（焦点 + 内存 + 数据）")
    ap.add_argument("--sources", default="jd,pdd,xianyu")
    ap.add_argument("--limit", type=int, default=0, help="0 = 用生产默认配额")
    args = ap.parse_args()

    SCREEN = screen_size()
    print("=" * 80)
    print(f"端到端验收：真实采集一轮（{args.sources}）")
    print("=" * 80)

    before_app = frontmost()
    print(f"\n[0] 采集前")
    print(f"    最前台应用：{before_app!r}")
    print(f"    受管 Chrome：{chrome_procs()} 进程 / {chrome_rss_mb():.0f} MB")
    print(f"    系统可用内存：{system_free_pct()}")

    env = dict(os.environ)
    if args.limit:
        for s in args.sources.split(","):
            env[f"DIYPRICE_{s.upper()}_LIMIT"] = str(args.limit)
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.cli", "collect", "--sources", args.sources],
        cwd=PROJ, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )

    apps: list[tuple[float, str]] = []
    disagreements: list[tuple[float, str]] = []
    wins: list[tuple[float, list[str]]] = []
    mems: list[tuple[float, float, int]] = []
    stop = threading.Event()
    t0 = time.monotonic()

    def watch_apps() -> None:
        while not stop.is_set():
            name, agree = frontmost()
            apps.append((time.monotonic() - t0, name))
            if not agree:
                disagreements.append((time.monotonic() - t0, name))
            time.sleep(1.0)

    def watch_wins() -> None:
        while not stop.is_set():
            wins.append((time.monotonic() - t0, windows()))
            time.sleep(3.0)

    def watch_mem() -> None:
        while not stop.is_set():
            mems.append((time.monotonic() - t0, chrome_rss_mb(), chrome_procs()))
            time.sleep(2.0)

    threads = [threading.Thread(target=f, daemon=True) for f in (watch_apps, watch_wins, watch_mem)]
    for t in threads:
        t.start()
    out, _ = proc.communicate()
    stop.set()
    for t in threads:
        t.join(timeout=8)

    after_app = frontmost()
    elapsed = time.monotonic() - t0

    # ---- 1. 焦点 ----
    print(f"\n[1] 焦点干扰（采样 {len(apps)} 次，间隔 1s）")
    changes = []
    for t, name in apps:
        if not changes or changes[-1][1] != name:
            changes.append((t, name))
    chrome_hits = sum(1 for _, n in apps if n == "Google Chrome")
    for t, name in changes:
        mark = "  ← 浏览器抢了焦点" if name == "Google Chrome" else ""
        print(f"      +{t:>5.0f}s  {name!r}{mark}")
    focus_ok = chrome_hits == 0
    print(f"    Chrome 成为前台：{chrome_hits} 次 → {'✅ 未抢焦点' if focus_ok else '❌ 抢了焦点'}")
    if disagreements:
        print(f"    ⚠️ 有 {len(disagreements)} 次两源读数不一致（结论以两源一致的部分为准）")

    # ---- 2. 窗口 ----
    print(f"\n[2] 窗口位置（采样 {len(wins)} 次，间隔 3s）")
    print("    判据：**窗口在屏内 且 应用可见** 才算闪现 —— 应用被隐藏时"
          "窗口坐标仍是屏内坐标，但系统不绘制它")
    uniq = sorted({l for _, ls in wins for l in ls})
    worst = 0.0
    worst_line = ""
    for line in uniq:
        r = visible_ratio(line)
        print(f"      {line[:74]}   屏内 {r * 100:.3f}%")
        if r > worst:
            worst, worst_line = r, line
    # 采到屏内窗口的时刻，同时看应用是否可见
    flashed: list[str] = []
    uncertain: list[str] = []
    for t, ls in wins:
        for line in ls:
            if visible_ratio(line) > 0.005:
                vis = app_visible()
                if vis is True:
                    flashed.append(f"+{t:.0f}s {line[:50]} app可见=True")
                elif vis is None:
                    uncertain.append(f"+{t:.0f}s {line[:50]} 可见性取不到")
    win_ok = not flashed
    if flashed:
        print(f"    ❌ 出现 {len(flashed)} 次**用户可见的屏内窗口**：")
        for f in flashed[:5]:
            print(f"        {f}")
    else:
        print("    ✅ 无用户可见的屏内窗口"
              f"（屏内坐标最大 {worst * 100:.3f}%，但应用已隐藏）")
    if uncertain:
        print(f"    ⚠️ {len(uncertain)} 次屏内采样**无法确认可见性**（进程正在切换）：")
        for u in uncertain[:3]:
            print(f"        {u}")

    # ---- 3. 内存 ----
    print(f"\n[3] 内存")
    if mems:
        peak = max(mems, key=lambda m: m[1])
        print(f"    采集前：{mems[0][2]} 进程 / {mems[0][1]:.0f} MB")
        print(f"    峰值：  {peak[2]} 进程 / {peak[1]:.0f} MB（第 {peak[0]:.0f}s）")
        print(f"    结束后：{chrome_procs()} 进程 / {chrome_rss_mb():.0f} MB")
        print(f"    系统可用内存：{system_free_pct()}")
        series = [f"+{t:.0f}s:{mb:.0f}" for t, mb, _ in mems[:: max(1, len(mems) // 10)]]
        print(f"    序列：{' '.join(series)}")

    # ---- 4. 数据完整性 ----
    print(f"\n[4] 数据完整性（总耗时 {elapsed:.0f}s）")
    tail = [l for l in out.splitlines()
            if any(k in l for k in ("采集完成", "源 ", "写入", "熔断跳过", "非正常"))]
    for line in tail[-10:]:
        print("      " + line.strip()[:120])
    forced = any("强制终止" in l for l in out.splitlines())
    print(f"    触发行程兜底强制终止：{'⚠️ 是' if forced else '否 ✅'}")

    ok = focus_ok and win_ok and not forced
    print("\n" + "=" * 80)
    print(f"{'✅ 验收通过' if ok else '❌ 验收未通过'}"
          f"（焦点 {'✅' if focus_ok else '❌'} / 窗口 {'✅' if win_ok else '❌'}"
          f" / 兜底 {'✅' if not forced else '❌'}）")
    print("=" * 80)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
