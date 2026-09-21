"""Chrome 内存构成测量 —— 给"8G 机器怎么压到 800MB 以内"提供依据。

为什么按**进程类型**拆开看：总量一个数字没用。Chrome 是多进程架构，
"主进程 200MB + 渲染 300MB + GPU 150MB + 一堆 utility 各 50MB" 与
"渲染进程 500MB" 是完全不同的优化方向。

进程类型从命令行里的 `--type=` 取（浏览器主进程没有这个参数）。
判定用**专属 profile 标记**，绝不按进程名模糊匹配 —— 否则会把你日常
浏览器的窗口/进程也算进来（这个坑踩过，见 2026-09-21 记忆）。

用法：
    python -m scripts.browser_memory              # 空载基线
    python -m scripts.browser_memory --during     # 跑一轮真实采集，采样全程峰值
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from collections import defaultdict

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

from app.services.session import DEFAULT_PROFILE, profile_pids  # noqa: E402

_TYPES = (
    ("--type=renderer", "渲染 renderer"),
    ("--type=gpu-process", "GPU"),
    ("--type=utility", "utility"),
    ("--type=zygote", "zygote"),
)


def classify(command: str) -> str:
    for marker, label in _TYPES:
        if marker in command:
            return label
    return "主进程 browser"


def snapshot() -> dict[str, dict]:
    """当前受管 Chrome 的进程明细：{类型: {pid: rss_mb}}。"""
    out = subprocess.run(
        ["ps", "-eo", "pid=,rss=,command="], capture_output=True, text=True
    ).stdout
    marker = str(DEFAULT_PROFILE)
    result: dict[str, dict] = defaultdict(dict)
    for line in out.splitlines():
        if marker not in line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, rss, command = parts
        try:
            result[classify(command)][int(pid)] = int(rss) / 1024.0
        except ValueError:
            continue
    return dict(result)


def summarize(snap: dict[str, dict]) -> tuple[int, float]:
    procs = sum(len(v) for v in snap.values())
    total = sum(sum(v.values()) for v in snap.values())
    return procs, total


def print_snap(label: str, snap: dict[str, dict]) -> None:
    procs, total = summarize(snap)
    print(f"  {label}：{procs} 进程 / {total:.0f} MB")
    for kind in sorted(snap):
        items = snap[kind]
        detail = " ".join(f"{mb:.0f}" for mb in sorted(items.values(), reverse=True))
        print(f"     {kind:16} {len(items):>2} 个 · {sum(items.values()):>6.0f} MB  [{detail}]")


def main() -> int:
    ap = argparse.ArgumentParser(description="Chrome 内存构成测量")
    ap.add_argument("--during", action="store_true", help="跑一轮真实采集并采样全程")
    ap.add_argument("--source", default="xianyu", help="--during 时用哪个源")
    ap.add_argument("--limit", type=int, default=6, help="--during 时的型号数")
    ap.add_argument("--interval", type=float, default=2.0, help="采样间隔（秒）")
    args = ap.parse_args()

    print("=" * 72)
    print("Chrome 内存构成测量")
    print("=" * 72)

    print("\n[1] 测量前基线（受管 Chrome 应为 0 进程）")
    print_snap("当前", snapshot())

    if not args.during:
        print("\n（加 --during 可跑一轮真实采集并采样全程峰值）")
        return 0

    print(f"\n[2] 启动真实采集（{args.source}，{args.limit} 个型号），全程采样")
    env = dict(os.environ)
    env[f"DIYPRICE_{args.source.upper()}_LIMIT"] = str(args.limit)
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.cli", "collect", "--sources", args.source],
        cwd=PROJ, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )

    samples: list[tuple[float, int, float, dict]] = []
    stop = threading.Event()

    def sample() -> None:
        t0 = time.monotonic()
        while not stop.is_set():
            snap = snapshot()
            procs, total = summarize(snap)
            samples.append((time.monotonic() - t0, procs, total, snap))
            time.sleep(args.interval)

    watcher = threading.Thread(target=sample, daemon=True)
    watcher.start()
    out, _ = proc.communicate()
    stop.set()
    watcher.join(timeout=5)

    print("\n[3] 采样结果")
    if samples:
        peak = max(samples, key=lambda s: s[2])
        print(f"  采样 {len(samples)} 次 / 共 {samples[-1][0]:.0f}s")
        print(f"  峰值：{peak[1]} 进程 / {peak[2]:.0f} MB（第 {peak[0]:.0f}s）")
        print("\n  峰值时刻的构成：")
        print_snap("  ", peak[3])
        print("\n  采样序列（进程数 / MB）：")
        for t, p, tot, _ in samples[:: max(1, len(samples) // 14)]:
            print(f"     +{t:>5.0f}s  {p:>3} 进程  {tot:>6.0f} MB")

    print("\n[4] 采集结束后（应回到 0 进程）")
    time.sleep(3)
    print_snap("当前", snapshot())

    print("\n[5] 采集进程输出尾部")
    for line in out.strip().splitlines()[-8:]:
        print("   ", line[:140])
    return 0


if __name__ == "__main__":
    sys.exit(main())
