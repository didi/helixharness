#!/bin/sh
set -eu
LC_ALL=C
LANG=C
export LC_ALL LANG

plugin_root=
command_name=
resources=
state_root=
action=prepare
invocation_key=
packet_round=
page_index=

blocked() {
  printf '{"status":"blocked","code":"helixharness_host_contract_invalid","reason":"%s"}\n' "$1"
  exit 2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --plugin-root) [ "$#" -ge 2 ] || blocked invalid_loader_argument; plugin_root=$2; shift 2 ;;
    --command) [ "$#" -ge 2 ] || blocked invalid_loader_argument; command_name=$2; shift 2 ;;
    --resource)
      [ "$#" -ge 2 ] || blocked invalid_loader_argument
      case " $resources " in *" $2 "*) blocked duplicate_adapter_resource ;; esac
      resources="$resources $2"
      shift 2
      ;;
    --state-root) [ "$#" -ge 2 ] || blocked invalid_loader_argument; state_root=$2; shift 2 ;;
    --read-page) [ "$action" = prepare ] || blocked invalid_loader_argument; action=read; shift ;;
    --finish) [ "$action" = prepare ] || blocked invalid_loader_argument; action=finish; shift ;;
    --invocation-key) [ "$#" -ge 2 ] || blocked invalid_loader_argument; invocation_key=$2; shift 2 ;;
    --packet-round) [ "$#" -ge 2 ] || blocked invalid_loader_argument; packet_round=$2; shift 2 ;;
    --page-index) [ "$#" -ge 2 ] || blocked invalid_loader_argument; page_index=$2; shift 2 ;;
    *) blocked invalid_loader_argument ;;
  esac
done

[ -n "$plugin_root" ] || blocked invalid_loader_argument
plugin_root=$(cd "$plugin_root" && pwd -P) || blocked invalid_plugin_root
if [ -z "$state_root" ]; then
  state_root=${TMPDIR:-/tmp}/helixharness-codex-entry-state-v1-$(id -u)
fi
[ ! -L "$state_root" ] || blocked entry_state_root_must_not_be_a_symlink
umask 077
[ ! -e "$state_root" ] || [ -d "$state_root" ] || blocked entry_state_root_is_invalid
mkdir -p "$state_root" || blocked entry_state_root_is_invalid
state_owner=$state_root/.helixharness-owner
if [ ! -e "$state_owner" ] && [ ! -L "$state_owner" ]; then
  if [ -n "$(ls -A "$state_root")" ]; then
    sleep 1
  fi
  if [ ! -e "$state_owner" ] && [ ! -L "$state_owner" ]; then
    remaining=$(ls -A "$state_root" | sed '/^\.helixharness-owner\.tmp\./d')
    [ -z "$remaining" ] || blocked entry_state_root_is_not_owned
    temporary_owner=$state_root/.helixharness-owner.tmp.$$
    printf 'helixharness-codex-entry-state-v1\n' >"$temporary_owner"
    chmod 600 "$temporary_owner"
    mv "$temporary_owner" "$state_owner"
  fi
fi
[ -f "$state_owner" ] && [ ! -L "$state_owner" ] || blocked entry_state_root_is_not_owned
[ "$(cat "$state_owner")" = helixharness-codex-entry-state-v1 ] || blocked entry_state_root_is_not_owned
chmod 700 "$state_root" || blocked entry_state_root_is_invalid
state_root=$(cd "$state_root" && pwd -P) || blocked entry_state_root_is_invalid

hash_file() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    return 1
  fi
}

