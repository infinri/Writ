#!/bin/bash
# Batch file existence checker — deterministic replacement for AI attempt-read.
# Returns structured JSON with existence status for each path.
#
# Usage:
#   bin/verify-files.sh file1.php file2.php ...
#   bin/verify-files.sh --project-root /path file1.php file2.php ...
#   echo "file1.php\nfile2.php" | bin/verify-files.sh --stdin
#
# Output: { "results": { "file1.php": true, "file2.php": false }, "all_exist": false, "missing": ["file2.php"] }

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"

PROJECT_ROOT=""
FILES=()
USE_STDIN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-root) PROJECT_ROOT="$2"; shift 2 ;;
    --stdin) USE_STDIN=true; shift ;;
    *) FILES+=("$1"); shift ;;
  esac
done

if $USE_STDIN; then
  while IFS= read -r line; do
    [ -n "$line" ] && FILES+=("$line")
  done
fi

if [ ${#FILES[@]} -eq 0 ]; then
  echo '{"error": "No files specified. Usage: verify-files.sh [--project-root DIR] file1 [file2 ...]"}'
  exit 2
fi

# Build newline-separated list for python
FILE_LIST=$(printf '%s\n' "${FILES[@]}")

python3 -c "
import json, os

project_root = '$PROJECT_ROOT'
files = '''$FILE_LIST'''.strip().split('\n')

results = {}
missing = []

for f in files:
    f = f.strip()
    if not f:
        continue

    # Resolve relative paths against project root if available
    if not os.path.isabs(f) and project_root:
        check_path = os.path.join(project_root, f)
    else:
        check_path = f

    exists = os.path.exists(check_path)
    results[f] = exists
    if not exists:
        missing.append(f)

output = {
    'results': results,
    'total': len(results),
    'existing': len(results) - len(missing),
    'all_exist': len(missing) == 0,
    'missing': missing
}

print(json.dumps(output, indent=2))
" 2>/dev/null