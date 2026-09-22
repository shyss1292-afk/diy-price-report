#!/bin/bash
# 把「DIY 配件价格追踪」Web 服务包装成 macOS 独立应用。
#
# 为什么走 Chrome `--app` 模式而不是造 Chrome 官方 PWA shim：
# 官方 shim 由 app_mode_loader + Info.plist 模板组成，但它启动时要拿
# CrAppModeShortcutID 去 Chrome 的 **WebApp 数据库**里查记录 —— 命令行写不进那个库，
# 手工复制出来的 shim 一定报 "No suitable profile found." 起不来。
# 想要真正独立的 Dock 图标只能让 Chrome 自己装（⋮ → 投放、保存和分享 →
# 将网页作为应用安装…），那一步无法脚本化，只能用户手点。
#
# 用法：bash scripts/make_desktop_app.sh
set -euo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="配件价格追踪"
APP_DIR="$HOME/Applications/$APP_NAME.app"
ICON_ENV=/Users/apple/.workbuddy-ai/binaries/python/envs/default/bin/python
URL="http://127.0.0.1:8848"
LAUNCHD_LABEL="com.diyprice.tracker"

echo "==> 生成图标"
TMP_ICON="$(mktemp -d)"
"$ICON_ENV" "$PROJ/scripts/make_icon.py" "$TMP_ICON"

echo "==> 组装 $APP_DIR"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"
cp "$TMP_ICON/app.icns" "$APP_DIR/Contents/Resources/app.icns"

cat > "$APP_DIR/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>            <string>$APP_NAME</string>
    <key>CFBundleDisplayName</key>     <string>$APP_NAME</string>
    <key>CFBundleIdentifier</key>      <string>local.diyprice.desktop</string>
    <key>CFBundleVersion</key>         <string>1.0</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundleExecutable</key>      <string>launcher</string>
    <key>CFBundleIconFile</key>        <string>app.icns</string>
    <key>CFBundlePackageType</key>     <string>APPL</string>
    <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
    <key>CFBundleDevelopmentRegion</key><string>zh_CN</string>
    <key>LSMinimumSystemVersion</key>  <string>12.0</string>
    <key>NSHighResolutionCapable</key> <true/>
</dict>
</plist>
PLIST

cat > "$APP_DIR/Contents/MacOS/launcher" <<LAUNCHER
#!/bin/sh
# 服务由 launchd（${LAUNCHD_LABEL}）托管，登录后自动启动。
# 这里先等它就绪 —— 刚开机或服务刚重启时，直接开窗会看到"无法连接"。
URL="${URL}"

ready() { curl -s -m 2 -o /dev/null "\${URL}/api/health"; }

i=0
while [ \$i -lt 20 ]; do
  ready && break
  i=\$((i + 1))
  sleep 0.5
done

# 等了 10 秒还没起来，就主动踢一下 launchd 任务（服务被手动停过的情况）
if ! ready; then
  launchctl kickstart -k "gui/\$(id -u)/${LAUNCHD_LABEL}" >/dev/null 2>&1 || true
  i=0
  while [ \$i -lt 60 ]; do
    ready && break
    i=\$((i + 1))
    sleep 0.5
  done
fi

# --app 模式：无地址栏、无标签栏的独立窗口
#
# ⚠️ 必须给独立 profile。不给的话会复用默认 profile —— 此时若用户自己的 Chrome
#    正在运行，`--app` 请求会被**路由到那个已有实例**，新进程立刻退出。
#    后果有两个：① 应用窗口和用户的日常浏览挤在同一个 Chrome 实例里；
#    ② 无法用 "有没有 --app= 进程" 判断应用是否在运行。
#    本应用访问的是 localhost 看板，不需要任何登录态，独立 profile 零代价。
exec "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \\
  --app="\${URL}" \\
  --user-data-dir="\${HOME}/Library/Application Support/DIYPriceDesktop" \\
  --no-first-run \\
  --no-default-browser-check
LAUNCHER
chmod +x "$APP_DIR/Contents/MacOS/launcher"

echo "==> 注册到 LaunchServices + 桌面快捷方式"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP_DIR"
ln -sfn "$APP_DIR" "$HOME/Desktop/$APP_NAME"
touch "$APP_DIR"
killall Dock 2>/dev/null || true

rm -rf "$TMP_ICON"
echo
echo "完成：$APP_DIR"
echo "桌面快捷方式：$HOME/Desktop/$APP_NAME"
