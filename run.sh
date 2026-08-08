#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

# .env 不在这里 source —— 交给脚本里的 python-dotenv。
# 它的语义是「已存在的环境变量优先」，所以临时试参数才好使：
#     INCLUDE_UNDATED=1 SYNC_DAYS_AHEAD=7 ./run.sh --dry-run
# 从前这里 source .env，会把命令行临时指定的值直接覆盖掉。

# 优先用项目自带的虚拟环境
if [ -x .venv/bin/python3 ]; then
    PYTHON=.venv/bin/python3
else
    PYTHON=python3
fi

# 不管从终端跑还是被桌面按钮调起来，都留一份日志。
# 以前只有按钮写日志，终端跑完关掉窗口就什么都查不到了。
LOG="$HOME/Library/Logs/zectrix-sync.log"
mkdir -p "$(dirname "$LOG")"

# 简单轮转：超过 2MB 就只留最后 2000 行
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 2097152 ]; then
    tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

# 老版本的桌面按钮会把整个输出重定向到同一个日志文件，那种情况下再 tee
# 一遍就会每次记两份。问 lsof 要 fd 1 的真实路径来判断。
#
# 不用 stat 比 inode：macOS 上 /dev/stdout 是个 devfs 节点，
# stat 出来的设备号/inode 跟真实文件对不上，加 -L 也一样。
#
# 之所以绕这一下而不是直接重新生成 app：重新签名会改变 cdhash，
# 系统会把已经授予的隐私权限作废，得重新点一遍授权窗。
canon() {
    d=$(dirname "$1"); b=$(basename "$1")
    (cd "$d" 2>/dev/null && printf '%s/%s\n' "$(pwd -P)" "$b") || printf '%s\n' "$1"
}

already_logging=no
fd1=$(lsof -p $$ -a -d 1 -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)
if [ -n "$fd1" ] && [ "$(canon "$fd1")" = "$(canon "$LOG")" ]; then
    already_logging=yes
fi

set -o pipefail
if [ "$already_logging" = yes ]; then
    printf '=== %s  %s ===\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$* "
    "$PYTHON" sync_calendar.py "$@" 2>&1
    code=$?
else
    printf '=== %s  %s ===\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$* " >> "$LOG"
    # 屏幕和日志各一份。pipefail + PIPESTATUS 保证拿到的是 python 的退出码，
    # 不是 tee 的。
    "$PYTHON" sync_calendar.py "$@" 2>&1 | tee -a "$LOG"
    code=${PIPESTATUS[0]}
    printf 'EXITCODE:%s\n' "$code" >> "$LOG"
fi
exit "$code"
