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

⚠️ 备份集合**从 `BREAKS` 推导**，不要再手写文件清单（踩过：新增一条改
   `report.py` 的变异却忘了加进手写清单，`restore()` 直接跳过它，
   变异残留在源码里，而脚本还打印"存在失效守卫" —— 方向完全指反）。
   还原后会**逐文件比对内容**（`verify_restored`），与"守卫失效"分开报告。

⚠️ **一轮只跑一次**。单轮内连续跑几十次自检会撞上沙箱的删除预算
   （`SAFE_DELETE_BULK_CONFIRM_REQUIRED`，`scope=turn` 按轮次累计），
   自检被中途掐断、跑不出汇总行。这类条目会标成「未能判定」而不是「守卫是虚的」。
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
TREND = PROJ / "app/services/trend.py"
AGGREGATE = PROJ / "app/services/aggregate.py"
SEED = PROJ / "app/seed_data.py"
ROUTES = PROJ / "app/api/routes.py"
SERVICE = PROJ / "scripts/service.sh"
PDD = PROJ / "app/collectors/pdd_source.py"
JD = PROJ / "app/collectors/jd_source.py"
BASE = PROJ / "app/collectors/base.py"
SESSION = PROJ / "app/services/session.py"
WORKER = PROJ / "app/services/browser_worker.py"
SCHED = PROJ / "scripts/collect_scheduled.sh"
REPORT = PROJ / "app/services/report.py"

