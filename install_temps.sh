#!/bin/bash
# 部署「大V温度提取」launchd：克隆/更新仓库到 HOME（TCC 要求，入口不能在 Downloads），
# 生成并加载 com.user.zhishuguzhi-temps（每天 09:50 与 12:00 二次兜底）。
# 用法：bash install_temps.sh
set -e

DEST="$HOME/zhishuguzhi"
PLIST="$HOME/Library/LaunchAgents/com.user.zhishuguzhi-temps.plist"
LABEL="com.user.zhishuguzhi-temps"
REPO_URL="https://github.com/bao1956/zhishuguzhi.git"

if [ -d "$DEST/.git" ]; then
  echo "更新已有克隆: $DEST"
  git -C "$DEST" pull --rebase --autostash
else
  echo "克隆到: $DEST"
  git clone "$REPO_URL" "$DEST"
fi

# launchd 直接 exec 真实 python 二进制（不经 bash），TCC「责任进程」才是被授 FDA 的那个
PYREAL="$(readlink -f "$(/usr/bin/python3 -c 'import sys; print(sys.executable)')")"
echo "运行用 python: $PYREAL"

echo "生成 launchd: $PLIST"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYREAL</string>
        <string>$DEST/extract_temps.py</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>$DEST</string>
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>50</integer></dict>
        <dict><key>Hour</key><integer>12</integer><key>Minute</key><integer>0</integer></dict>
    </array>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>$DEST/temps_run.log</string>
    <key>StandardErrorPath</key>
    <string>$DEST/temps_run.log</string>
</dict>
</plist>
PLISTEOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "✅ launchd 已加载（每天 09:50 / 12:00 运行，日志 $DEST/temps_run.log）"
