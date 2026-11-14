#!/bin/sh
set -u

event_name=${1:-}
target=${2:-}

# Pick the first interpreter that actually runs Python. Windows Store
# app-execution aliases (WindowsApps stubs) fail the probe and are skipped.
python_cmd=""
for candidate in python3 python py; do
  case "$candidate" in
    py) probe="py -3" ;;
    *) probe="$candidate" ;;
  esac
  if command -v "$candidate" >/dev/null 2>&1 \
    && $probe -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    python_cmd="$probe"
    break
  fi
done

if [ -n "$python_cmd" ]; then
  if [ -f "$target" ]; then
    exec $python_cmd "$target"
  fi
  printf '%s\n' '{"continue":true,"systemMessage":"HelixHarness Hook 未执行：插件内目标脚本不存在（runtime_target_missing）。"}'
  exit 0
fi

case "$event_name" in
  SessionStart)
    printf '%s\n' '{"continue":true,"systemMessage":"HelixHarness Python preflight 未通过：需要 Python 3.10+ 且 python3、python 或 py 至少其一在 PATH 中可用。运行 HelixHarness 命令时会提供受控安装选项。"}'
    ;;
  *)
    # Lifecycle hooks must stay silent when Python is unavailable. The command
    # preflight or SessionStart already emitted the actionable warning.
    ;;
esac

exit 0
