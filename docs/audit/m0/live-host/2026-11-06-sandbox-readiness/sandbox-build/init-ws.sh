#!/usr/bin/env bash
# Create the SR-06 workspace with dummy data only. Run as tester.
set -euo pipefail
WS=/home/tester/ws
mkdir -p "$WS/src" "$WS/secrets" "$WS/tests" /home/tester/run/current
git init -q --bare /home/tester/remote.git
git init -q "$WS"
git -C "$WS" config user.name "sr06-tester"
git -C "$WS" config user.email "sr06@sandbox.invalid"
printf 'def a():\n    return "a"\n' > "$WS/src/app.py"
printf 'def b():\n    return "a"\n' > "$WS/src/app2.py"
printf 'def c():\n    return "a"\n' > "$WS/src/app3.py"
printf 'seed\n' > "$WS/seed"
printf 'FAKE\n' > "$WS/secrets/FAKE"
git -C "$WS" add -A && git -C "$WS" commit -qm "sr06 dummy workspace"
git -C "$WS" remote add origin /home/tester/remote.git
git -C "$WS" push -q origin HEAD:refs/heads/main
