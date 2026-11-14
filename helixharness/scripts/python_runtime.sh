#!/bin/sh
# Sourced by the preflight and lifecycle bootstrap. Never consume hook stdin.
probe_helix_python() {
  # On Git Bash, never trigger the Microsoft Store's placeholder.
  case "$1" in
    *[[:cntrl:]]*|*/Microsoft/WindowsApps/*) return 1 ;;
  esac
  [ -f "$1" ] && [ -x "$1" ] || return 1
  if [ "$PYTHON_STATUS" = missing ]; then
    PYTHON_STATUS=unusable
    PYTHON_PROBE=$1
  fi
  python_version=$("$@" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' </dev/null 2>/dev/null) || return 1
  case "$python_version" in *[!0-9.]*|''|.*|*.|*..*) return 1 ;; esac
  python_major=${python_version%%.*}
  python_minor_patch=${python_version#*.}
  python_minor=${python_minor_patch%%.*}
  python_patch=${python_minor_patch#*.}
  [ "$python_minor_patch" != "$python_version" ] && [ "$python_patch" != "$python_minor_patch" ] || return 1
  case "$python_patch" in *.*) return 1 ;; esac
  if [ "$python_major" -gt 3 ] || { [ "$python_major" -eq 3 ] && [ "$python_minor" -ge 10 ]; }; then
    PYTHON_STATUS=ready
    PYTHON_VERSION=$python_version
    PYTHON_COMMAND=$1
    PYTHON_LAUNCHER=${2:-}
    PYTHON_PROBE=$1
    return 0
  fi
  if [ "$PYTHON_STATUS" != unsupported_version ]; then
    PYTHON_STATUS=unsupported_version
    PYTHON_VERSION=$python_version
    PYTHON_PROBE=$1
  fi
  return 1
}

probe_helix_python_dir() {
  for python_candidate in "$1/python3" "$1/python" "$1/python3.exe" "$1/python.exe" "$1"/python3.[0-9]*; do
    case "${python_candidate##*/}" in
      python3.[0-9]*)
        python_minor_name=${python_candidate##*/python3.}
        case "${python_minor_name%.exe}" in *[!0-9]*) continue ;; esac ;;
    esac
    probe_helix_python "$python_candidate" && return 0
  done
  for python_candidate in "$1/py" "$1/py.exe"; do
    probe_helix_python "$python_candidate" -3 && return 0
  done
  return 1
}

find_helix_python() {
  PYTHON_STATUS=missing
  PYTHON_VERSION=
  PYTHON_COMMAND=
  PYTHON_LAUNCHER=
  PYTHON_PROBE=
  # Split on PATH's delimiter only. Empty/relative entries are skipped so a
  # project cannot supply a cwd-dependent interpreter to automatic hooks.
  python_path=${PATH:-}
  while [ -n "$python_path" ]; do
    python_dir=${python_path%%:*}
    case "$python_dir" in
      /*) probe_helix_python_dir "$python_dir" && return 0 ;;
    esac
    [ "$python_dir" != "$python_path" ] || break
    python_path=${python_path#*:}
  done
  if [ "$(uname -s 2>/dev/null)" = Darwin ]; then
    # GUI hosts can omit Homebrew's bin from PATH. Probe the stable prefix
    # and formula symlinks without invoking brew or modifying the environment.
    for python_prefix in /opt/homebrew /usr/local; do
      for python_dir in "$python_prefix/bin" "$python_prefix"/opt/python@3.*/bin; do
        probe_helix_python_dir "$python_dir" && return 0
      done
    done
  fi
  return 1
}

exec_helix_python() {
  exec "$PYTHON_COMMAND" ${PYTHON_LAUNCHER:+"$PYTHON_LAUNCHER"} -X utf8 "$@"
}

# Discovered paths contain no control characters (see probe_helix_python).
helix_python_json_path() {
  if [ -z "$1" ]; then
    printf 'null'
    return
  fi
  printf '"'
  json_path=$1
  while [ -n "$json_path" ]; do
    json_char=${json_path%"${json_path#?}"}
    case "$json_char" in '"'|'\') printf '\\' ;; esac
    printf '%s' "$json_char"
    json_path=${json_path#?}
  done
  printf '"'
}
