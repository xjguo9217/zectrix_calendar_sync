#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

# 加载 .env 文件中的环境变量
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# 优先用项目自带的虚拟环境
if [ -x .venv/bin/python3 ]; then
    PYTHON=.venv/bin/python3
else
    PYTHON=python3
fi

# 把 --dry-run 之类的参数透传给脚本
exec "$PYTHON" sync_calendar.py "$@"