validate_invocation() {
  invocation_suffix=${invocation_key#entry.}
  [ "$invocation_suffix" != "$invocation_key" ] || blocked invalid_invocation_key
  case "$invocation_suffix" in ''|*[!A-Za-z0-9]*) blocked invalid_invocation_key ;; esac
  [ ${#invocation_suffix} -ge 16 ] && [ ${#invocation_suffix} -le 32 ] || blocked invalid_invocation_key
  invocation=$state_root/$invocation_key
  [ -d "$invocation" ] && [ ! -L "$invocation" ] || blocked entry_state_is_missing_or_expired
  invocation_owner=$invocation/.helixharness-invocation
  [ -f "$invocation_owner" ] && [ ! -L "$invocation_owner" ] || blocked entry_state_is_not_owned
  [ "$(cat "$invocation_owner")" = helixharness-codex-entry-state-v1 ] || blocked entry_state_is_not_owned
  meta=$invocation/meta
  [ -f "$meta" ] && [ ! -L "$meta" ] || blocked entry_state_is_missing_or_expired
  stored_root=$(sed -n 's/^plugin_root=//p' "$meta")
  stored_command=$(sed -n 's/^command=//p' "$meta")
  expires_at=$(sed -n 's/^expires_at=//p' "$meta")
  [ "$stored_root" = "$plugin_root" ] || blocked entry_state_belongs_to_a_different_plugin_root
  case "$expires_at" in ''|*[!0-9]*) blocked entry_state_is_missing_or_expired ;; esac
  if [ "$expires_at" -le "$(date +%s)" ]; then
    rm -rf "$invocation"
    blocked entry_state_is_missing_or_expired
  fi
}

emit_content() {
  snapshot=$1
  last_byte=$(tail -c 1 "$snapshot" | od -An -tuC | tr -d ' ')
  trailing_newline=false
  [ "$last_byte" = 10 ] && trailing_newline=true
  awk -v trailing_newline="$trailing_newline" '
    BEGIN { printf "\"" }
    {
      if (NR > 1) printf "\\n"
      gsub(/\\/, "\\\\")
      gsub(/\"/, "\\\"")
      gsub(/\t/, "\\t")
      gsub(/\r/, "\\r")
      printf "%s", $0
    }
    END {
      if (trailing_newline == "true") printf "\\n"
      printf "\""
    }
  ' "$snapshot"
}

if [ "$action" = read ]; then
  [ -z "$command_name$resources" ] && [ -n "$invocation_key" ] || blocked invalid_read_page_arguments
  case "$packet_round" in 1|2) ;; *) blocked invalid_read_page_arguments ;; esac
  case "$page_index" in ''|*[!0-9]*) blocked invalid_read_page_arguments ;; esac
  validate_invocation
  row=$(awk -F '\t' -v round="$packet_round" -v page="$page_index" '$1 == round && $2 == page { print; found++ } END { if (found != 1) exit 1 }' "$invocation/pages.tsv") || blocked entry_page_is_missing_or_out_of_order
  relative=$(printf '%s\n' "$row" | awk -F '\t' '{print $3}')
  source_digest=$(printf '%s\n' "$row" | awk -F '\t' '{print $4}')
  resource_page_index=$(printf '%s\n' "$row" | awk -F '\t' '{print $5}')
  resource_page_count=$(printf '%s\n' "$row" | awk -F '\t' '{print $6}')
  page_digest=$(printf '%s\n' "$row" | awk -F '\t' '{print $7}')
  filename=$(printf '%s\n' "$row" | awk -F '\t' '{print $8}')
  case "$filename" in *[!A-Za-z0-9.-]*) blocked entry_page_metadata_is_invalid ;; esac
  case "$filename" in page-[12]-[0-9]*.txt) ;; *) blocked entry_page_metadata_is_invalid ;; esac
  page_path=$invocation/$filename
  [ -f "$page_path" ] && [ ! -L "$page_path" ] || blocked entry_page_identity_is_invalid
  [ "$(hash_file "$page_path")" = "$page_digest" ] || blocked entry_page_identity_is_invalid
  packet_page_count=$(sed -n "s/^packet_${packet_round}_pages=//p" "$meta")
  progress_file=$invocation/progress-$packet_round
  progress=$(cat "$progress_file" 2>/dev/null || true)
  case "$progress" in ''|*[!0-9]*) blocked entry_page_metadata_is_invalid ;; esac
  if [ "$packet_round" -eq 2 ]; then
    first_progress=$(cat "$invocation/progress-1" 2>/dev/null || true)
    first_count=$(sed -n 's/^packet_1_pages=//p' "$meta")
    [ "$first_progress" = "$first_count" ] || blocked entry_page_is_missing_or_out_of_order
  fi
  [ "$page_index" -le "$progress" ] || blocked entry_page_is_missing_or_out_of_order
  if [ "$page_index" -eq "$progress" ]; then
    printf '%s\n' $((progress + 1)) >"$invocation/progress-$packet_round.tmp"
    mv "$invocation/progress-$packet_round.tmp" "$progress_file"
  fi
  printf '{"schema_version":"1.1","status":"page","invocation_key":"%s","command":"%s","round":%s,"page_index":%s,"page_count":%s,"path":"%s","source_sha256":"%s","page_sha256":"%s","resource_page_index":%s,"resource_page_count":%s,"content":' "$invocation_key" "$stored_command" "$packet_round" "$page_index" "$packet_page_count" "$relative" "$source_digest" "$page_digest" "$resource_page_index" "$resource_page_count"
  emit_content "$page_path"
  printf '}\n'
  exit 0
