#!/usr/bin/env bash
# Writ documents: one of five UserPromptSubmit hooks that split the per-prompt injection so
# no single hook's output crosses the host's per-hook cap (docs/adr/ADR-prompt-injection-split.md,
# docs/adr/ADR-document-retrieval.md). Prints only the fenced documents block: the project's
# document chunks closest to the prompt, ingested by `writ docs ingest`.
# Exit: always 0, never blocks the prompt.
set -euo pipefail
if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then
  WRIT_DIR="${CLAUDE_PLUGIN_ROOT}"
else
  HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
  WRIT_DIR="$(cd "$HOOK_DIR/../.." && pwd)"
fi
# shellcheck source=bin/lib/writ-venv.sh
source "$WRIT_DIR/bin/lib/writ-venv.sh"
writ_resolve_venv "$WRIT_DIR" || true
source "$WRIT_DIR/bin/lib/common.sh"
WRIT_HOOK_LOG_SINK="$(hook_log_sink)"
source "$WRIT_DIR/bin/lib/writ-prompt-section.sh"
writ_prompt_section_main documents writ-inject-documents
exit 0
