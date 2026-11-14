#!/usr/bin/env bash
# Run a command with no inherited descriptor above 2 (M3-07, E-071).
#
# The mechanical runner's local sandbox refuses every job when the broker process holds a
# descriptor above 2 without close-on-exec, because such a descriptor would leak into the
# job and cannot be closed in the child without unsafe code. Some hosts (for example CI
# runners) start processes with such descriptors. This wrapper closes them in a fresh
# shell, reports each one, and then executes the command. It is a host precondition for
# the broker and its tests, not a change to any check.
set -euo pipefail
[ "$#" -ge 1 ] || { echo "usage: $0 command [args...]" >&2; exit 2; }
exec bash -c '
for p in /proc/$$/fd/*; do
  fd=${p##*/}
  if [ "$fd" -gt 2 ] 2>/dev/null; then
    target=$(readlink "$p" 2>/dev/null) || continue
    echo "run-with-clean-fds: closing inherited fd $fd ($target)" >&2
    eval "exec $fd>&-"
  fi
done
exec "$@"' bash "$@"
