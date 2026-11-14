#!/bin/sh
# Sourced by the preflight and lifecycle bootstrap. Never consume hook stdin.
find_helix_python() {
  PYTHON_STATUS=missing
  PYTHON_VERSION=
  PYTHON_COMMAND=
  PYTHON_LAUNCHER=
  for candidate in python3 python py; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    launcher=
    [ "$candidate" != py ] || launcher=-3
    version=$("$candidate" $launcher -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' </dev/null 2>/dev/null) || {
      [ "$PYTHON_STATUS" != missing ] || PYTHON_STATUS=unusable
      continue
    }
    case "$version" in *[!0-9.]*|''|.*) continue ;; esac
    major=${version%%.*}
    minor=${version#*.}; minor=${minor%%.*}
    if [ "$major" -gt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -ge 10 ]; }; then
      PYTHON_STATUS=ready
      PYTHON_VERSION=$version
      PYTHON_COMMAND=$candidate
      PYTHON_LAUNCHER=$launcher
      return
    fi
    PYTHON_STATUS=unsupported_version
    PYTHON_VERSION=$version
  done
}
