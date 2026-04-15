#!/usr/bin/env bash
# Writ Stop hook -- no-op retained for hook registration compatibility.
# Session-level operations run in writ-session-end.sh on the SessionEnd event.
#
# Hook type: Stop
# Exit: always 0

set -euo pipefail

exit 0
