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
# BYTE-IDENTICAL with the copy in parse-hook-stdin.jq. This copy had drifted (it was
# missing the 0 and empty-container cases), which is exactly the failure mode
# duplication invites, so if you edit one, edit both.
def pytruthy: . != null and . != false and . != "" and . != 0 and . != [] and . != {};
def pyor(a; b): if (a | pytruthy) then a else b end;

(if type == "string" then (try fromjson catch {}) else . end | as_object) as $d
| $d.tool_input as $ti0
| (if ($ti0 | type) == "string" then (try ($ti0 | fromjson | as_object) catch {})
   else ($ti0 | as_object) end) as $ti
# The embedded-newline strip is structural, not cosmetic. The call site splits this
# output positionally (head -1 / sed -n 2p / tail -n +3), so a session id containing a
# newline would emit FOUR lines: SESSION_ID would silently truncate at the newline and
# CHECK_BODY would become a non-JSON line concatenated with the real body, which is then
# POSTed to /pre-write-check. Both arms did this identically, so it was never a jq-vs-
# python divergence, just a hole in the format. A real session id is a UUID, so this
# only ever fires on a malformed envelope, which is precisely when a gate must not
# quietly mis-parse.
# A NON-STRING id becomes "", on both arms, rather than being coerced. The type test
# sits AFTER the agent_id/session_id selection so it mirrors python's `or` picking the
# truthy value first and only then failing to .strip() it. Coercion was the obvious
# alternative and it is NOT parity-safe: jq's tostring gives "true" where python's str()
# gives "True", and an id is not worth inventing. "" routes to the hook's existing
# detect_session_id fallback, which derives the real session from PPID and cwd.
| (pyor($d.agent_id; pyor($d.session_id; ""))
   | if type == "string" then . else "" end
   | gsub("[\n\r]"; " ") | gsub("^\\s+"; "") | gsub("\\s+$"; "")) as $sid
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
