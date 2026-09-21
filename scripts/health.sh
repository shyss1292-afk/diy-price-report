#!/usr/bin/env bash
# 一键体检：服务 / 浏览器 / 数据库 / 系统负载
#
# 用法：
#   ./scripts/health.sh          只体检，不改动任何东西
#   ./scripts/health.sh --fix    顺带清理：收掉残留标签页 + 收缩 WAL
#
# 为什么需要它
# ------------
# "网页滑动卡顿、不跟手" 这类问题，根因常常**不在网页本身**，
# 而在浏览器进程状态或系统负载。此脚本把这些指标一次摆出来，避免瞎猜。
set -u

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

PYTHON_BIN="/Users/apple/.workbuddy/binaries/python/envs/diyprice/bin/python"
PORT=8848
FIX=0
[ "${1:-}" = "--fix" ] && FIX=1

line() { printf '%s\n' "---------------------------------------------------------------"; }

line
echo "体检时间  $(date '+%Y-%m-%d %H:%M:%S')   （$(pmset -g batt 2>/dev/null | head -1 | grep -o "'.*'")）"
line

# ---------------------------------------------------------------- 服务
echo "【Web 服务】"
if curl -s -m 5 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
  echo "  ✅ 运行中  http://127.0.0.1:${PORT}  pid=$(lsof -ti:${PORT} 2>/dev/null | head -1)"
  for ep in "/api/overview?days=180&period=7" "/api/daily-report?category=gpu"; do
    t=$(curl -s -o /dev/null -w '%{time_starttransfer}' -m 60 "http://127.0.0.1:${PORT}${ep}")
    printf "     首字节 %6.3fs   %s\n" "$t" "$ep"
  done
else
  echo "  ❌ 无响应 —— 试 ./scripts/service.sh restart"
fi

# ---------------------------------------------------------------- 浏览器
echo ""
echo "【采集浏览器】"
BPID=$(pgrep -f "remote-debugging-port=9222" | head -1 || true)
if [ -z "$BPID" ]; then
  echo "  ⚠️  未运行（下一轮采集会自动拉起）"
else
  BCPU=$(ps aux | grep "[G]oogle Chrome" | awk '{s+=$3} END {printf "%.0f", s}')
  BMEM=$(ps aux | grep "[G]oogle Chrome" | awk '{s+=$6} END {printf "%.0f", s/1024}')
  BPROC=$(ps aux | grep -c "[G]oogle Chrome")
  echo "  pid=${BPID}  进程 ${BPROC} 个  内存 ${BMEM}MB  总 CPU ${BCPU}%"
  echo "  （空闲时应接近 0%；持续偏高说明有页面在空转，跑 --fix）"
  "$PYTHON_BIN" - <<'PY' 2>/dev/null
from playwright.sync_api import sync_playwright
from app.services.session import cdp_url
try:
    with sync_playwright() as pw:
        ctx = pw.chromium.connect_over_cdp(cdp_url()).contexts[0]
        tabs = ctx.pages
        print(f"  标签页 {len(tabs)} 个" + ("  ← 偏多，建议 --fix 清理" if len(tabs) > 2 else "  ✅"))
        for t in tabs[:6]:
            print(f"     · {t.url[:76]}")
except Exception as exc:
    print(f"  ⚠️  CDP 连接失败：{str(exc)[:90]}")
PY
fi

# ---------------------------------------------------------------- 数据库
echo ""
echo "【数据库】"
if [ -f data/diyprice.db ]; then
  printf "   主库 %6s   WAL %6s\n" \
    "$(ls -lh data/diyprice.db | awk '{print $5}')" \
    "$(ls -lh data/diyprice.db-wal 2>/dev/null | awk '{print $5}')"
  "$PYTHON_BIN" - <<'PY' 2>/dev/null
import sqlite3
con = sqlite3.connect("data/diyprice.db")
con.execute("PRAGMA busy_timeout=8000")
last = con.execute("SELECT MAX(trade_date) FROM listings").fetchone()[0]
real = con.execute("SELECT COUNT(*) FROM listings WHERE is_synthetic=0").fetchone()[0]
print(f"   最新数据日期 {last}   真实明细 {real:,} 条")
PY
  WALSZ=$(stat -f%z data/diyprice.db-wal 2>/dev/null || echo 0)
  if [ "$WALSZ" -gt 20971520 ]; then
    echo "   ⚠️  WAL 超过 20MB（占位空间过大，建议跑 --fix 收缩）"
  fi
fi

# ---------------------------------------------------------------- 系统
echo ""
echo "【系统负载】"
uptime | sed 's/^/   /'
echo "   CPU 占用前 6 名："
ps -Ao %cpu,comm | awk 'NR>1' | sort -rn | head -6 | \
  awk '{n=split($2,a,"/"); printf "     %5s%%  %s\n", $1, a[n]}'

echo ""
echo "   提示：若 WindowServer / mds（Spotlight）/ 某个 Electron 进程占用很高，"
echo "         系统级输入延迟会让**所有**页面都显得卡，与网页代码无关。"

# ---------------------------------------------------------------- 修复
if [ "$FIX" = "1" ]; then
  echo ""
  line
  echo "【执行清理】"
  if [ -n "$BPID" ]; then
    "$PYTHON_BIN" - <<'PY' 2>&1 | sed 's/^/   /'
from playwright.sync_api import sync_playwright
from app.services.session import cdp_url
try:
    with sync_playwright() as pw:
        ctx = pw.chromium.connect_over_cdp(cdp_url()).contexts[0]
        extra = ctx.pages[1:]
        for page in extra:
            page.close()
        print(f"关闭残留标签页 {len(extra)} 个")
except Exception as exc:
    print(f"标签页清理跳过：{str(exc)[:110]}")
PY
  fi
  "$PYTHON_BIN" - <<'PY' 2>&1 | sed 's/^/   /'
import sqlite3
con = sqlite3.connect("data/diyprice.db")
con.execute("PRAGMA busy_timeout=15000")
busy, log, ckpt = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
print(f"WAL 收缩完成（待处理帧 {log}）")
PY
  echo "   ✅ 清理结束。若浏览器仍偏慢，再执行："
  echo "      ./scripts/service.sh restart   （会重启服务，浏览器需下一轮采集自动拉起）"
fi

echo ""
line
echo "常用命令"
echo "   ./scripts/service.sh restart     重启 Web 服务"
echo "   ./scripts/health.sh --fix        清理标签页 + 收缩 WAL"
echo "   lsof -nP -iTCP:9222 -sTCP:LISTEN 看调试端口在 IPv4 还是 IPv6"
line
