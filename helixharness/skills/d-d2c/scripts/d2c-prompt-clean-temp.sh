#!/usr/bin/env bash
# 深度自检等流程结束后：询问是否「结束本轮」；若确认，则清空指定目录下临时产物（默认 d-d2c/temp）。
# 用法：bash $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-prompt-clean-temp.sh [--out d-d2c/temp 或 d-d2c/temp/<页面子目录>]
# 说明：请在已确认叠图 / diff / 报告无需再保留后再选 y；重要结论请先写入 MR 或交付说明。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT_REL="d-d2c/temp"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out)
      OUT_REL="$2"
      shift 2
      ;;
    -h|--help)
      echo "Usage: bash $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-prompt-clean-temp.sh [--out d-d2c/temp|d-d2c/temp/<子目录>]"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

if [[ "$OUT_REL" = /* ]]; then
  TARGET="$OUT_REL"
else
  TARGET="$ROOT/$OUT_REL"
fi

if [[ ! -d "$TARGET" ]]; then
  echo "目录不存在，无需清理：$TARGET"
  exit 0
fi

CANON="$(cd "$TARGET" && pwd)"
if [[ "$CANON" != "$ROOT"/* ]]; then
  echo "Refusing: not under repository root: $CANON" >&2
  exit 1
fi

# 仅允许清理 d-d2c/temp 及其子目录，防止误传路径
temp_root="$ROOT/d-d2c/temp"
if [[ "$CANON" != "$temp_root" && "$CANON" != "$temp_root/"* ]]; then
  echo "Refusing: path must be under d-d2c/temp. Got: $CANON" >&2
  echo "Hint: default or --out d-d2c/temp / d-d2c/temp/<页面子目录>" >&2
  exit 1
fi

count="$(find "$CANON" -mindepth 1 | wc -l | tr -d ' ')"
if [[ "${count:-0}" -eq 0 ]]; then
  echo "目录已为空：$CANON"
  exit 0
fi

echo "假定初检 + 深度自检（叠图等）已按流程完成。"
read -r -p "是否结束本轮并删除「${CANON}」下的全部临时产物？[y/N] " reply
reply_lc="$(printf '%s' "${reply:-}" | tr '[:upper:]' '[:lower:]')"
case "$reply_lc" in
  y|yes)
    find "$CANON" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
    echo "已删除 ${CANON} 下的临时文件（目录本身保留）。"
    ;;
  *)
    echo "已取消，未删除任何文件。"
    ;;
esac
