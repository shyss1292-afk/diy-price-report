#!/usr/bin/env bash
# 定时采集入口 —— 由 launchd 调用（不是由 Web 服务内的 APScheduler）。
#
# 为什么改成 launchd
# ------------------
# 原来用进程内的 APScheduler，实测在 Mac 睡眠后会**彻底停摆**：
# 2026-09-16 夜里 23:30 之后就没再触发过任何任务，凌晨 02:00 的全量采集、
# 早上 8/9/10 点的轮转全部没跑，而且 next_run 卡在过去时间上。
# launchd 是 macOS 原生调度，睡醒后会把错过的任务补跑（合并成一次），
# 这是进程内定时器做不到的。
#
# 这一轮做什么
# ------------
#   · 浏览器健康检查（睡眠后会「假活」，必须先探一次）
#   · 反爬源轮转采集：jd/pdd/xianyu，只看显卡 + CPU
#   · 汇总一行结果便于扫日志
#
# 注：2026-09-17 起 ZOL / 太平洋电脑网两个静态站已停用，原先的
#     「每日首次静态站全品类打底」随之取消 —— 剩下的三个源都有配额限制，
#     全部额度留给日报真正需要的显卡 + CPU。
#
# 用法：./scripts/collect_scheduled.sh
set -u

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

PYTHON_BIN="/Users/apple/.workbuddy/binaries/python/envs/diyprice/bin/python"
LOG="$PROJECT_DIR/data/collect.log"
LOCK="$PROJECT_DIR/data/.collect.lock"

mkdir -p "$PROJECT_DIR/data"

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

# 日志轮转：超过 2MB 就留最后 500 行，避免无限增长
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 2000000 ]; then
  tail -500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

# 磁盘兜底：清理**非日志**的陈旧产物。
#
# 为什么需要：日志有上面的轮转，但下面这两类**没有任何清理机制**，
# 实测在 8G 紧凑环境下会静默膨胀：
#   · 数据库备份 data/*.bak-*        —— 已有一个 5 天前的备份占 72.76 MB
#   · queue_results/<source>-<day>.jsonl —— 约 0.87 MB/天，一年约 317 MB
#
# ⚠️ 删旧的 queue_results 是**安全**的：task_id 形如 "source:product_id:day"，
#    文件名里带日期，旧日期的文件不会再被读取。
#    （当天那个是采集中间态 —— 崩溃后靠它捡回结果，必须留。）
# ⚠️ 备份保留 14 天，且**至少留一个** —— 它是数据库唯一的回滚点，
#    全删掉等于把"改坏了能退回去"这条路断掉。
_retention_days=7
_removed=$(find "$PROJECT_DIR/data/queue_results" -maxdepth 1 -name "*.jsonl" \
             -mtime +$_retention_days -print -delete 2>/dev/null | wc -l | tr -d ' ')
if [ "${_removed:-0}" -gt 0 ]; then
  log "磁盘兜底：清理 ${_removed} 个超过 ${_retention_days} 天的 queue_results 文件"
fi

