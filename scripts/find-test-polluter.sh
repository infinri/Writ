#!/usr/bin/env bash
# Find the test module that pollutes another test, by bisection over the suite's
# own collection order.
#
# Two predicates, one mechanism:
#   --victim  <node id>  reproduce = that test FAILS when the subset ran first
#   --artifact <path>    reproduce = that path EXISTS after the subset ran
#
# Why bisection and not the one-by-one scan: this suite has 418 test modules, so
# a linear scan is 418 pytest starts. Halving needs ~9 runs, and the first run is
# the expensive one, so the whole search costs roughly two full-suite runs.
#
# Why it never deletes anything: --artifact takes a path from the caller, and a
# tool that rm -rf's a caller-supplied path is the exact shape of the incident
# this repo already survived. If the artifact exists before a run, the script
# refuses and tells you to remove it yourself.
#
# Assumes ONE polluter. When neither half reproduces alone, the interaction needs
# two or more modules together; the script says so and names both halves rather
# than returning a confident wrong answer.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=".venv/bin/python"

VICTIM=""
ARTIFACT=""
while [ $# -gt 0 ]; do
    case "$1" in
        --victim)   VICTIM="$2"; shift 2 ;;
        --artifact) ARTIFACT="$2"; shift 2 ;;
        *) echo "usage: $0 (--victim <pytest node id> | --artifact <path>)" >&2; exit 2 ;;
    esac
done
if [ -z "$VICTIM" ] && [ -z "$ARTIFACT" ]; then
    echo "usage: $0 (--victim <pytest node id> | --artifact <path>)" >&2
    exit 2
fi

# Collection doubles as the preflight: tests/conftest.py refuses the whole run
# when the disposable 7688 instance is absent, so this fails here with pytest's
# own message instead of failing nine times inside the loop.
if ! COLLECTED="$("$PYTHON" -m pytest tests/ --collect-only -q 2>&1)"; then
    echo "collection failed; start the disposable graph first: make test-graph-up" >&2
    printf '%s\n' "$COLLECTED" | tail -n 5 >&2
    exit 2
fi

VICTIM_MODULE="${VICTIM%%::*}"
mapfile -t MODULES < <(
    printf '%s\n' "$COLLECTED" \
        | grep -E '^tests/.*\.py::' \
        | sed 's/::.*//' \
        | awk '!seen[$0]++'
)
CANDIDATES=()
for module in "${MODULES[@]}"; do
    [ -n "$VICTIM_MODULE" ] && [ "$module" = "$VICTIM_MODULE" ] && break
    CANDIDATES+=("$module")
done
echo "candidates: ${#CANDIDATES[@]} module(s) collected before the victim"

reproduce() {
    if [ -n "$ARTIFACT" ] && [ -e "$ARTIFACT" ]; then
        echo "refusing to run: $ARTIFACT already exists. Remove it yourself and re-run." >&2
        exit 2
    fi
    local out
    out="$("$PYTHON" -m pytest "$@" ${VICTIM:+"$VICTIM"} -q --tb=no -rfE 2>&1 || true)"
    if [ -n "$ARTIFACT" ]; then
        [ -e "$ARTIFACT" ]
        return
    fi
    printf '%s\n' "$out" | grep -Fq "FAILED $VICTIM" && return 0
    printf '%s\n' "$out" | grep -Fq "ERROR $VICTIM" && return 0
    return 1
}

if reproduce; then
    echo "the predicate already holds with NO other module: this is not test-state pollution." >&2
    exit 1
fi
if ! reproduce "${CANDIDATES[@]}"; then
    echo "the full candidate set does not reproduce it; the interaction is not in this ordering." >&2
    exit 1
fi

set_=("${CANDIDATES[@]}")
while [ "${#set_[@]}" -gt 1 ]; do
    half=$(( ${#set_[@]} / 2 ))
    first=("${set_[@]:0:half}")
    second=("${set_[@]:half}")
    echo "[bisect] ${#set_[@]} -> ${#first[@]} / ${#second[@]}"
    if reproduce "${first[@]}"; then
        set_=("${first[@]}")
    elif reproduce "${second[@]}"; then
        set_=("${second[@]}")
    else
        echo "neither half reproduces alone: two or more modules interact." >&2
        echo "  first half:  ${first[*]}" >&2
        echo "  second half: ${second[*]}" >&2
        exit 3
    fi
done

echo "polluter: ${set_[0]}"
echo "confirm with: $PYTHON -m pytest ${set_[0]} ${VICTIM:-} -q"
