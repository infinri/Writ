# jq arm of writ-pre-write-dispatch.sh's consolidated stdin parse.
#
# Emits exactly three lines, in this order, because the call site reads them with
# head -1 / sed -n 2p / tail -n +3:
#   1. session id  (agent_id preferred over session_id, then stripped)
#   2. write context  (file path + content + new_string, single line, for the
#      always-on applicability filter)
#   3. the /pre-write-check request body  (compact JSON)
#
# WHY: this is the hottest gate path in the system, and the python arm below it pays
# ~15ms of interpreter startup to do JSON reshaping (measured 2026-08-07). The python
# snippet REMAINS as the fallback: this file is an accelerator, and jq's absence must
# change speed and never behavior, the contract install.md states and
# tests/test_pre_write_parse_parity.py enforces arm against arm.
#
# INVOCATION: jq -R -s -r --arg skill_dir "$SKILL_DIR" -f pre-write-parse.jq
# Raw-slurp because the python arm wraps its json.loads in try/except and falls back
# to {}: parsing inside the filter puts the same failure inside a `try`, so malformed
# or empty stdin produces three lines here too instead of nothing.
#
# The two truthiness helpers are the same ones parse-hook-stdin.jq documents: python's
# `or` falls through on the empty string while jq's `//` only tests for null, and the
# file_path / content chains here are `or` chains.

def as_object: if type == "object" then . else {} end;
def pyor(a; b): if (a == null or a == "" or a == false) then b else a end;

(if type == "string" then (try fromjson catch {}) else . end | as_object) as $d
| $d.tool_input as $ti0
| (if ($ti0 | type) == "string" then (try ($ti0 | fromjson | as_object) catch {})
   else ($ti0 | as_object) end) as $ti
| (pyor($d.agent_id; pyor($d.session_id; "")) | tostring
   | gsub("^\\s+"; "") | gsub("\\s+$"; "")) as $sid
# NotebookEdit uses notebook_path, not file_path. Mapping it here is load-bearing:
# the server gate sees an empty path otherwise and silently allows the write.
| pyor($ti.file_path; pyor($ti.path; pyor($ti.notebook_path; ""))) as $fp
| ([$fp,
    pyor($ti.content; pyor($ti.new_source; "")),
    pyor($ti.new_string; "")]
   | map(select(type == "string" and . != ""))
   | join(" ") | gsub("[\n\r]"; " ")) as $ctx
| $sid,
  $ctx,
  ({session_id: $sid, tool_input: $ti, skill_dir: $skill_dir, file_path: $fp} | tojson)
