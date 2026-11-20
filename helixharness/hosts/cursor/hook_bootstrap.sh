#!/bin/sh
# Cursor startup hooks use native response fields and never grant permissions.
set -u

event_name=${1:-}
case "$event_name" in
  sessionStart|sessionEnd|beforeSubmitPrompt|afterAgentResponse|postToolUse|postToolUseFailure|subagentStart|subagentStop|stop) ;;
  *) exit 0 ;;
esac

# Missing runtime must preserve the native permission-hook response shape.
# Other hook sources and Cursor's own approval policy remain authoritative.
neutral_response() {
  case "$event_name" in
    beforeSubmitPrompt) printf '%s\n' '{"continue":true}' ;;
    subagentStart) printf '%s\n' '{"permission":"allow"}' ;;
  esac
}

script_dir=${0%/*}
if [ "$script_dir" = "$0" ]; then
  script_dir=.
fi
script_dir=$(CDPATH= cd -P "$script_dir" && pwd) || exit 0
target="$script_dir/hook_entry.py"

# Keep the lightweight hook gate aligned with scripts/runtime_preflight.sh.
# Do not run the full command preflight on every latency-sensitive hook.
if command -v python3 >/dev/null 2>&1 \
  && python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
  if [ -f "$target" ]; then
    exec python3 "$target"
  fi
  if [ "$event_name" = sessionStart ]; then
    printf '%s\n' '{"additional_context":"HelixHarness Cursor hook unavailable: runtime_target_missing. Verify the current plugin installation before running a HelixHarness command."}'
  fi
  neutral_response
  exit 0
fi

if [ "$event_name" = sessionStart ]; then
  printf '%s\n' '{"additional_context":"HelixHarness requires Python 3.9+ with python3 on PATH. Its command runtime preflight must succeed before workflow execution."}'
fi
neutral_response
exit 0
