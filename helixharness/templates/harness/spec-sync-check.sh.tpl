#!/usr/bin/env bash

# spec-sync-check.sh — specs / evidence 一致性提醒
# 从脚本所在位置解析项目根，不依赖宿主当前工作目录。

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
ACTIVE_DIR="$PROJECT_ROOT/docs/specs/active"
ACTIVE_COUNT=0

echo "=== HelixHarness spec-sync check ==="

if [ -d "$ACTIVE_DIR" ]; then
  for feature_dir in "$ACTIVE_DIR"/*; do
    [ -d "$feature_dir" ] || continue
    feature="$(basename "$feature_dir")"
    ACTIVE_COUNT=$((ACTIVE_COUNT + 1))
    if [ ! -f "$feature_dir/spec.md" ]; then
      echo "  ⚠️  $feature — spec.md 缺失，请确认是否需更新"
    fi
    # Workspace 根只保存概要设计；子仓由各自 linter 检查三工件。
    if [ -f "$PROJECT_ROOT/helixharness.workspace.yaml" ]; then
      continue
    fi
    if [ -f "$feature_dir/tasks.md" ]; then
      echo "  ✅ $feature — tasks.md 存在"
    else
      echo "  ⚠️  $feature — tasks.md 缺失，请确认是否需更新"
    fi
    # Propose 阶段尚未生成报告；只在 Spec 已实现或完成时检查 Evidence。
    spec_status=$(awk '
      NR == 1 { if ($0 !~ /^---\r?$/) exit; next }
      /^---\r?$/ { exit }
      /^status:[[:space:]]*/ {
        sub(/^status:[[:space:]]*/, ""); sub(/[[:space:]]+#.*$/, "")
        gsub(/["\047\r]/, ""); gsub(/[[:space:]]/, ""); print; exit
      }
    ' "$feature_dir/spec.md" 2>/dev/null)
    case "$spec_status" in
      implemented|completed)
        if [ ! -s "$feature_dir/verify-report.md" ]; then
          echo "  ⚠️  $feature — verify-report.md 缺失或为空，请确认验证证据"
        fi
        ;;
    esac
  done
fi

if [ "$ACTIVE_COUNT" -eq 0 ]; then
  echo "ℹ️  当前无活跃 feature"
else
  echo "💡 提示：按阶段核对 spec.md、tasks.md 与 verify-report.md；Workspace 根只维护 spec.md"
fi

echo "=== spec-sync check 完成 ==="
exit 0
