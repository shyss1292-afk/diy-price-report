"""启动参数对照实验：在**不伤指纹**的前提下能省多少内存。

为什么必须做对照而不是直接加参数：
"看着像优化"的参数分两类 —— 一类只改进程模型（页面内不可见，安全），
一类会改变渲染能力或页面可见特征（等于给风控递证据）。后者一律不能加。
这个脚本把候选参数逐个实跑，同时**验证 WebGL / Canvas / webdriver 仍然正常**，
只有"既省内存又指纹无变化"的才会被采纳。

用法：
    python -m scripts.browser_flags_eval            # 空载对照（快，不发平台请求）
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

from app.services.session import (  # noqa: E402
    DEFAULT_PROFILE,
    build_launch_args,
    close_browser,
    find_browser,
    launch_browser,
)
from scripts.browser_memory import print_snap, snapshot  # noqa: E402

# 候选参数：只列**不改渲染能力、不暴露自动化**的进程/缓存类开关。
# 明确不测的（会伤指纹，测了也不能用）：
#   --headless=new / --disable-gpu / --use-gl=swiftshader
#   --disable-blink-features=AutomationControlled / --enable-automation
#   --blink-settings=imagesEnabled=false（图片是滑块验证码的载体）
_CACHE_OFF = (
    "--disable-features=Translate,BackForwardCache,CalculateNativeWinOcclusion,"
    "SpareRendererForSitePerProcess,AudioServiceOutOfProcess"
)

VARIANTS: list[tuple[str, list[str]]] = [
    ("基线（现状）", []),
    ("renderer-process-limit=1", ["--renderer-process-limit=1"]),
    ("关 SpareRenderer", ["--disable-features=SpareRendererForSitePerProcess"]),
    ("关 AudioServiceOutOfProcess", ["--disable-features=AudioServiceOutOfProcess"]),
    ("关缓存类（BackForwardCache 等）", [_CACHE_OFF]),
    ("组合（全部安全项）", ["--renderer-process-limit=1", _CACHE_OFF, "--mute-audio"]),
]

PROBE = """
(() => {
  const c = document.createElement('canvas');
  const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
  let renderer = null;
  if (gl) {
    const ext = gl.getExtension('WEBGL_debug_renderer_info');
    if (ext) renderer = gl.getParameter(ext.UNMASKED_RENDERER_WEBGL);
  }
  const c2 = document.createElement('canvas');
  c2.width = c2.height = 8;
  const ctx = c2.getContext('2d');
  ctx.fillStyle = 'rgb(255,0,0)';
  ctx.fillRect(0, 0, 8, 8);
  const px = Array.from(ctx.getImageData(0, 0, 8, 8).data.slice(0, 4));
  return {
    webgl: !!gl,
    webgl2: !!document.createElement('canvas').getContext('webgl2'),
    renderer: renderer,
    canvas2d: px,
    webdriver: navigator.webdriver,
    plugins: navigator.plugins.length,
    dpr: window.devicePixelRatio,
  };
})()
"""


def fingerprint_probe() -> dict:
    """用 CDP 连上去做指纹探针（与生产同一套连接方式）。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("about:blank")
        result = page.evaluate(PROBE)
        return result


def main() -> int:
    binary = find_browser()
    if not binary:
        print("找不到 Chrome")
        return 1

    print("=" * 78)
    print("启动参数对照实验（空载，每个组合启动后稳定 6s 再测）")
    print("=" * 78)

    rows = []
    for label, extra in VARIANTS:
        close_browser()
        time.sleep(1)

        # 用项目**同一套** base args，只在尾部追加候选参数，
        # 保证对比的是"加了这一项"的净效果。
        os.environ["DIYPRICE_EXTRA_LAUNCH_ARGS"] = " ".join(extra)
        try:
            ok = launch_browser()
        finally:
            os.environ.pop("DIYPRICE_EXTRA_LAUNCH_ARGS", None)
        if not ok:
            print(f"\n[{label}] 启动失败，跳过")
            continue

        time.sleep(6)
        snap = snapshot()
        procs = sum(len(v) for v in snap.values())
        total = sum(sum(v.values()) for v in snap.values())
        renderers = len(snap.get("渲染 renderer", {}))

        probe = {}
        try:
            probe = fingerprint_probe()
        except Exception as exc:  # noqa: BLE001
            probe = {"error": str(exc)[:70]}

        print(f"\n[{label}]  额外参数: {extra or '（无）'}")
        print_snap("  ", snap)
        print(f"     渲染进程数 {renderers}")
        if "error" in probe:
            print(f"     指纹探针失败：{probe['error']}")
        else:
            print(f"     WebGL={probe['webgl']} WebGL2={probe['webgl2']} "
                  f"Canvas2D={probe['canvas2d']} webdriver={probe['webdriver']} "
                  f"plugins={probe['plugins']} dpr={probe['dpr']}")
            print(f"     GPU renderer={probe['renderer']}")

        rows.append((label, procs, total, renderers, probe))

    close_browser()

    print("\n" + "=" * 78)
    print("汇总")
    print("=" * 78)
    print("  ⚠️ RSS 单次读数噪声约 ±25%（macOS 会压缩/换页），**只看进程数与指纹**；")
    print("     内存收益用 scripts/browser_memory.py --during 测真实采集峰值。\n")
    base = next((r for r in rows if r[0] == "基线（现状）"), None)
    print(f"  {'组合':<30}{'进程':>5}{'渲染':>5}{'内存MB':>9}  {'指纹':<8}")
    for label, procs, total, renderers, probe in rows:
        fp = ("❌探针失败" if "error" in probe
              else ("✅ 完好" if probe.get("webgl") and probe.get("canvas2d") == [255, 0, 0, 255]
                    and probe.get("webdriver") is False else "⚠️ 异常"))
        delta = ""
        if base:
            d = renderers - base[3]
            delta = f"（渲染 {d:+d}）" if d else "（渲染持平）"
        print(f"  {label:<30}{procs:>5}{renderers:>5}{total:>9.0f}  {fp:<8}{delta}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
