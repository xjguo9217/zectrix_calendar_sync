#!/bin/bash
# 生成「Zectrix 同步.app」并在桌面放一个替身。
# 换台电脑时把整个项目文件夹拷过去，再跑一次这个脚本就行。
set -euo pipefail

cd "$(dirname "$0")"

APP_NAME="Zectrix 同步"
APP="$APP_NAME.app"
BUNDLE_ID="com.zectrix.sync.launcher"

echo "==> 编译 $APP"
rm -rf "$APP"
osacompile -o "$APP" app/launcher.applescript

PLIST="$APP/Contents/Info.plist"

# osacompile 生成的 plist 里有的键有、有的没有，统一先删再加
set_plist() {   # set_plist <key> <type> <value>
    /usr/libexec/PlistBuddy -c "Delete :$1" "$PLIST" >/dev/null 2>&1 || true
    /usr/libexec/PlistBuddy -c "Add :$1 $2 $3" "$PLIST"
}

# 稳定的 bundle id：TCC（隐私授权）是按它记的，改了就要重新授权
set_plist CFBundleIdentifier string "$BUNDLE_ID"
set_plist CFBundleName string "$APP_NAME"

# 授权弹窗上显示的理由，覆盖掉 osacompile 那句机翻味儿的默认文案。
# macOS 14+ 认 FullAccess 那个键，老系统认前一个，两个都写。
REASON="同步「提醒事项」和 Zectrix 墨水屏便利贴"
set_plist NSRemindersUsageDescription string "$REASON"
set_plist NSRemindersFullAccessUsageDescription string "$REASON"

# 后台跑，不用在程序坞里占位
set_plist LSUIElement bool true

echo "==> 生成图标"
PYTHON=python3
[ -x .venv/bin/python3 ] && PYTHON=.venv/bin/python3
ICONSET="$(mktemp -d)/zectrix.iconset"
if "$PYTHON" app/make_icon.py "$ICONSET" 2>/dev/null; then
    iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/applet.icns"
    echo "    好了"
else
    echo "    跳过（缺 pyobjc，用默认图标）"
fi

# 自签名。未签名的 app 每次改动都会让系统把授权作废，签一下能稳一点。
echo "==> 签名"
codesign --force --deep --sign - "$APP" 2>/dev/null && echo "    好了" || echo "    跳过"

echo "==> 在桌面放替身"
DESKTOP="$HOME/Desktop"
osascript >/dev/null <<EOF
tell application "Finder"
    set appFile to POSIX file "$PWD/$APP" as alias
    set desktopFolder to POSIX file "$DESKTOP" as alias
    if exists file "$APP_NAME" of desktopFolder then
        delete file "$APP_NAME" of desktopFolder
    end if
    make new alias file at desktopFolder to appFile
    set name of result to "$APP_NAME"
end tell
EOF

cat <<EOF

搞定。桌面上现在有「$APP_NAME」，双击即可同步。

  · app 本体在：$PWD/$APP
  · 日志在：~/Library/Logs/zectrix-sync.log
  · 第一次双击会连着弹两个授权窗（提醒事项、通知），都点允许

注意：app 要和 sync_calendar.py 待在同一个文件夹里，桌面上那个是替身。
EOF
