"""抢焦**阻断**方案对照实验 —— 目标是"根本不抢"，而不是"抢了再还"。

用户反馈是对的：轮询检测 + 切回原应用是**被动挽救**，毫秒级时间差照样吞键。
这个脚本对比四种"阻断型"方案，看哪个能让 Chrome **从不成为前台应用**。

判据只有一个：启动后 15 秒内，最前台应用出现 "Google Chrome" 的次数。
  · 0 次 = 真正阻断 ✅
  · >0 次 = 仍会抢（哪怕随后还回去）

四个变体：
  A 现状            Popen + 隐藏 + 归还（被动挽救，作为基线）
  B open -g         macOS 原生后台打开（用户建议的方案 A）
  C no-startup-window  启动时不建窗口 —— 没有窗口，系统可能就不激活它
  D 先隐藏再建窗口   启动后立刻隐藏应用，**再**通过 CDP 建页

用法：
    python -m scripts.focus_prevent_eval
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

from app.services.session import (  # noqa: E402
    BROWSER_PROFILE,
    build_launch_args,
    close_browser,
    find_browser,
    hide_browser_app,
    launch_browser,
    main_browser_pid,
    restore_front_app,
)


def frontmost() -> str:
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


def wait_pid(timeout: float = 8.0) -> int | None:
    end = time.time() + timeout
    while time.time() < end:
        pid = main_browser_pid(BROWSER_PROFILE)
        if pid:
            return pid
        time.sleep(0.15)
    return None


def observe(seconds: float = 15.0, on_tick=None) -> tuple[int, list[str]]:
    """采样最前台应用，返回 (Chrome 出现次数, 切换序列)。"""
    hits = 0
    seq: list[str] = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        name = frontmost()
        if not seq or seq[-1] != name:
            seq.append(name)
        if name == "Google Chrome":
            hits += 1
        if on_tick:
            on_tick(time.monotonic() - t0)
        time.sleep(0.5)
    return hits, seq


# ------------------------------------------------------------------ 变体

def variant_a(prev: str) -> None:
    """现状：Popen → 隐藏 → 归还（被动挽救）。"""
    subprocess.Popen(build_launch_args(find_browser(), BROWSER_PROFILE, 9222),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pid = wait_pid()
    if pid:
        hide_browser_app(BROWSER_PROFILE, pid=pid)
        restore_front_app(prev)


def variant_b(prev: str) -> None:
    """open -g：macOS 原生后台打开。"""
    args = build_launch_args(find_browser(), BROWSER_PROFILE, 9222)
    subprocess.run(["open", "-g", "-n", "-a", args[0], "--args", *args[1:]],
                   capture_output=True, text=True)
    wait_pid()


def variant_c(prev: str) -> None:
    """--no-startup-window：启动时不建窗口。"""
    args = build_launch_args(find_browser(), BROWSER_PROFILE, 9222)
    # about:blank 位置参数会强制建窗口，这里去掉
    args = [a for a in args if a != "about:blank"]
    args.append("--no-startup-window")
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_pid()


def variant_d(prev: str) -> None:
    """先隐藏应用，**再**通过 CDP 建页。"""
    subprocess.Popen(build_launch_args(find_browser(), BROWSER_PROFILE, 9222),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pid = wait_pid()
    if pid:
        # 反复隐藏，确保"建窗口之前"应用已是隐藏态
        for _ in range(12):
            hide_browser_app(BROWSER_PROFILE, pid=pid)
            time.sleep(0.2)
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp("http://127.0.0.1:9222", timeout=8000)
            ctx = browser.contexts[0] if browser.contexts else None
            if ctx is not None and not ctx.pages:
                ctx.new_page()
            time.sleep(2)
    except Exception:  # noqa: BLE001
        pass
    if pid:
        hide_browser_app(BROWSER_PROFILE, pid=pid)


VARIANTS = [
    ("A 现状（隐藏+归还，被动挽救）", variant_a),
    ("B open -g（macOS 后台打开）", variant_b),
    ("C --no-startup-window", variant_c),
    ("D 先隐藏再建窗口", variant_d),
]


def asn_map() -> dict[str, str]:
    """ASN → 应用名。从 `lsappinfo list` 解析。

    为什么要这个：`lsappinfo front` 只要 **6ms**（osascript 要 254ms，慢 42 倍），
    但它只返回 ASN 不返回名字；而 `lsappinfo info <asn>` 在沙箱里查不到。
    所以自己建映射表 —— 这样采样间隔能压到约 10ms，
    足以测出"抢焦窗口"的真实毫秒数。

    ⚠️ 用正则整表提取，不要逐行状态机 —— 逐行写法实测漏了 3 条，
       连前台应用本身都查不到（返回 `<0x0-e00e:>` 占位符）。
       格式：` 3) "XAppTool" ASN:0x0-8008:`
    """
    out = subprocess.run(["lsappinfo", "list"], capture_output=True, text=True).stdout
    return {
        asn: name
        for name, asn in re.findall(r'"([^"]+)"\s+ASN:([0-9a-fx\-:]+)', out)
    }


class FastFrontmost:
    """高频采样最前台应用名（约 10ms 一次）。

    先用 osascript 拿不到这个分辨率（254ms/次），实测会把 1.5 秒的窗口
    采成 7 个点，误差极大。这里改用 `lsappinfo front` + 自建 ASN 映射。
    """

    def __init__(self) -> None:
        self._map = asn_map()
        self._last_refresh = time.monotonic()

    def refresh(self) -> None:
        self._map = asn_map()
        self._last_refresh = time.monotonic()

    def current(self) -> str:
        if time.monotonic() - self._last_refresh > 3.0:
            self.refresh()
        out = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True).stdout
        asn = out.strip().replace("ASN:", "")
        if asn not in self._map:
            self.refresh()
        return self._map.get(asn, f"<{asn or '未知'}>")


def measure_focus_window(rounds: int = 3) -> None:
    """精确测量"抢焦窗口"的毫秒数。"""
    print("=" * 84)
    print(f"精确测量抢焦窗口（{rounds} 次启动，~10ms 采样）")
    print("=" * 84)

    for i in range(1, rounds + 1):
        close_browser()
        time.sleep(2)
        ff = FastFrontmost()
        prev = ff.current()
        t0 = time.monotonic()
        launch_browser()

        samples: list[tuple[float, str]] = []
        while time.monotonic() - t0 < 16:
            samples.append(((time.monotonic() - t0) * 1000, ff.current()))

        chrome = [(t, n) for t, n in samples if n == "Google Chrome"]
        print(f"\n  第 {i} 次（启动前前台 {prev!r}）")
        if not chrome:
            print("    ✅ Chrome 从未成为前台（0 ms）")
            continue
        start, end = chrome[0][0], chrome[-1][0]
        # 用相邻采样点估上下界
        gap = (samples[1][0] - samples[0][0]) if len(samples) > 1 else 10.0
        print(f"    抢焦窗口：{end - start:.0f} ms（采样 {len(samples)} 次，"
              f"间隔约 {gap:.0f} ms）")
        print(f"    起止：+{start:.0f} ms → +{end:.0f} ms")
        transitions = []
        for t, n in samples:
            if not transitions or transitions[-1][1] != n:
                transitions.append((t, n))
        for t, n in transitions[:6]:
            print(f"      +{t:>7.0f} ms  {n}")

    close_browser()


def main() -> int:
    print("=" * 84)
    print("抢焦**阻断**方案对照（判据：启动后 15s 内最前台出现 Chrome 的次数，0 = 真正阻断）")
    print("=" * 84)

    rows = []
    for label, fn in VARIANTS:
        close_browser()
        time.sleep(2)
        prev = frontmost() or "Electron"
        fn(prev)
        hits, seq = observe(15.0)
        print(f"\n[{label}]")
        print(f"    启动前前台：{prev!r}")
        print(f"    序列：{seq}")
        print(f"    Chrome 成为前台：{hits} 次  → {'✅ 真正阻断' if hits == 0 else '❌ 仍会抢'}")
        rows.append((label, hits))

    close_browser()

    # ---- 关键补充：--no-startup-window 在**后续建页**时还成立吗？----
    # 只测启动阶段是不够的 —— 如果建页时又抢，那只是把问题推迟了几秒。
    print("\n" + "=" * 84)
    print("补充实验：--no-startup-window 下**通过 CDP 建页**，是否仍然 0 次？")
    print("=" * 84)
    close_browser()
    time.sleep(2)
    prev = frontmost() or "Electron"
    args = build_launch_args(find_browser(), BROWSER_PROFILE, 9222)
    args = [a for a in args if a != "about:blank"] + ["--no-startup-window"]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pid = wait_pid()

    hits_launch, seq_launch = observe(6.0)
    print(f"\n  启动后 6s：Chrome 成为前台 {hits_launch} 次  序列={seq_launch}")

    ctx_state = "未连接"
    page_ok = False
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp("http://127.0.0.1:9222", timeout=8000)
            ctxs = browser.contexts
            ctx_state = f"contexts={len(ctxs)}"
            if ctxs:
                ctx = ctxs[0]
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto("about:blank")
                page_ok = True
                ctx_state += f" pages={len(ctx.pages)}"
            else:
                # 没有 context 时试 browser.new_context()
                ctx = browser.new_context()
                page = ctx.new_page()
                page_ok = True
                ctx_state += " → 用 browser.new_context() 建成功"
    except Exception as exc:  # noqa: BLE001
        ctx_state += f" 异常：{str(exc)[:70]}"

    hits_page, seq_page = observe(10.0)
    print(f"  建页后 10s：Chrome 成为前台 {hits_page} 次  序列={seq_page}")
    print(f"  CDP/Playwright：{ctx_state}  建页成功={page_ok}")

    # 窗口最终位置
    r = subprocess.run([sys.executable, "-m", "scripts._window_probe"],
                       capture_output=True, text=True, cwd=PROJ)
    for line in r.stdout.strip().splitlines():
        if line.startswith("win"):
            print(f"  窗口：{line[:76]}")

    close_browser()

    total = hits_launch + hits_page
    print(f"\n  → 启动 + 建页合计抢焦 {total} 次："
          f"{'✅ 全程阻断' if total == 0 else '❌ 建页阶段仍会抢'}")

    print("\n" + "=" * 84)
    print(f"  {'方案':<34}{'Chrome 成为前台次数':>20}  结论")
    for label, hits in rows:
        print(f"  {label:<34}{hits:>20}  {'✅ 阻断' if hits == 0 else '❌ 被动挽救'}")
    print("=" * 84)
    return 0


if __name__ == "__main__":
    import argparse

    _ap = argparse.ArgumentParser(description="抢焦阻断方案对照 / 抢焦窗口精确测量")
    _ap.add_argument("--measure-window", action="store_true",
                     help="只做抢焦窗口的毫秒级精确测量")
    _ap.add_argument("--rounds", type=int, default=3, help="测量轮数")
    _a = _ap.parse_args()
    if _a.measure_window:
        measure_focus_window(_a.rounds)
        sys.exit(0)
    sys.exit(main())
