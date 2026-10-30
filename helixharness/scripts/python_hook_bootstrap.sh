#!/bin/sh
set -u

event_name=${1:-}
target=${2:-}

if command -v python3 >/dev/null 2>&1 \
  && python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
  if [ -f "$target" ]; then
    exec python3 "$target"
  fi
  printf '%s\n' '{"continue":true,"systemMessage":"HelixHarness Hook 未执行：插件内目标脚本不存在（runtime_target_missing）。"}'
  exit 0
fi

case "$event_name" in
  SessionStart)
    printf '%s\n' '{"continue":true,"systemMessage":"HelixHarness Python preflight 未通过：需要 Python 3.10+ 且 python3 必须在 PATH 中。运行 HelixHarness 命令时会提供受控安装选项。"}'
    ;;
  *)
    # Lifecycle hooks must stay silent when Python is unavailable. The command
    # preflight or SessionStart already emitted the actionable warning.
    ;;
esac

exit 0
