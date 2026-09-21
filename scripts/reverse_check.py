#!/usr/bin/env python
"""反向验证：证明 `scripts/selftest.py` 的断言真的能拦住回归。

为什么需要它
------------
自检全绿只说明"没坏"，不说明"坏了会被发现"。一个写歪的断言（锚点错了、
期望值抄了实现、用例之间互相遮蔽）会**永远绿**，比没有断言更危险 ——
它给人已经验证过的错觉。

这个脚本把每个关键机制**逐个改坏**，然后跑自检，要求自检必须变红。
任何一次"改坏了但自检仍然全绿"，说明那条守卫是虚的。

用法
----
    python -m scripts.reverse_check        # 退出码 0 = 全部守卫有效

它是纯本地的：只动源码文本、跑自检、立刻还原（`finally` 里兜底），
不碰数据库、不碰浏览器、不碰网络。

新增关键机制时，往 `BREAKS` 里加一条即可 —— 锚点用**最小且唯一**的片段，
太短会误伤别处，太长会随重构漂移。
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

PROJ = pathlib.Path(__file__).resolve().parent.parent
PY = sys.executable

BREAKER = PROJ / "app/services/breaker.py"
PIPELINE = PROJ / "app/services/pipeline.py"
BASE = PROJ / "app/collectors/base.py"
DB = PROJ / "app/db.py"
PROBE = PROJ / "app/services/probe.py"
SEED = PROJ / "app/seed_data.py"
CLEAN = PROJ / "app/services/clean.py"
NORMALIZE = PROJ / "app/services/normalize.py"
SCHED = PROJ / "scripts/collect_scheduled.sh"

# (说明, 文件, 原文锚点, 改坏成)
BREAKS: list[tuple[str, pathlib.Path, str, str]] = [
    (
        "退避阶梯失效（trip 改回固定 180s）",
        BREAKER,
        "        cooldown = float(seconds) if seconds is not None else backoff_for(consecutive, sev)",
        "        cooldown = float(seconds) if seconds is not None else 180.0",
    ),
    (
        "Fast-Fail 变成会睡觉（不再是 fast-fail）",
        BREAKER,
        "    return False, remaining\n\n\ndef wait_until_ready(",
        "    time.sleep(2.0)\n    return False, remaining\n\n\ndef wait_until_ready(",
    ),
    (
        "record_success 不再清零连续计数",
        BREAKER,
        '        data["sources"].pop(source, None)\n        _save(data)\n    return True',
        "        pass\n    return True",
    ),
    (
        "0 条文案退回「疑似被限流」（把数据质量问题说成风控）",
        PIPELINE,
        "    parsed = matched + unmatched + filtered\n    if parsed:",
        "    parsed = matched + unmatched + filtered\n    if False:",
    ),
    (
        "成功重置被放宽（命中限流也照样清零计数）",
        BASE,
        "    return int(quote_count or 0) > 0 and not aborted and not suspect_throttle",
        "    return int(quote_count or 0) > 0",
    ),
    (
        "软风控退避上限失效（系统繁忙也走完整阶梯）",
        BREAKER,
        '    if severity == "soft":\n        return min(rung, soft_cap())',
        '    if False:\n        return min(rung, soft_cap())',
    ),
    (
        "错误分级顺序反了（先判软再判硬 → 京东真拦截被降级）",
        BREAKER,
        "    for marker in _HARD_MARKERS:\n"
        "        if marker.lower() in text:\n"
        '            return "hard"\n'
        "    for marker in _SOFT_MARKERS:",
        "    for marker in _SOFT_MARKERS:\n"
        "        if marker.lower() in text:\n"
        '            return "soft"\n'
        "    for marker in _HARD_MARKERS:",
    ),
    (
        "busy_timeout 失效（拿不到写锁立刻抛异常）",
        DB,
        'cur.execute(f"PRAGMA busy_timeout={busy_timeout_ms()}")',
        'cur.execute("PRAGMA busy_timeout=0")',
    ),
    (
        "探针失败改成会放大阶梯",
        PROBE,
        "    except policy.RateLimitError as exc:\n",
        "    except policy.RateLimitError as exc:\n"
        "        breaker.trip(source, reason=exc.indicator)\n",
    ),
    (
        "探针的 verified 豁免被去掉（探活成功也清不掉退避）",
        BREAKER,
        '        if not verified and float(entry.get("until", 0)) > time.time():',
        '        if float(entry.get("until", 0)) > time.time():',
    ),
    (
        "日志收尾阈值被调小（会误杀正在跑的轮次）",
        PIPELINE,
        "_STALE_RUNNING_MINUTES = 45",
        "_STALE_RUNNING_MINUTES = 5",
    ),
    (
        "墙钟兜底上限被调成超过锁陈旧阈值（会重复采集）",
        PIPELINE,
        "_DEFAULT_WALL_CLOCK_LIMIT = 2100.0",
        "_DEFAULT_WALL_CLOCK_LIMIT = 3000.0",
    ),
    (
        "别名粘连变体失效（'5500XT' 这类写法整类漏匹配）",
        SEED,
        '    forms |= {_GLUE_SUFFIX.sub(r"\\1\\2", f) for f in list(forms)}',
        "    forms |= set()",
    ),
    (
        "品牌粘连变体失效（'RTX4060 Ti' 被判成 'RTX4060 8G'）",
        SEED,
        "    forms |= {_GLUE_BRAND.sub(lambda m: m.group(1), f) for f in list(forms)}",
        "    forms |= set()",
    ),
    (
        "去品牌与去容量叠加 → 产出裸数字别名（跨容量抢单）",
        SEED,
        "    stripped = _CAPACITY_SUFFIX.sub(\"\", model)\n"
        "    if stripped != model:\n"
        "        forms.add(stripped)\n",
        "    stripped = _CAPACITY_SUFFIX.sub(\"\", model)\n"
        "    if stripped != model:\n"
        "        forms.add(stripped)\n"
        "        for _p in _BRAND_PREFIXES:\n"
        "            if stripped.upper().startswith(_p):\n"
        "                forms.add(stripped[len(_p):])\n",
    ),
    (
        "求购帖过滤失效（求购价会当成成交价入库）",
        CLEAN,
        "    for pattern in _WANTED_PATTERNS:",
        "    for pattern in ():",
    ),
    (
        "求购规则写宽（'绝不收购' 这类否定语境被误杀）",
        CLEAN,
        '    re.compile(r"求购"),',
        '    re.compile(r"求购"),\n    re.compile(r"收购"),',
    ),
    (
        "编号式罗列回归（把规格列表当打包商品误杀）",
        CLEAN,
        "    return len(_SEPARATORS.findall(title)) >= _MULTI_MODEL_THRESHOLD",
        "    if len(_SEPARATORS.findall(title)) >= _MULTI_MODEL_THRESHOLD:\n"
        "        return True\n"
        '    return len(re.findall(r"(?:^|\\s)[1-9][.、)）]\\s*\\S", title)) >= 3',
    ),
    (
        "容量抽取器失效（容量消歧全部退化）",
        NORMALIZE,
        '    return {int(m.group(1)) for m in _CAPACITY_IN_TEXT.finditer(text or "")}',
        "    return set()",
    ),
    (
        "容量消歧的底线被去掉（无容量标题会随机漂移）",
        NORMALIZE,
        "            if len(matched) == 1:\n"
        "                return matched[0]\n"
        "        return None",
        "            if len(matched) >= 1:\n"
        "                return matched[0]\n"
        "        return candidates[0]",
    ),
    (
        "看门狗退回单 PID 广播（Chrome / node 驱动会变孤儿）",
        SCHED,
        'kill -TERM -- "-$pgid" 2>/dev/null',
        'kill -TERM "$COLLECT_PID" 2>/dev/null',
    ),
]


def _run_selftest() -> tuple[int, int]:
    """跑一次自检，返回 (通过数, 总数)。"""
    proc = subprocess.run(
        [PY, "-m", "scripts.selftest"], cwd=PROJ, capture_output=True, text=True
    )
    out = proc.stdout + proc.stderr
    match = re.search(r"通过 (\d+) / 共 (\d+)", out)
    if not match:
        print("    !! 自检没有输出统计行，原始尾部：")
        for line in out.strip().splitlines()[-6:]:
            print("      ", line)
        return -1, -1
    return int(match.group(1)), int(match.group(2))


def main() -> int:
    backups: dict[pathlib.Path, pathlib.Path] = {}
    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="diyprice_reverse_"))

    def restore() -> None:
        for path, bak in backups.items():
            shutil.copy2(bak, path)

    try:
        for path in {BREAKER, PIPELINE, BASE, DB, PROBE, SEED, CLEAN, NORMALIZE, SCHED}:
            bak = tmpdir / path.name
            shutil.copy2(path, bak)
            backups[path] = bak

        base_ok, base_total = _run_selftest()
        print(f"基线（未改坏）：通过 {base_ok}/{base_total}")
        if base_ok != base_total:
            print("基线就不是全绿 —— 先修好自检，再谈反向验证")
            return 1

        all_caught = True
        for label, path, old, new in BREAKS:
            src = path.read_text(encoding="utf-8")
            if old not in src:
                print(f"  ⚠️ {label} —— 注入失败：锚点找不到，这条守卫用例需要跟着重构更新")
                all_caught = False
                continue
            path.write_text(src.replace(old, new, 1), encoding="utf-8")
            try:
                bad_ok, bad_total = _run_selftest()
            finally:
                restore()
            caught = 0 <= bad_ok < bad_total
            print(
                f"  {'✅' if caught else '❌'} {label} —— 自检 {bad_ok}/{bad_total}"
                f"（失败 {bad_total - bad_ok} 条）{'被拦住' if caught else '**没拦住，守卫是虚的**'}"
            )
            all_caught = all_caught and caught

        restored_ok, restored_total = _run_selftest()
        print(f"还原后：通过 {restored_ok}/{restored_total}")
        if restored_ok != restored_total:
            print("!! 还原失败，请检查源码是否被改坏")
            return 1
        print("结论：全部守卫有效" if all_caught else "结论：存在失效守卫，见上方 ❌")
        return 0 if all_caught else 1
    finally:
        restore()
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
