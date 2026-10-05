"""Friction rows for one /prompt-bundle response: the python fallback arm of
bin/lib/friction-rows.jq.

Reads the bundle JSON on stdin, WRIT_SID and WRIT_MODE from the environment, and prints
one JSON row per line (rag_query, rag_channel_suppressed, always_on_inject). The four
UserPromptSubmit injection hooks run it through writ_bundle_friction
(bin/lib/writ-prompt-section.sh) whenever jq is absent or WRIT_NO_JQ is set, so absence
of jq changes speed, never behaviour. tests/test_friction_rows_jq.py executes this exact
file against the filter and asserts the parsed rows are identical.

A section response carries only its own *_meta, so each hook's response yields only its
own row. Malformed input yields no rows and exit 0.
"""
import json
import os
import sys

try:
    b = json.load(sys.stdin)
except Exception:
    sys.exit(0)
sid = os.environ.get('WRIT_SID', '')
mode = os.environ.get('WRIT_MODE', '') or None
def rag(src, meta):
    e = {'session': sid, 'mode': mode, 'event': 'rag_query', 'query_source': src,
         'tokens_injected': int(meta.get('cost', 0)),
         'rules_returned_count': len(meta.get('rule_ids', [])), 'rule_ids': meta.get('rule_ids', [])}
    e['event_name'] = 'UserPromptSubmit'; e['mechanism'] = 'stdout'
    return e
lines = []
bm = b.get('broad_meta')
if bm is not None:
    # A suppressed ranked channel (include_ranked=false) is NOT a zero-rule
    # rag_query: a zero-rule rag_query is the abstention signal every census
    # that counts retrievals by source relies on, so recording the
    # suppression that way would be indistinguishable from a real retrieval
    # that came back empty.
    if bm.get('suppressed'):
        lines.append({'session': sid, 'mode': mode, 'event': 'rag_channel_suppressed',
                      'channel': 'broad', 'event_name': 'UserPromptSubmit', 'mechanism': 'stdout'})
    else:
        lines.append(rag('broad', bm))
ao = b.get('ao_meta')
if ao is not None and int(ao.get('tokens', 0)) > 0:
    lines.append({'session': sid, 'mode': mode, 'event': 'always_on_inject',
                  'tokens': int(ao.get('tokens', 0)), 'rule_count': int(ao.get('count', 0)),
                  'rule_ids': ao.get('rule_ids') or [],
                  'event_name': 'UserPromptSubmit', 'mechanism': 'stdout'})
mm = b.get('method_meta')
if mm is not None:
    lines.append(rag(mm.get('query_source', ''), mm))
for e in lines:
    print(json.dumps(e))