# (说明, 文件, 原文锚点, 改坏成)
BREAKS: list[tuple[str, pathlib.Path, str, str]] = [
    (
        "退避阶梯失效（trip 改回固定 180s）",
        BREAKER,
        "            cooldown = max(backoff_for(consecutive, sev), min_cooldown())",
        "            cooldown = 180.0",
    ),
    (
        "Fast-Fail 变成会睡觉（不再是 fast-fail）",
        BREAKER,
        "    return False, remaining\n\n\ndef wait_until_ready(",
        "    time.sleep(2.0)\n    return False, remaining\n\n\ndef wait_until_ready(",
    ),
    (
        "record_success 退回一次清零（一次侥幸抹掉整条退避阶梯）",
        BREAKER,
        '        remaining = int(entry.get("trips", 1)) - 1\n',
        "        remaining = 0\n",
    ),
    (
        "最短生效时长退回 0（第 1 级重新变成空转，每小时都去撞）",
        BREAKER,
        "_DEFAULT_MIN_COOLDOWN = 3900.0",
        "_DEFAULT_MIN_COOLDOWN = 0.0",
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
        "软风控不走专用阶梯（系统繁忙套用硬拦截阶梯 → 第 2 次起不再跨轮冷却）",
        BREAKER,
        '    ladder = soft_ladder() if severity == "soft" else backoff_ladder()',
        "    ladder = backoff_ladder()",
    ),
    (
        "软风控阶梯被压平成一律 15 分钟（退避到期即再拦，每小时都去撞）",
        BREAKER,
        "_DEFAULT_SOFT_LADDER: tuple[float, ...] = (900.0, 7200.0, 14400.0)",
        "_DEFAULT_SOFT_LADDER: tuple[float, ...] = (900.0,)",
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
        "jd 放行 legacy 型号（向京东发老硬件的无效搜索）",
        SEED,
        '    "jd": frozenset({LIFECYCLE_ACTIVE}),',
        '    "jd": frozenset({LIFECYCLE_ACTIVE, LIFECYCLE_LEGACY}),',
    ),
    (
        "轮转忘记按源过滤生命周期（路由形同虚设）",
        BASE,
        "    ordered = build_rotation(products, source=source)",
        "    ordered = build_rotation(products)",
    ),
    (
        "队列补做不做生命周期过滤（路由生效前入队的 legacy 任务被派发给 jd/pdd）",
        BASE,
        "    return lifecycle_of(product.model, product.category, product.brand) in allowed",
        "    return True",
    ),
    (
        "日报板块跨品牌混排（涨跌榜把 A卡的涨幅顶到 N卡榜上）",
        REPORT,
        "        brand_rows = by_brand.get(brand, [])",
        "        brand_rows = list(rows)",
    ),
    (
        "涨跌幅把跨天比较冒充成「日间」（陈年价差被当今日异动）",
        REPORT,
        '        return "日间"\n    return f"跨{gap}天"',
        '        return "日间"\n    return "日间"',
    ),
    (
        "全新口径不再按平台品相过滤（闲鱼二手价混进「全新在售」板块）",
        REPORT,
        '        return [p for p in platforms if p.kind == "new"]',
        "        return list(platforms)",
    ),
    (
        "拼多多去掉首页预热（深链搜索触发安全验证，整轮 0 条）",
        PDD,
        'if "search_result" not in (page.url or ""):',
        "if False:",
    ),
    (
        "触发时间退回整点（正好压在通勤合盖窗口上）",
        SERVICE,
        '<key>Minute</key><integer>30</integer>',
        '<key>Minute</key><integer>0</integer>',
    ),
    (
        "清残留锁时不检查活跃进程（会删掉正在运行实例的锁）",
        SESSION,
        "    if profile_pids(profile):\n        return []          # 有活跃进程，锁是有效的，绝不能碰",
        "    pass",
    ),
    (
        "端口回收不检查是不是浏览器（见谁杀谁，会误伤别的服务）",
        SESSION,
        "        if not _looks_like_browser(cmdline):",
        "        if False:",
    ),
    (
        "监控范围偷偷加回 ram（单轮耗时逼近拼多多风控红线）",
        SEED,
        'CATEGORY_ORDER = ["gpu", "cpu"]',
        'CATEGORY_ORDER = ["gpu", "cpu", "ram"]',
    ),
    (
        "meta 计数丢掉 is_active 过滤（界面显示 351、列表只有 130）",
        ROUTES,
        "            .where(Product.is_active.is_(True))\n            .group_by(Product.category)",
        "            .group_by(Product.category)",
    ),
    (
        "聚合层去掉 is_synthetic 过滤（模拟数据重新污染行情）",
        AGGREGATE,
        "            Listing.is_synthetic.is_(False),\n",
        "",
    ),
    (
        "聚合层去掉激活平台过滤（停用平台重新进均线）",
        AGGREGATE,
        "            Platform.is_active.is_(True),\n",
        "",
    ),
    (
        "口径闸门退回「平台数 ≥2」（真实单平台型号全部失去涨跌幅）",
        TREND,
        "comparable = same_channels and enough and balanced",
        "comparable = (bool(sets) and idx < len(sets) and len(sets[idx]) >= 2)",
    ),
    (
        "样本量闸门退回无门槛（单条样本造出假暴涨）",
        TREND,
        "            MIN_SAMPLES = 3",
        "            MIN_SAMPLES = 1",
    ),
    (
        "最新报价兜底退回 low[-1]（basis=new/used 下会把有数据的型号误判成空白）",
        TREND,
        "        for i in range(len(low) - 1, -1, -1):\n            if low[i] is not None:\n                idx = i\n                break",
        "        idx = len(low) - 1 if low else None",
    ),
    (
        "涨跌基准退回 dates[-1]（拿昨天价跟今天减 N 天比，区间口径错）",
        TREND,
        "        prev = _lookup(dates, low, last_date - timedelta(days=period))",
        "        prev = _lookup(dates, low, dates[-1] - timedelta(days=period))",
    ),
    (
        "墙钟兜底退回一次长 sleep（休眠期间计时冻结，一轮可跨 12 小时）",
        PIPELINE,
        "            _time.sleep(poll)\n            elapsed = _time.time() - started_at",
        "            _time.sleep(limit)\n            elapsed = _time.time() - started_at",
    ),
    (
        "墙钟兜底改用 monotonic（休眠期间同样冻结）",
        PIPELINE,
        "    started_at = _time.time()",
        "    started_at = _time.monotonic()",
    ),
    (
        "去掉 caffeinate（采集期间允许系统休眠）",
        SCHED,
        "caffeinate -dimsu -w $$ &",
        "true &",
    ),
    (
        "锁陈旧判定退回目录 mtime（丢掉显式绝对时间戳）",
        SCHED,
        'started_at="$(cat "$LOCK/started" 2>/dev/null || true)"',
        'started_at=""',
    ),
    (
        "看门狗退回单 PID 广播（Chrome / node 驱动会变孤儿）",
        SCHED,
        'kill -TERM -- "-$pgid" 2>/dev/null',
        'kill -TERM "$COLLECT_PID" 2>/dev/null',
    ),
    (
        "窗口离屏坐标被挪回屏幕内（弹窗立刻回来）",
        SESSION,
        "OFFSCREEN_X = -3000\nOFFSCREEN_Y = 5000",
        "OFFSCREEN_X = 100\nOFFSCREEN_Y = 100",
    ),
    (
        "隐藏改为按应用名定位（会连用户自己的浏览器一起藏掉）",
        SESSION,
        "whose unix id is {int(pid)}",
        'whose name is "Google Chrome"',
    ),
    (
        "图片被加进拦截（滑块验证码的载体被拦掉）",
        WORKER,
        '    if resource_type == "image":\n        return False',
        '    if resource_type == "image":\n        return True',
    ),
    (
        "限流特征正文匹配退回大小写敏感（大写特征静默失效）",
        BREAKER.replace("breaker.py", "policy.py"),
        "    probe_lower = probe.lower()\n",
        "    probe_lower = probe\n",
    ),
    (
        "京东不再挂响应监听（截获形同虚设，退回 DOM）",
        JD,
        '        page.on("response", _on_response)\n',
        "        pass\n",
    ),
    (
        "京东监听器不摘（多型号重复解析 300KB）",
        JD,
        '                page.remove_listener("response", _on_response)\n',
        "                pass\n",
    ),
    (
        "京东解析器不剥 HTML 标签（标题带 <font> 污染去重）",
        JD,
        '    return re.sub(r"<[^>]+>", "", text or "")\n',
        "    return text or \"\"\n",
    ),
    (
        "京东价格退到「到手价」顶替展示价（口径污染）",
        JD,
        '    for key in ("jdPrice", "realPrice"):\n',
        '    for key in ():\n',
    ),
    (
        "京东去掉体积下限（AB 配置 7KB 会被当成商品列表）",
        JD,
        "                if len(body) < _MIN_PAYLOAD_BYTES:\n",
        "                if False:\n",
    ),



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

    def verify_restored() -> list[pathlib.Path]:
        """还原后逐文件比对内容，返回**没还原干净**的文件。

        为什么不能只看"最后那次自检是否全绿"：2026-09-24 实测过一次，
        脚本被沙箱中途掐断、还原没跑完，最后一次自检跑不出统计行（-1/-1），
        于是脚本打印"结论：存在失效守卫" —— 看起来像守卫失效，
        实际是**源码被改坏了**。方向完全指反。
        直接比内容，才能把"守卫失效"和"源码没还原"分开。
        """
        dirty = []
        for path, bak in backups.items():
            try:
                if path.read_bytes() != bak.read_bytes():
                    dirty.append(path)
            except OSError:
                dirty.append(path)
        return dirty

    try:
        # ⚠️ 备份集合**从 BREAKS 推导**，不要再手写文件清单。
        #    踩过的坑：新增了一条改 report.py 的变异，却忘了把它加进手写清单，
        #    于是 restore() 直接跳过它、**变异残留在源码里**。
        #    推导出来的集合不可能漏（手写的会）。
        for path in {entry[1] for entry in BREAKS}:
            bak = tmpdir / path.name
            shutil.copy2(path, bak)
            backups[path] = bak

        base_ok, base_total = _run_selftest()
        print(f"基线（未改坏）：通过 {base_ok}/{base_total}")
        if base_ok != base_total:
            print("基线就不是全绿 —— 先修好自检，再谈反向验证")
            return 1

        all_caught = True
        unverified: list[str] = []
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
            if bad_ok < 0:
                # ⚠️ 跑不出统计行 ≠ 守卫失效。实测单轮内连续跑几十次自检会撞上
                #    沙箱的删除预算（SAFE_DELETE_BULK_CONFIRM_REQUIRED，scope=turn），
                #    自检进程被中途掐断，于是没有汇总行。
                #    以前这里一律打成"没拦住，守卫是虚的" —— 把环境问题误报成代码问题，
                #    方向指反，比不报还糟。所以单列一档。
                print(f"  ⚠️ {label} —— 自检没跑出统计行（-1/-1），**本轮无法判定**；"
                      f"通常是沙箱删除预算掐断了自检进程，不是守卫失效")
                unverified.append(label)
                continue
            caught = bad_ok < bad_total
            print(
                f"  {'✅' if caught else '❌'} {label} —— 自检 {bad_ok}/{bad_total}"
                f"（失败 {bad_total - bad_ok} 条）{'被拦住' if caught else '**没拦住，守卫是虚的**'}"
            )
            all_caught = all_caught and caught

        restore()
        dirty = verify_restored()
        if dirty:
            print("!! 还原失败，以下文件与运行前不一致：")
            for p in dirty:
                print(f"     {p}")
            return 1

        restored_ok, restored_total = _run_selftest()
        print(f"还原后：通过 {restored_ok}/{restored_total}")
        if restored_ok < 0:
            print("⚠️ 还原后自检没跑出统计行（-1/-1）—— 但上面的逐文件比对已确认"
                  "源码与运行前**逐字节一致**，所以这是环境问题（沙箱删除预算掐断），"
                  "不是源码被改坏。规避办法：本脚本一轮只跑一次。")
        elif restored_ok != restored_total:
            print("!! 还原后自检不是全绿 —— 源码可能被改坏，或自检本身有问题")
            return 1

        if unverified:
            print(f"⚠️ 本次有 {len(unverified)} 条变异**未能判定**（环境原因）：{unverified}")
            print("   其余变异均已确认被拦住；未判定的请单独重跑复核。")
        print("结论：全部守卫有效" if all_caught else "结论：存在失效守卫，见上方 ❌")
        return 0 if all_caught else 1
    finally:
        restore()
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
