#!/bin/bash
# Shared library for phaselock bin/ scripts and hooks.
# Source this file: source "$(dirname "$0")/lib/common.sh"

# ── Hook stdin envelope parser ──────────────────────────────────────────────
# Reads the Claude Code hook stdin envelope and normalizes it into a JSON
# object with flattened fields (file_path, content, tool_name, is_error, etc.).
# Falls back to CLAUDE_TOOL_INPUT env var when envelope is missing.
#
# Usage (call ONCE per hook, stdin is consumed):
#   PARSED=$(parse_hook_stdin)
#   FILE=$(echo "$PARSED" | jq -r '.file_path // empty')
#   TOOL=$(echo "$PARSED" | jq -r '.tool_name // empty')
#
# If jq is unavailable, use python3:
#   FILE=$(echo "$PARSED" | python3 -c "import sys,json; print(json.load(sys.stdin).get('file_path',''))")
_PARSE_HOOK_STDIN_PY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/parse-hook-stdin.py"

parse_hook_stdin() {
    python3 "$_PARSE_HOOK_STDIN_PY" 2>/dev/null || echo '{}'
}

# Convenience: extract a single string field from parsed JSON.
# Usage: FILE=$(parsed_field "$PARSED" "file_path")
parsed_field() {
    local json="$1" field="$2"
    echo "$json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('$field',''))" 2>/dev/null
}

# Convenience: extract a boolean field from parsed JSON.
# Usage: if parsed_bool "$PARSED" "is_error"; then ...
parsed_bool() {
    local json="$1" field="$2"
    local val
    val=$(echo "$json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('$field', False))" 2>/dev/null)
    [ "$val" = "True" ]
}

# ── Project root detection ────────────────────────────────────────────────────
# Walks up from a given path to find the project root by marker files.
# Usage: PROJECT_ROOT=$(detect_project_root "/path/to/some/file.php")
detect_project_root() {
  local start_path="$1"
  python3 -c "
import os, sys
markers = ['composer.json','package.json','Cargo.toml','go.mod','pyproject.toml','.git']
path = os.path.abspath('$start_path')
while path != '/':
    path = os.path.dirname(path)
    if any(os.path.exists(os.path.join(path, m)) for m in markers):
        print(path); sys.exit(0)
print('')
" 2>/dev/null
}

# ── JSON output helpers ──────────────────────────────────────────────────────
# Produces a single JSON finding object. All values are passed as arguments.
# Usage: json_finding true "ENF-POST-007" "PHPStan error" "src/Foo.php" "Fix it"
json_finding() {
  local is_error="$1" rule="$2" message="$3" file="$4" fix="$5"
  python3 -c "
import json, sys
print(json.dumps({
    'error': $(echo "$is_error" | python3 -c "import sys; print(sys.stdin.read().strip().capitalize())"),
    'rule': sys.argv[1],
    'message': sys.argv[2],
    'file': sys.argv[3],
    'fix': sys.argv[4]
}, ensure_ascii=False))" "$rule" "$message" "$file" "$fix" 2>/dev/null
}

# Produces a JSON array from individual JSON objects (one per line on stdin).
# Usage: echo "$FINDINGS" | json_array
json_array() {
  python3 -c "
import json, sys
items = []
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        items.append(json.loads(line))
    except json.JSONDecodeError:
        pass
print(json.dumps(items, indent=2, ensure_ascii=False))
" 2>/dev/null
}

# ── Tool location helpers ────────────────────────────────────────────────────
# Finds a tool binary: checks project vendor path first, then global PATH.
# Usage: PHPSTAN=$(find_tool "$PROJECT_ROOT" "vendor/bin/phpstan" "phpstan")
find_tool() {
  local project_root="$1" vendor_path="$2" global_name="$3"
  if [ -n "$project_root" ] && [ -f "$project_root/$vendor_path" ]; then
    echo "$project_root/$vendor_path"
  elif command -v "$global_name" &>/dev/null; then
    echo "$global_name"
  fi
}

# ── File extension helpers ───────────────────────────────────────────────────
# Returns the language category for a file extension.
# Usage: LANG=$(detect_language "src/Foo.php")
detect_language() {
  local file="$1"
  case "$file" in
    *.php)               echo "php" ;;
    *.xml)               echo "xml" ;;
    *.js|*.jsx)          echo "javascript" ;;
    *.ts|*.tsx)          echo "typescript" ;;
    *.py)                echo "python" ;;
    *.rs)                echo "rust" ;;
    *.go)                echo "go" ;;
    *.graphqls|*.graphql) echo "graphql" ;;
    *)                   echo "unknown" ;;
  esac
}

# ── Config readers ───────────────────────────────────────────────────────────
# Reads a single-line config value from a project config file.
# Usage: LEVEL=$(read_project_config "$PROJECT_ROOT" ".claude/phpstan-level" "8")
read_project_config() {
  local project_root="$1" config_file="$2" default="$3"
  local full_path="$project_root/$config_file"
  if [ -f "$full_path" ]; then
    cat "$full_path"
  else
    echo "$default"
  fi
}
