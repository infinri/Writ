# Build the RAG hook's friction/telemetry rows from a /prompt-bundle response.
#
# One JSON object per line on stdout, exactly what the python builder it replaces emitted.
# Those rows are AUDIT records (rag_query and always_on_inject), so the parity bar here is
# equality of the PARSED objects, which tests/test_friction_rows_jq.py asserts against the
# original python across every bundle shape the endpoint produces.
#
# INVOCATION: jq -R -s -r --arg sid ... --arg mode ... --arg effort ... -f friction-rows.jq
# The raw-slurp flags are part of the contract, same as parse-hook-stdin.jq: without them
# a malformed body makes jq emit nothing AND fail, where the python arm exits 0 silently.
# Parsing inside the filter puts that case in a `try` so both arms agree.
#
# WHY KEY ORDER IS NOT MATCHED. python's json.dumps preserves insertion order and jq's
# object construction does not always agree. The consumer is json.loads in the drain, so
# order carries no meaning; matching it would constrain the filter for nothing.

def as_object: if type == "object" then . else {} end;
def root: (if type == "string" then (try fromjson catch {}) else . end) | as_object;

# python's int(x or 0): absent or null is 0. The endpoint sends integers for cost, tokens
# and count (verified against a live response), so this only has to survive a missing
# field, not reproduce python's float-truncation rules on a value that never arrives.
def n0: if . == null then 0 else (. | floor) end;
def arr: if . == null then [] else . end;

def rag($src; $meta; $sid; $modev; $effort):
  {
    session: $sid,
    mode: $modev,
    event: "rag_query",
    query_source: $src,
    tokens_injected: ($meta.cost | n0),
    rules_returned_count: (($meta.rule_ids | arr) | length),
    rule_ids: ($meta.rule_ids | arr)
  }
  # `effort` is present only when non-empty, matching `if effort: e['effort'] = effort`.
  + (if $effort == "" then {} else {effort: $effort} end)
  + {event_name: "UserPromptSubmit", mechanism: "stdout"};

root
| . as $b
# python: `os.environ.get('WRIT_MODE','') or None`, so an empty mode is JSON null, not "".
| (if $mode == "" then null else $mode end) as $modev
| [
    (if $b.broad_meta != null then rag("broad"; $b.broad_meta; $sid; $modev; $effort) else empty end),

    # The tokens > 0 test is the python builder's, kept because a zero-token always-on
    # inject is not an event worth recording and dropping it here keeps the two arms equal.
    (if ($b.ao_meta != null and (($b.ao_meta.tokens | n0) > 0)) then
       {
         session: $sid,
         mode: $modev,
         event: "always_on_inject",
         tokens: ($b.ao_meta.tokens | n0),
         rule_count: ($b.ao_meta.count | n0),
         rule_ids: ($b.ao_meta.rule_ids | arr),
         event_name: "UserPromptSubmit",
         mechanism: "stdout"
       }
     else empty end),

    (if $b.method_meta != null
     then rag(($b.method_meta.query_source // ""); $b.method_meta; $sid; $modev; $effort)
     else empty end)
  ][]
| tojson
