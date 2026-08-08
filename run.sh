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

# 把 --dry-run 之类的参数透传给脚本
exec "$PYTHON" sync_calendar.py "$@"