fi

if [ "$action" = finish ]; then
  [ -z "$command_name$resources$packet_round$page_index" ] && [ -n "$invocation_key" ] || blocked invalid_finish_arguments
  validate_invocation
  [ "$(cat "$invocation/progress-1" 2>/dev/null || true)" = "$(sed -n 's/^packet_1_pages=//p' "$meta")" ] || blocked entry_pages_are_not_fully_consumed
  packet_two_count=$(sed -n 's/^packet_2_pages=//p' "$meta")
  [ "$packet_two_count" -eq 0 ] || [ "$(cat "$invocation/progress-2" 2>/dev/null || true)" = "$packet_two_count" ] || blocked entry_pages_are_not_fully_consumed
  rm -rf "$invocation"
  printf '{"schema_version":"1.1","status":"finished","invocation_key":"%s","command":"%s"}\n' "$invocation_key" "$stored_command"
  exit 0
fi

[ -z "$invocation_key$packet_round$page_index" ] || blocked invalid_prepare_arguments
case "$command_name" in ''|[0-9-]*|*[!a-z0-9-]*) blocked invalid_command ;; esac
[ ${#command_name} -le 64 ] || blocked invalid_command

host_manifest=$plugin_root/hosts/codex/host.json
[ -f "$host_manifest" ] && [ ! -L "$host_manifest" ] || blocked codex_host_context_is_invalid
host_id_count=$(sed -n 's/^[[:space:]]*"host_id"[[:space:]]*:.*$/x/p' "$host_manifest" | wc -l | tr -d ' ')
codex_host_id_count=$(sed -n 's/^[[:space:]]*"host_id"[[:space:]]*:[[:space:]]*"codex"[[:space:]]*,\{0,1\}[[:space:]]*$/x/p' "$host_manifest" | wc -l | tr -d ' ')
[ "$host_id_count" -eq 1 ] && [ "$codex_host_id_count" -eq 1 ] || blocked codex_host_context_is_invalid
host_api_count=$(sed -n 's/^[[:space:]]*"host_api_version"[[:space:]]*:.*$/x/p' "$host_manifest" | wc -l | tr -d ' ')
supported_host_api_count=$(sed -n 's/^[[:space:]]*"host_api_version"[[:space:]]*:[[:space:]]*"1\.1"[[:space:]]*,\{0,1\}[[:space:]]*$/x/p' "$host_manifest" | wc -l | tr -d ' ')
[ "$host_api_count" -eq 1 ] && [ "$supported_host_api_count" -eq 1 ] || blocked codex_host_context_is_invalid
for core_mapping in command_projection:hosts/codex/command-projection.json runtime:hosts/codex/runtime.md; do
  core_key=${core_mapping%%:*}
  core_expected=${core_mapping#*:}
  core_expression="s/^[[:space:]]*\"$core_key\"[[:space:]]*:[[:space:]]*\"resource:\([^\"]*\)\".*/\1/p"
  core_count=$(sed -n "$core_expression" "$host_manifest" | wc -l | tr -d ' ')
  [ "$core_count" -eq 1 ] || blocked codex_host_context_is_invalid
  [ "$(sed -n "$core_expression" "$host_manifest")" = "$core_expected" ] || blocked codex_host_context_is_invalid
done

projection=$plugin_root/hosts/codex/command-projection.json
projection_line="\"$command_name\": [\"hosts/codex/skills/$command_name/SKILL.md\", \"commands/$command_name.md\"]"
[ "$(grep -F -c "$projection_line" "$projection")" -eq 1 ] || blocked command_projection_is_missing_or_ambiguous
command_relative=commands/$command_name.md
command_path=$plugin_root/$command_relative
[ -f "$command_path" ] && [ ! -L "$command_path" ] || blocked command_contract_is_missing
for stale in "$state_root"/entry.*; do
  [ -d "$stale" ] && [ ! -L "$stale" ] || continue
  [ -f "$stale/.helixharness-invocation" ] && [ ! -L "$stale/.helixharness-invocation" ] || continue
  [ "$(cat "$stale/.helixharness-invocation")" = helixharness-codex-entry-state-v1 ] || continue
  stale_expiry=$(sed -n 's/^expires_at=//p' "$stale/meta" 2>/dev/null || true)
  case "$stale_expiry" in ''|*[!0-9]*) continue ;; esac
  [ "$stale_expiry" -gt "$(date +%s)" ] || rm -rf "$stale"
done

invocation=$(mktemp -d "$state_root/entry.XXXXXXXXXXXXXXXXXXXXXXXX") || blocked entry_state_is_invalid
invocation_key=${invocation##*/}
chmod 700 "$invocation"
printf 'helixharness-codex-entry-state-v1\n' >"$invocation/.helixharness-invocation"
chmod 600 "$invocation/.helixharness-invocation"
trap 'rm -rf "$invocation"' EXIT HUP INT TERM
: >"$invocation/pages.tsv"
: >"$invocation/resources-1.tsv"
: >"$invocation/resources-2.tsv"
packet_one_bytes=0
packet_two_bytes=0
packet_one_pages=0
packet_two_pages=0
snapshot_count=0

validate_utf8() {
  selected=$1
  command -v iconv >/dev/null 2>&1 || blocked utf8_validator_unavailable
  converted=$invocation/utf8-check
  iconv -f UTF-8 -t UTF-8 "$selected" >"$converted" 2>/dev/null || blocked packet_resource_is_not_utf8
  [ "$(hash_file "$converted")" = "$(hash_file "$selected")" ] || blocked packet_resource_utf8_roundtrip_mismatch
  od -An -v -tuC "$selected" | awk '{ for (i = 1; i <= NF; i++) if ($i < 32 && $i != 9 && $i != 10 && $i != 13) exit 1 }' || blocked packet_resource_contains_unsupported_control_byte
}

add_resource() {
  selected_round=$1
  selected_relative=$2
  target=$plugin_root/$selected_relative
  [ -f "$target" ] && [ ! -L "$target" ] || blocked packet_resource_is_missing
  resource_table=$invocation/resources-$selected_round.tsv
  grep -F -q "$(printf '%s\t' "$selected_relative")" "$resource_table" 2>/dev/null && return
  snapshot_count=$((snapshot_count + 1))
  snapshot=$invocation/source-$snapshot_count
  cp "$target" "$snapshot"
  chmod 400 "$snapshot"
  source_digest=$(hash_file "$target") || blocked hash_tool_unavailable
  [ "$source_digest" = "$(hash_file "$snapshot")" ] || blocked packet_resource_changed_during_snapshot
  validate_utf8 "$snapshot"
  bytes=$(wc -c <"$snapshot" | tr -d ' ')
  [ "$bytes" -le 2097152 ] || blocked packet_resource_exceeds_limit
  if [ "$selected_round" -eq 1 ]; then
    packet_one_bytes=$((packet_one_bytes + bytes)); [ "$packet_one_bytes" -le 8388608 ] || blocked entry_packet_exceeds_limit
    global_page=$packet_one_pages
  else
    packet_two_bytes=$((packet_two_bytes + bytes)); [ "$packet_two_bytes" -le 8388608 ] || blocked entry_packet_exceeds_limit
    global_page=$packet_two_pages
  fi

  offset=0
  resource_page=0
  resource_pages=$(( (bytes + 4095) / 4096 ))
  [ "$resource_pages" -gt 0 ] || resource_pages=1
  while [ "$offset" -lt "$bytes" ] || { [ "$bytes" -eq 0 ] && [ "$resource_page" -eq 0 ]; }; do
    remaining=$((bytes - offset))
    size=4096
    [ "$remaining" -ge "$size" ] || size=$remaining
    candidate=$invocation/page-$selected_round-$global_page.txt
    if [ "$bytes" -eq 0 ]; then
      : >"$candidate"
    else
      while :; do
        tail -c +$((offset + 1)) "$snapshot" | head -c "$size" >"$candidate"
        candidate_check=$invocation/page-check
        if iconv -f UTF-8 -t UTF-8 "$candidate" >"$candidate_check" 2>/dev/null \
          && [ "$(hash_file "$candidate")" = "$(hash_file "$candidate_check")" ]; then
          rm -f "$candidate_check"
          break
        fi
        size=$((size - 1))
        [ "$size" -gt 0 ] || blocked packet_resource_is_not_utf8
      done
    fi
    chmod 400 "$candidate"
    page_digest=$(hash_file "$candidate") || blocked hash_tool_unavailable
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$selected_round" "$global_page" "$selected_relative" "$source_digest" "$resource_page" "$resource_pages" "$page_digest" "${candidate##*/}" >>"$invocation/pages.tsv"
    offset=$((offset + size))
    resource_page=$((resource_page + 1))
    global_page=$((global_page + 1))
  done
  resource_pages=$resource_page
  awk -F '\t' -v OFS='\t' -v round="$selected_round" -v relative="$selected_relative" -v count="$resource_pages" '
    $1 == round && $3 == relative { $6 = count }
    { print }
  ' "$invocation/pages.tsv" >"$invocation/pages.tsv.tmp"
  mv "$invocation/pages.tsv.tmp" "$invocation/pages.tsv"
  printf '%s\t%s\t%s\t%s\n' "$selected_relative" "$source_digest" "$bytes" "$resource_pages" >>"$resource_table"
  if [ "$selected_round" -eq 1 ]; then packet_one_pages=$global_page; else packet_two_pages=$global_page; fi
  rm -f "$snapshot" "$invocation/utf8-check"
}

for relative in reference/workflow-projection-protocol.md reference/host-runtime-protocol.md reference/tool-semantics.md hosts/codex/host.json hosts/codex/runtime.md hosts/codex/command-projection.json "$command_relative"; do
  add_resource 1 "$relative"
done

for resource in $resources; do
  case "$resource" in ''|*[!a-z_]*) blocked invalid_adapter_resource ;; esac
  resource_pattern="resources\\.$resource([^a-z_]|$)"
  grep -E -q "$resource_pattern" "$command_path" || blocked adapter_is_not_allowed_for_command
  shared=
  case "$resource" in
    subagent_protocol) shared=reference/subagent-runtime-protocol.md ;;
    telemetry_protocol) shared=reference/telemetry-command-protocol.md ;;
  esac
  adapter_expression="s/^[[:space:]]*\"$resource\"[[:space:]]*:[[:space:]]*\"resource:\([^\"]*\)\".*/\1/p"
  adapter_count=$(sed -n "$adapter_expression" "$host_manifest" | wc -l | tr -d ' ')
  [ "$adapter_count" -eq 1 ] || blocked adapter_resource_is_not_mapped
  adapter_relative=$(sed -n "$adapter_expression" "$host_manifest")
  case "$adapter_relative" in
    ''|/*|*\\*|.|..|./*|../*|*/./*|*/../*|*/.|*/..) blocked adapter_resource_is_not_mapped ;;
  esac
  [ -z "$shared" ] || add_resource 2 "$shared"
  add_resource 2 "$adapter_relative"