_bak_count=$(ls -1 "$PROJECT_DIR"/data/*.bak-* 2>/dev/null | wc -l | tr -d ' ')
if [ "${_bak_count:-0}" -gt 1 ]; then
  _old_baks=$(find "$PROJECT_DIR/data" -maxdepth 1 -name "*.bak-*" -mtime +14 \
                -print -delete 2>/dev/null | wc -l | tr -d ' ')
  if [ "${_old_baks:-0}" -gt 0 ]; then
    log "磁盘兜底：清理 ${_old_baks} 个超过 14 天的数据库备份（至少保留最新一个）"
  fi
fi

# 单实例锁：避免手动执行与定时任务撞车（launchd 自身对同一 label 也会串行）。
#
# ⚠️ 锁必须能自愈，否则一次卡死会让采集**整体停摆且毫无告警**。
# 实测（2026-09-18）：02:14 启动的一轮采集卡在网络等待上（进程栈停在
# select_kqueue），一直占着锁不放，导致当天 08:00 / 09:00 / 10:00 三轮
# 全部被这里"跳过"，一整天的数据都是空的，日志里还只是一句轻描淡写的
# "已有采集在进行中"。
# 而且它还握着 SQLite 的写事务不放 —— 新采集即便绕过锁也会
# `database is locked` 写不进去。
#
# 所以：锁里记 PID，接管前先判断持有者是否还活着、锁是否过期。
LOCK_STALE_SECONDS=2400          # 锁超过 40 分钟即视为卡死（正常一轮 9~10 分钟）
if ! mkdir "$LOCK" 2>/dev/null; then
  holder="$(cat "$LOCK/pid" 2>/dev/null || true)"
  age=$(( $(date +%s) - $(stat -f %m "$LOCK" 2>/dev/null || echo 0) ))
  if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null && [ "$age" -lt "$LOCK_STALE_SECONDS" ]; then
    log "已有采集在进行中（PID ${holder}，已运行 $((age / 60)) 分钟），本轮跳过"
    exit 0
  fi
  log "⚠️ 检测到失效的锁（PID ${holder:-未知}，已 $((age / 60)) 分钟）—— 强制接管"
  rm -rf "$LOCK"
  mkdir "$LOCK" 2>/dev/null || { log "❌ 接管锁失败，本轮放弃"; exit 1; }
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK" 2>/dev/null || true' EXIT

log "──────── 采集轮次开始 ────────"

# 1) 浏览器不再由本脚本预热（2026-09-21 架构改造）。
#
# 采集改走「短生命周期 Worker」（app/services/browser_worker.py）：
# 浏览器**按需拉起、用完彻底关闭**，内存归还系统 —— 不再有常驻 Chrome。
#
# 原来这里的 launch_browser() 预热会启动一个**常驻**实例，与新模型直接冲突：
# Worker 每次都得先把它当"残留进程"清掉再重开（实测日志里那句
# 「发现 11 个残留进程占用 profile，先清理」说的就是它），白付一次启动开销。
#
# 一并删掉的还有"清理残留标签页" —— 每轮都是全新实例，标签页不会跨轮累积。
# 原来那一步是为了治常驻浏览器跨轮复用导致的卡顿，前提已不存在。
#
# 启动失败的自愈现在在 Worker 内部：启动超时会清理残留后重试；
# 借页失败（页面崩溃 / context 丢失）会自动换实例重试一次。

# 2) 轮转采集：反爬源单轮量小，靠高频轮次 + 游标覆盖全部显卡与 CPU 型号
#
# 必须收窄到显卡+CPU —— 三个源的配额都很有限，花在机箱电源上就挤掉了
# 日报真正要的型号。
#
# 配额按各源的实际抗压能力分别设定（环境变量 DIYPRICE_<源>_LIMIT）：
#   · 京东 2   —— 平台硬限制，连搜 2 次后必然限流；调大只是白等超时，不动
#   · 拼多多 6 —— 用默认值即可（当前被搜索级风控拦着，恢复后自动贡献）
#   · 闲鱼 15  —— 唯一稳定的源，从默认 6 上调。单型号约 30s，
#                一轮约 (15+2)×30s ≈ 8.5 分钟，远小于 1 小时的调度间隔。
#                2026-09-17 实测：显卡+CPU 共 88 个型号，
#                京东 2/轮 + 闲鱼 15/轮 ≈ 17 个/轮 → 约 5 轮轮完一遍。
#
# ⚠️ 不要再改成更密集的多轮并发 —— 昨晚「每 8 分钟一轮」的密集采集把拼多多的
#    搜索打进了风控（首页正常、搜索页返回空），恢复要按小时算。宁可慢，别被封。
log "轮转采集：jd/pdd/xianyu（仅显卡+CPU · 闲鱼配额 15）"

# 硬超时看门狗（**进程组级**）。
#
# 为什么必须有：采集走 CDP 驱动真实浏览器，一旦某个等待卡在网络 I/O 上
# （典型场景：采集途中 Mac 睡眠，CDP 的 WebSocket 断了但调用没有超时），
# 进程会**无限期挂着**。实测卡过 8.5 小时，期间：
#   · 占着单实例锁 → 后续轮次全被跳过
#   · 占着 SQLite 写事务 → 新采集 `database is locked`
# 正常一轮约 9~10 分钟，30 分钟足够宽裕。
#
# ⚠️ 2026-09-21 实测过"只对主进程 PID 发信号"的失败：
#   17:00 那轮卡了 3 小时 27 分，看门狗子 shell 明明执行完退出了，
#   子进程却还活着（而且对 SIGTERM 无响应）。原因未定位，但根因方向明确 ——
#   主进程不是唯一的受害者：它衍生的 Chrome、Playwright 的 node 驱动
#   都不受单一 PID 的信号影响，全都变成孤儿。
#
# 所以改成**按进程组广播**：
#   · `app.cli collect` 启动时会调 os.setpgrp() 自建进程组（见 cli.py），
#     浏览器用 start_new_session=False 继承该组，node 驱动同理
#   · 于是 `kill -- -PGID` 一次覆盖全树
MAX_COLLECT_SECONDS=1800
DIYPRICE_FOCUS_CATEGORY=gpu,cpu \
DIYPRICE_XIANYU_LIMIT=15 \
  "$PYTHON_BIN" -m app.cli collect --sources jd,pdd,xianyu >> "$LOG" 2>&1 &
COLLECT_PID=$!

# 看门狗必须在拿到 PID **之后**再 fork —— 子 shell 只会拿到 fork 那一刻的变量副本，
# 顺序写反的话它读到的永远是空值，看门狗就形同虚设。
(
  sleep "$MAX_COLLECT_SECONDS"

  # PGID **在这里现读**，不能沿用启动时读到的值：
  # Python 入口会在启动后调 os.setpgrp() 把自己变成新组的组长，
  # 启动瞬间读到的是旧组（= shell 自己的组），拿它去 kill 会**误杀自己**。
  pgid="$(ps -o pgid= -p "$COLLECT_PID" 2>/dev/null | tr -d ' ')"
  [ -z "$pgid" ] && exit 0                      # 进程早就正常结束了

  shell_pgid="$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ')"
  if [ "$pgid" = "$shell_pgid" ]; then
    # 兜底：进程组没分开（比如 setpgrp 失败）。此时按组广播会连自己一起杀，
    # 只能退回单 PID，并把这个事实记下来 —— 这是配置问题，不该静默。
    echo "[$(date '+%F %T')] ⚠️ 采集超过 ${MAX_COLLECT_SECONDS}s 仍未结束，但进程组未隔离（PGID=$pgid 与本脚本同组），只能按单 PID 终止" >> "$LOG"
    kill -TERM "$COLLECT_PID" 2>/dev/null
    sleep 5
    kill -KILL "$COLLECT_PID" 2>/dev/null
    exit 0
  fi

  echo "[$(date '+%F %T')] ⚠️ 采集超过 ${MAX_COLLECT_SECONDS}s 仍未结束（PID $COLLECT_PID / PGID $pgid），判定卡死，向**整个进程组**广播终止信号" >> "$LOG"

  # 第一步：软终止整个组（python + Chrome + node 驱动）
  kill -TERM -- "-$pgid" 2>/dev/null
  # 第二步：5 秒缓冲窗口
  sleep 5
  # 第三步：组内仍有活跃进程就直接硬杀
  if kill -0 -- "-$pgid" 2>/dev/null; then
    echo "[$(date '+%F %T')] ⚠️ 软终止 5s 后组内仍有存活进程，改为 SIGKILL 广播" >> "$LOG"
    kill -KILL -- "-$pgid" 2>/dev/null
  fi
) &
WATCHDOG_PID=$!

wait "$COLLECT_PID" || log "⚠️ 采集进程非正常退出（退出码 $?）"

# 收掉看门狗，别让它空等到超时
kill "$WATCHDOG_PID" 2>/dev/null || true
wait "$WATCHDOG_PID" 2>/dev/null || true

# 2b) 可选：对处于熔断退避期的源做一次轻量探活。
#
# 为什么默认关闭：这是**唯一会主动向平台发请求的新增行为**。探针有 10 分钟
# 最小间隔、只查 1 个型号，且失败不放大退避 —— 风险很低，但"往定时任务里
# 悄悄加平台请求"属于生产行为变更，不该由我替你默认打开。
#
# 打开方式（二选一）：
#   · 一次性：DIYPRICE_PROBE_ON_SCHEDULE=1 ./scripts/collect_scheduled.sh
#   · 长期：在 launchd plist 的 EnvironmentVariables 里加这个变量
#
# 打开后：退避中的源每轮最多被探一次；探活成功会立刻清掉退避、
# 让下一轮恢复正常调度（探针失败什么都不改）。
if [ "${DIYPRICE_PROBE_ON_SCHEDULE:-0}" = "1" ]; then
  log "探活：检查处于熔断退避期的源"
  "$PYTHON_BIN" -m app.cli probe >> "$LOG" 2>&1 || log "⚠️ 探针执行异常（不影响采集）"
fi

# 3) 汇总一行结果 + 覆盖进度，便于快速扫日志
"$PYTHON_BIN" - <<'PY' >> "$LOG" 2>&1
import sqlite3

con = sqlite3.connect("data/diyprice.db")
con.execute("PRAGMA busy_timeout=10000")

row = con.execute(
    "SELECT source, status, items FROM crawl_log ORDER BY id DESC LIMIT 3"
).fetchall()
print("本轮结果：" + " | ".join(f"{s}={st}({n}条)" for s, st, n in row))
# skipped 是设计行为不是故障 —— 该源在熔断退避期内，调度层直接跳过了它
if any(st == "skipped" for _, st, _ in row):
    print("  （skipped = 熔断退避期内被 Fast-Fail 跳过，非故障；"
          "查看：python -m app.cli breaker）")

# 覆盖进度：显卡+CPU 是采集范围，盯这两个品类的当日真实覆盖
total, covered = con.execute("""
    SELECT COUNT(*),
           SUM(CASE WHEN EXISTS (
                 SELECT 1 FROM listings l JOIN platforms pf ON pf.id = l.platform_id
                 WHERE l.product_id = p.id AND l.is_synthetic = 0
                   AND l.trade_date = date('now','localtime') AND pf.is_active = 1
               ) THEN 1 ELSE 0 END)
    FROM products p WHERE p.category IN ('gpu','cpu')
""").fetchone()
covered = covered or 0
left = total - covered
print(f"当日覆盖：显卡+CPU {covered}/{total}" + (f"（还差 {left} 个）" if left else "（已全覆盖）"))
PY

# 3b) 游标进度（单独读 JSON，避免在 SQL 里绕）
"$PYTHON_BIN" - <<'PY' >> "$LOG" 2>&1
import json
from pathlib import Path

p = Path("data/collect_cursor.json")
if p.exists():
    d = json.loads(p.read_text(encoding="utf-8"))
    print("游标：" + "  ".join(f"{k}={v.get('offset')}" for k, v in sorted(d.items())))
PY

log "──────── 采集轮次结束 ────────"
