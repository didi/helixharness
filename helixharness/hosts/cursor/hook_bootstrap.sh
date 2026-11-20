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

# Share the command runtime's 3.10+ interpreter search without running the
# full preflight on latency-sensitive hooks. Probes never consume hook stdin.
runtime_library="$script_dir/../../scripts/python_runtime.sh"
if [ -f "$runtime_library" ]; then
  . "$runtime_library"
  find_helix_python
else
  PYTHON_STATUS=missing
fi
if [ "$PYTHON_STATUS" = ready ]; then
  if [ -f "$target" ]; then
    exec_helix_python "$target"
  fi
  if [ "$event_name" = sessionStart ]; then
    printf '%s\n' '{"additional_context":"HelixHarness Cursor hook unavailable: runtime_target_missing. Verify the current plugin installation before running a HelixHarness command."}'
  fi
  neutral_response
  exit 0
fi

if [ "$event_name" = sessionStart ]; then
  printf '%s\n' '{"additional_context":"HelixHarness requires Python 3.10+. No supported interpreter was found by its runtime resolver. Its command runtime preflight must succeed before workflow execution."}'
fi
neutral_response
exit 0
