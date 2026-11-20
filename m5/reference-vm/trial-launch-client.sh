#!/usr/bin/env bash

stage_launch_client() {
  client="$share/launch-client.py"
  install -m 0644 -o root -g root "$here/launch-client.py" "$client"
  cmp -s "$here/launch-client.py" "$client"
}

record_launch_client() {
  local id="$1" rc=0
  shift
  "$@" > "$share/launch-$id.txt" 2>&1 || rc=$?
  cp "$share/launch-$id.txt" "$ev/launch-$id.txt"
  printf '%s\n' "$rc" > "$ev/launch-$id.exit"
  if [ "$rc" -ne 0 ]; then
    printf 'launch client %s exited %s; diagnostic in %s/launch-%s.txt\n' "$id" "$rc" "$ev" "$id" >&2
    return "$rc"
  fi
}

# launch AS ID MODE: run the client as AS; output in $share/launch-ID.txt and its stdout bytes
# in $share/launch-ID.out.
launch() {
  local as="$1" id="$2" mode="${3:-normal}"
  local req="$share/req-$id.json" job_stdout="$share/launch-$id.out"
  local -a runner=()
  [ -f "$req" ] || req="$share/req-default.json"
  chmod 0644 "$req"
  install -m 0600 -o "$as" /dev/null "$job_stdout"
  if [ "$as" != root ]; then runner=(runuser -u "$as" --); fi
  record_launch_client "$id" "${runner[@]}" python3 "$client" \
    --socket "$sock" --request "$req" --mode "$mode" --stdout-file "$job_stdout"
}
