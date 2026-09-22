#!/bin/bash
# 把「DIY 配件价格追踪」Web 看板包成 macOS 独立应用（**系统原生 WebView 版**）。
#
# 为什么不用 Chrome `--app` 套壳：
#   Chrome 会拉起一个**完整的 Chrome 实例**，实测吃 **660~783 MB**
#   （sync、组件更新、Safe Browsing、后台联网全在跑）。
#   在 8G 机器上还要和 WorkBuddy、日常浏览器分内存 —— 这就是"卡"的来源。
#   系统 WebKit 的 XPC 服务只要 ~145 MB，加上主进程共 **~211 MB（-73%）**。
#   本项目是**本地看板**，不需要 Chrome 的任何特性（扩展/同步/DevTools），
#   没理由为它养一个完整浏览器。
#
# 为什么是 Objective-C 而不是 Swift：
#   本机 CommandLineTools 坏了 —— usr/include/swift/ 下同时存在
#   module.modulemap(2023) 与 bridging.modulemap(2024)，两者都定义
#   `SwiftBridging`，swiftc 直接报 "redefinition of module"。
#   那是 root 拥有的系统文件，不动它；clang 不加载 Swift 的 modulemap，绕开。
#   （要修的话：sudo rm -rf /Library/Developer/CommandLineTools && xcode-select --install）
#
# 用法：bash scripts/make_desktop_app.sh
set -euo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="配件价格追踪"
APP_DIR="$HOME/Applications/$APP_NAME.app"
ICON_PY=/Users/apple/.workbuddy-ai/binaries/python/envs/default/bin/python
SRC="$PROJ/scripts/webview_app.m"

echo "==> 编译原生应用"
if ! command -v clang >/dev/null; then
  echo "❌ 找不到 clang（需要 Xcode 命令行工具）" >&2
  exit 1
fi
BIN="$(mktemp -d)/$APP_NAME"
clang -fobjc-arc -O2 -framework Cocoa -framework WebKit -o "$BIN" "$SRC"

echo "==> 生成图标"
ICON_DIR="$(mktemp -d)"
"$ICON_PY" "$PROJ/scripts/make_icon.py" "$ICON_DIR" >/dev/null

echo "==> 组装 $APP_DIR"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"
cp "$BIN" "$APP_DIR/Contents/MacOS/$APP_NAME"
chmod +x "$APP_DIR/Contents/MacOS/$APP_NAME"
cp "$ICON_DIR/app.icns" "$APP_DIR/Contents/Resources/app.icns"

cat > "$APP_DIR/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>             <string>$APP_NAME</string>
    <key>CFBundleDisplayName</key>      <string>$APP_NAME</string>
    <key>CFBundleIdentifier</key>       <string>local.diyprice.desktop</string>
    <key>CFBundleVersion</key>          <string>2.0</string>
    <key>CFBundleShortVersionString</key><string>2.0</string>
    <key>CFBundleExecutable</key>       <string>$APP_NAME</string>
    <key>CFBundleIconFile</key>         <string>app.icns</string>
    <key>CFBundlePackageType</key>      <string>APPL</string>
    <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
    <key>CFBundleDevelopmentRegion</key><string>zh_CN</string>
    <key>LSMinimumSystemVersion</key>   <string>12.0</string>
    <key>NSHighResolutionCapable</key>  <true/>
    <key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
</dict>
</plist>
PLIST

plutil -lint "$APP_DIR/Contents/Info.plist" >/dev/null

echo "==> 注册到 LaunchServices + 桌面快捷方式"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP_DIR"
ln -sfn "$APP_DIR" "$HOME/Desktop/$APP_NAME"
touch "$APP_DIR"
killall Dock 2>/dev/null || true

echo
echo "完成：$APP_DIR"
echo "桌面快捷方式：$HOME/Desktop/$APP_NAME"
