#!/bin/sh
set -u

event_name=${1:-}
target=${2:-}

. "$(dirname "$0")/python_runtime.sh"
find_helix_python

if [ "$PYTHON_STATUS" = ready ]; then
  if [ -f "$target" ]; then
    exec_helix_python "$target"
  fi
  printf '%s\n' '{"continue":true,"systemMessage":"HelixHarness Hook 未执行：插件内目标脚本不存在（runtime_target_missing）。"}'
  exit 0
fi

case "$event_name" in
  SessionStart)
    printf '%s\n' '{"continue":true,"systemMessage":"HelixHarness Python preflight 未通过：未找到可用的 Python 3.10+。请运行插件 runtime_preflight 的 check 查看探测路径与版本。"}'
    ;;
  *)
    # Lifecycle hooks must stay silent when Python is unavailable. The command
    # preflight or SessionStart already emitted the actionable warning.
    ;;
esac

exit 0
