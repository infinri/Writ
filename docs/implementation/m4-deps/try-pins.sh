#!/usr/bin/env bash
# Tries each release-age pin candidate (young.tsv) individually against the unpinned lock,
# then all resolving pins together. Run from the repository root with the M1 toolchain on PATH.
set -u
E=docs/implementation/m4-deps
cp Cargo.lock "$E/.unpinned.lock"
ok=()
while IFS=$'\t' read -r name ver _ _ cand _; do
  [ "$name" = name ] && continue
  [ "$cand" = none ] && { echo "$name $ver: no compatible version at least 14 days old"; continue; }
  cp "$E/.unpinned.lock" Cargo.lock
  if out=$(cargo update -p "$name@$ver" --precise "$cand" 2>&1); then
    echo "$name $ver -> $cand: resolves"; ok+=("$name@$ver=$cand")
  else
    echo "$name $ver -> $cand: does not resolve: $(echo "$out" | grep -m1 -E 'error|required by' )"
  fi
done < "$1"
cp "$E/.unpinned.lock" Cargo.lock
for p in "${ok[@]}"; do
  n=${p%%=*}; c=${p#*=}
  cargo update -p "$n" --precise "$c" >/dev/null 2>&1 && echo "combined: applied $n -> $c" || echo "combined: FAILED $n -> $c"
done
rm -f "$E/.unpinned.lock"
