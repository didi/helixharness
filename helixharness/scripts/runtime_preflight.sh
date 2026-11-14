#!/bin/sh
set -u

MINIMUM_PYTHON="3.10"

probe_python_version() {
  # Prints the interpreter version only when the candidate runs a real Python.
  # Windows Store app-execution aliases (WindowsApps stubs) fail this probe and
  # are skipped in favour of the next candidate.
  case "$1" in
    py)
      command -v py >/dev/null 2>&1 || return 1
      py -3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null
      ;;
    *)
      command -v "$1" >/dev/null 2>&1 || return 1
      "$1" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null
      ;;
  esac
}

candidate_command() {
  case "$1" in
    py) printf '%s' "py -3" ;;
    *) printf '%s' "$1" ;;
  esac
}

python_status() {
  PYTHON_STATUS="missing"
  PYTHON_VERSION=""
  PYTHON_COMMAND=""
  found_any=false
  probed_version=""
  probed_command=""
  for candidate in python3 python py; do
    version="$(probe_python_version "$candidate" || true)"
    case "$version" in
      [0-9]*.[0-9]*.[0-9]*)
        if [ -z "$probed_version" ]; then
          probed_version="$version"
          probed_command="$(candidate_command "$candidate")"
        fi
        old_ifs=$IFS
        IFS=.
        set -- $version
        IFS=$old_ifs
        if [ "$1" -gt 3 ] || { [ "$1" -eq 3 ] && [ "$2" -ge 10 ]; }; then
          PYTHON_STATUS="ready"
          PYTHON_VERSION="$version"
          PYTHON_COMMAND="$(candidate_command "$candidate")"
          return
        fi
        ;;
      *)
        if command -v "$candidate" >/dev/null 2>&1; then
          found_any=true
        fi
        ;;
    esac
  done
  if [ -n "$probed_version" ]; then
    PYTHON_STATUS="unsupported_version"
    PYTHON_VERSION="$probed_version"
    PYTHON_COMMAND="$probed_command"
  elif [ "$found_any" = "true" ]; then
    PYTHON_STATUS="unusable"
  fi
}

git_status() {
  if command -v git >/dev/null 2>&1; then
    GIT_STATUS="ready"
  else
    GIT_STATUS="missing"
  fi
}

emit_check() {
  python_status
  git_status
  if [ "$GIT_STATUS" != "ready" ]; then
    ok=false
    next_action="install_git"
  elif [ "$PYTHON_STATUS" != "ready" ]; then
    ok=false
    next_action="install_python_or_disable_telemetry"
  else
    ok=true
    next_action="continue"
  fi
  printf '{"schema_version":"1.0","ok":%s,"python":{"status":"%s","minimum_version":"%s","version":%s,"command":%s},"git":{"status":"%s"},"next_action":"%s"}\n' \
    "$ok" "$PYTHON_STATUS" "$MINIMUM_PYTHON" \
    "$(if [ -n "$PYTHON_VERSION" ]; then printf '"%s"' "$PYTHON_VERSION"; else printf 'null'; fi)" \
    "$(if [ -n "$PYTHON_COMMAND" ]; then printf '"%s"' "$PYTHON_COMMAND"; else printf 'null'; fi)" \
    "$GIT_STATUS" "$next_action"
}

emit_install_error() {
  code=$1
  message=$2
  printf '{"schema_version":"1.0","ok":false,"installed":false,"error_code":"%s","message":"%s"}\n' \
    "$code" "$message"
}

run_as_root() {
  if [ "$(id -u 2>/dev/null || printf '1')" = "0" ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    return 126
  fi
}

install_python() {
  if [ "${1:-}" != "--user-confirmed" ]; then
    emit_install_error \
      "python_install_confirmation_required" \
      "安装 Python 会修改系统环境，必须由用户明确确认。"
    return 2
  fi

  python_status
  if [ "$PYTHON_STATUS" = "ready" ]; then
    printf '{"schema_version":"1.0","ok":true,"installed":false,"python":{"status":"ready","minimum_version":"%s","version":"%s","command":"%s"},"next_action":"continue"}\n' \
      "$MINIMUM_PYTHON" "$PYTHON_VERSION" "$PYTHON_COMMAND"
    return 0
  fi

  os_name="$(uname -s 2>/dev/null || printf 'unknown')"
  install_result=0
  case "$os_name" in
    Darwin)
      if command -v brew >/dev/null 2>&1; then
        brew install python >&2 || install_result=$?
      else
        emit_install_error \
          "python_package_manager_unavailable" \
          "未找到 Homebrew；请先安装 Homebrew 或手工安装 Python 3.10+。"
        return 1
      fi
      ;;
    Linux)
      if command -v apt-get >/dev/null 2>&1; then
        run_as_root apt-get update >&2 || install_result=$?
        if [ "$install_result" -eq 0 ]; then
          run_as_root apt-get install -y python3 >&2 || install_result=$?
        fi
      elif command -v dnf >/dev/null 2>&1; then
        run_as_root dnf install -y python3 >&2 || install_result=$?
      elif command -v yum >/dev/null 2>&1; then
        run_as_root yum install -y python3 >&2 || install_result=$?
      elif command -v apk >/dev/null 2>&1; then
        run_as_root apk add python3 >&2 || install_result=$?
      elif command -v pacman >/dev/null 2>&1; then
        run_as_root pacman -S --noconfirm python >&2 || install_result=$?
      elif command -v zypper >/dev/null 2>&1; then
        run_as_root zypper --non-interactive install python3 >&2 || install_result=$?
      else
        emit_install_error \
          "python_package_manager_unavailable" \
          "未找到受支持的系统包管理器；请手工安装 Python 3.10+。"
        return 1
      fi
      ;;
    MINGW*|MSYS*|CYGWIN*)
      if command -v winget.exe >/dev/null 2>&1; then
        winget.exe install --id Python.Python.3.12 -e \
          --accept-package-agreements --accept-source-agreements >&2 \
          || install_result=$?
      else
        emit_install_error \
          "python_package_manager_unavailable" \
          "未找到 winget；请手工安装 Python 3.10+ 并确保 python3 在 PATH 中。"
        return 1
      fi
      ;;
    *)
      emit_install_error \
        "python_install_platform_unsupported" \
        "当前系统不支持自动安装；请手工安装 Python 3.10+。"
      return 1
      ;;
  esac

  if [ "$install_result" -ne 0 ]; then
    emit_install_error \
      "python_install_failed" \
      "系统包管理器安装 Python 失败；统计保持关闭。"
    return 1
  fi

  hash -r 2>/dev/null || true
  python_status
  if [ "$PYTHON_STATUS" != "ready" ]; then
    emit_install_error \
      "python_install_incomplete" \
      "安装命令已完成，但当前 PATH 中仍没有 Python 3.10+；统计保持关闭。"
    return 1
  fi
  printf '{"schema_version":"1.0","ok":true,"installed":true,"python":{"status":"ready","minimum_version":"%s","version":"%s","command":"%s"},"next_action":"continue"}\n' \
    "$MINIMUM_PYTHON" "$PYTHON_VERSION" "$PYTHON_COMMAND"
}

case "${1:-}" in
  check)
    emit_check
    ;;
  install-python)
    shift
    install_python "${1:-}"
    ;;
  *)
    printf '%s\n' 'usage: runtime_preflight.sh check | install-python --user-confirmed' >&2
    exit 2
    ;;
esac
