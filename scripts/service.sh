#!/bin/bash
# DIY 配件价格追踪 —— 服务管理脚本
#
# 用法：
#   ./scripts/service.sh install     安装并启动后台常驻服务（开机自启）
#   ./scripts/service.sh uninstall   停止并卸载服务
#   ./scripts/service.sh start       启动服务
#   ./scripts/service.sh stop        停止服务
#   ./scripts/service.sh restart     重启服务
#   ./scripts/service.sh status      查看服务状态
#   ./scripts/service.sh logs        实时查看服务日志

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="/Users/apple/.workbuddy/binaries/python/envs/diyprice/bin/python"
LABEL="com.diyprice.tracker"
COLLECT_LABEL="com.diyprice.collect"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
PORT=8848
LOG_DIR="${PROJECT_DIR}/data"

mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"

write_plist() {
  cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON_BIN}</string>
        <string>-m</string>
        <string>app.cli</string>
        <string>serve</string>
        <string>--port</string>
        <string>${PORT}</string>
        <string>--no-scheduler</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${PROJECT_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <!-- Interactive：这是给用户直接打开页面的服务，必须跑在性能核上。
         曾经写成 Background，macOS 据此给低 QoS、把线程排到**能效核**，
         实测同一段代码比 shell 里慢 4~5 倍（M1：4 性能核 + 4 能效核）。
         接口 630ms vs 154ms 的差距就是这么来的，不是代码问题。 -->
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/service.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/service.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>LANG</key>
        <string>zh_CN.UTF-8</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
        <key>DIYPRICE_SCHEDULER</key>
        <string>0</string>
    </dict>
</dict>
</plist>
PLIST
}

# 采集调度改用 launchd（StartCalendarInterval）。
# 为什么不用进程内 APScheduler：Mac 睡眠后它会彻底停摆 ——
# 2026-09-16 夜里 23:30 之后所有任务都没再触发，凌晨与早上的采集全部丢失。
# launchd 睡醒后会把错过的任务补跑，这是它和进程内定时器的本质区别。
write_collect_plist() {
  local collect_plist="$HOME/Library/LaunchAgents/${COLLECT_LABEL}.plist"
  {
    cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${COLLECT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PROJECT_DIR}/scripts/collect_scheduled.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${PROJECT_DIR}</string>
    <key>StartCalendarInterval</key>
    <array>
PLIST
    # 每小时整点（02:00 顺带做当日全量打底）
    for h in 2 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23; do
      echo "        <dict><key>Hour</key><integer>${h}</integer><key>Minute</key><integer>0</integer></dict>"
    done
    cat <<PLIST
    </array>
    <key>RunAtLoad</key>
    <false/>
    <!-- 采集刻意保持 Background：它是批处理任务，主要时间在等网络，
         让它跑在能效核上，既不拖慢用户正在看的页面，也不影响采集本身。 -->
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/collect.launchd.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/collect.launchd.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>LANG</key>
        <string>zh_CN.UTF-8</string>
    </dict>
</dict>
</plist>
PLIST
  } > "$collect_plist"
  echo "$collect_plist"
}

install_schedule() {
  local collect_plist
  collect_plist="$(write_collect_plist)"
  launchctl bootout "gui/$(id -u)/${COLLECT_LABEL}" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$collect_plist" 2>/dev/null || launchctl load "$collect_plist"
  chmod +x "${PROJECT_DIR}/scripts/collect_scheduled.sh"
  echo "采集调度已安装（launchd，每小时一轮）：${collect_plist}"
  echo "采集日志：${LOG_DIR}/collect.log"
}

uninstall_schedule() {
  launchctl bootout "gui/$(id -u)/${COLLECT_LABEL}" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/${COLLECT_LABEL}.plist"
  echo "采集调度已移除"
}

unload_service() {
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || launchctl unload "$PLIST_PATH" 2>/dev/null || true
  # bootout 是异步的：进程还没退干净就 bootstrap，会报
  # "Load failed: 5: Input/output error"。这里等端口真正释放。
  for _ in $(seq 1 24); do
    lsof -ti:${PORT} >/dev/null 2>&1 || break
    sleep 0.5
  done
}

case "${1:-}" in
  install)
    unload_service
    write_plist
    launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || launchctl load "$PLIST_PATH"
    sleep 2
    echo "Web 服务已安装并启动：http://127.0.0.1:${PORT}"
    echo "配置文件：${PLIST_PATH}"
    echo "访问日志：${LOG_DIR}/service.log   应用日志：${LOG_DIR}/service.err.log"
    echo ""
    install_schedule
    ;;
  install-schedule)
    install_schedule
    ;;
  uninstall-schedule)
    uninstall_schedule
    ;;
  uninstall)
    unload_service
    rm -f "$PLIST_PATH"
    uninstall_schedule
    echo "服务已停止并移除"
    ;;
  start)
    launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || launchctl load "$PLIST_PATH"
    echo "服务已启动"
    ;;
  stop)
    unload_service
    echo "服务已停止"
    ;;
  restart)
    unload_service
    sleep 1
    launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || launchctl load "$PLIST_PATH"
    echo "服务已重启"
    ;;
  status)
    if launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
      echo "服务状态：运行中"
      launchctl print "gui/$(id -u)/${LABEL}" | grep -E "state|pid|last exit" | head -5 || true
    else
      echo "服务状态：未运行"
    fi
    echo ""
    echo "端口检查：$(lsof -ti:${PORT} >/dev/null 2>&1 && echo "127.0.0.1:${PORT} 已监听" || echo "127.0.0.1:${PORT} 未监听")"
    echo "健康检查：$(curl -s -m 2 "http://127.0.0.1:${PORT}/api/health" || echo "无响应")"
    ;;
  logs)
    tail -f "${LOG_DIR}/service.log"
    ;;
  *)
    echo "用法：$0 {install|uninstall|start|stop|restart|status|logs}"
    exit 1
    ;;
esac
