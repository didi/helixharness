#!/bin/sh
set -u

event_name=${1:-}
target=${2:-}

. "$(dirname "$0")/python_runtime.sh"
find_helix_python

if [ "$PYTHON_STATUS" = ready ]; then
  if [ -f "$target" ]; then
    exec "$PYTHON_COMMAND" ${PYTHON_LAUNCHER:+"$PYTHON_LAUNCHER"} -X utf8 "$target"
  fi
  printf '%s\n' '{"continue":true,"systemMessage":"HelixHarness Hook 未执行：插件内目标脚本不存在（runtime_target_missing）。"}'
  exit 0
fi

case "$event_name" in
  SessionStart)
    printf '%s\n' '{"continue":true,"systemMessage":"HelixHarness Python preflight 未通过：需要 Python 3.10+ 且 python3、python 或 py -3 必须可用。运行 HelixHarness 命令时会提供受控安装选项。"}'
    ;;
  *)
    # Lifecycle hooks must stay silent when Python is unavailable. The command
    # preflight or SessionStart already emitted the actionable warning.
    ;;
esac

exit 0
