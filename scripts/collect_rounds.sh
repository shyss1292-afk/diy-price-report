#!/usr/bin/env bash
# 多轮轮转采集：反爬源单轮只能采少量型号，靠游标在轮次之间轮转覆盖全库。
# 用法：./scripts/collect_rounds.sh [轮数] [源] [轮间隔秒]
set -u
ROUNDS="${1:-10}"
SOURCES="${2:-pdd,xianyu,jd}"
GAP="${3:-20}"

cd "$(dirname "$0")/.."
# Python 解释器：统一解析（环境变量 → 项目内 venv → 系统 python3）。
# 原先硬编码作者本机路径，别人 clone 下来跑不了。
source "$(dirname "${BASH_SOURCE[0]:-$0}")/_python.sh"
PY="$PYTHON_BIN"
export DIYPRICE_FOCUS_CATEGORY="${DIYPRICE_FOCUS_CATEGORY:-gpu,cpu}"

echo "=== 多轮采集：${ROUNDS} 轮 / 源=${SOURCES} / 间隔=${GAP}s / 品类=${DIYPRICE_FOCUS_CATEGORY} ==="
START=$(date +%s)
for i in $(seq 1 "$ROUNDS"); do
  echo ""
  echo "────────── 第 ${i}/${ROUNDS} 轮  $(date '+%H:%M:%S') ──────────"
  $PY -m app.cli collect --sources "$SOURCES" 2>&1 | grep -E "解析到|采集完成|WARNING|0 条|success|failed|partial" || true
  if [ "$i" -lt "$ROUNDS" ]; then sleep "$GAP"; fi
done
echo ""
echo "=== 全部结束，耗时 $(( $(date +%s) - START )) 秒 ==="