done

expires_at=$(($(date +%s) + 900))
cat >"$invocation/meta" <<EOF
plugin_root=$plugin_root
command=$command_name
expires_at=$expires_at
packet_1_pages=$packet_one_pages
packet_2_pages=$packet_two_pages
EOF
chmod 600 "$invocation/meta"
printf '0\n' >"$invocation/progress-1"
printf '0\n' >"$invocation/progress-2"

emit_resources() {
  selected_round=$1
  separator=
  while IFS="$(printf '\t')" read -r relative digest bytes page_count; do
    [ -n "$relative" ] || continue
    printf '%s{"path":"%s","source_sha256":"%s","bytes":%s,"page_count":%s}' "$separator" "$relative" "$digest" "$bytes" "$page_count"
    separator=,
  done <"$invocation/resources-$selected_round.tsv"
}

printf '{"schema_version":"1.1","status":"ready","host_id":"codex","command":"%s","max_rounds":2,"invocation_key":"%s","expires_at":%s,"packets":[{"round":1,"page_count":%s,"resources":[' "$command_name" "$invocation_key" "$expires_at" "$packet_one_pages"
emit_resources 1
printf ']}'
if [ "$packet_two_pages" -gt 0 ]; then
  printf ',{"round":2,"page_count":%s,"resources":[' "$packet_two_pages"
  emit_resources 2
  printf ']}'
fi
printf ']}\n'
trap - EXIT HUP INT TERM
