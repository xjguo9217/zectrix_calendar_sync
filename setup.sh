#!/bin/bash
# 新电脑一键配置：装依赖 + 生成桌面按钮 + 触发系统授权。
# 重复跑是安全的。
set -euo pipefail

cd "$(dirname "$0")"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
warn() { printf '  \033[33m! %s\033[0m\n' "$*"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }

IS_MAC=no
[ "$(uname -s)" = "Darwin" ] && IS_MAC=yes

say "[1/5] 检查 Python"
if ! command -v python3 >/dev/null; then
    warn "没找到 python3。先装一个：https://www.python.org/downloads/"
    exit 1
fi
ok "$(python3 --version)"

say "[2/5] 建虚拟环境并装依赖"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
ok "依赖装好了"
if [ "$IS_MAC" = no ]; then
    warn "这不是 macOS：提醒事项同步用不了（EventKit 是 macOS 独有的）"
    warn "只能跑 CalDAV 日历 -> 墨水屏，记得在 .env 里设 CALENDAR_SOURCE=caldav"
fi

say "[3/5] 检查配置"
if [ ! -f .env ]; then
    cp .env.example .env
    warn "已生成 .env，请填进 API_KEY 和 DEVICE_ID 再继续："
    warn "  $PWD/.env"
    warn "（从老电脑直接把 .env 拷过来最省事，它不在 git 里）"
    exit 1
fi
# shellcheck disable=SC1091
set -a; source .env; set +a
if [ -z "${API_KEY:-}" ] || [ -z "${DEVICE_ID:-}" ]; then
    warn ".env 里 API_KEY / DEVICE_ID 还是空的，填完再跑一次这个脚本"
    exit 1
fi
ok "读到 API_KEY 和 DEVICE_ID"

if [ "$IS_MAC" = no ]; then
    say "完成"
    echo "  非 macOS，桌面按钮跳过。用 ./run.sh --dry-run 先试试。"
    exit 0
fi

say "[4/5] 生成桌面按钮"
./make_app.sh >/dev/null
ok "桌面上有「Zectrix 同步」了"

say "[5/5] 申请系统授权"
echo "  接下来会弹「提醒事项」和「日历」两个授权窗，都点允许。"
echo "  （这台电脑的授权要重新给一次，它不跟着 iCloud 同步）"
echo
.venv/bin/python3 sync_calendar.py --list-calendars || true

cat <<EOF

$(printf '\033[1m下一步\033[0m')

  1. 上面没列出日历/提醒事项列表的话，去开开关：
     系统设置 → 隐私与安全性 → 提醒事项  /  日历

  2. 工作日历（Google、umd.edu 之类）要先加账号：
     系统设置 → 互联网账户 → 添加 Google 账户，勾上日历
     然后再跑一次  ./run.sh --list-calendars  查名字

  3. 先空跑一次看看会发生什么：
     ./run.sh --dry-run

  4. 没问题了就双击桌面上的「Zectrix 同步」

$(printf '\033[1m注意\033[0m') 别把老电脑的 .sync_state.json 拷过来，让这台自己生成。
     提醒事项的本机编号每台 Mac 都不一样，配对会靠待办备注自动认回来。
EOF
