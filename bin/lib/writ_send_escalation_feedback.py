"""C10 negative-feedback POSTer for writ-rag-inject.sh.

Extracted VERBATIM from the hook's inline `python3 -c` block (was lines 696-723).
argv[1]=cache JSON, argv[2]=gate; POSTs one negative signal per distinct rule id.
The localhost:8765 URL is a pre-existing hook quirk, preserved verbatim. stdlib-only."""
import sys, json

cache_str = sys.argv[1]
gate = sys.argv[2]

try:
    cache = json.loads(cache_str)
except Exception:
    sys.exit(0)

records = cache.get('invalidation_history', {}).get(gate, [])
rule_ids = set(r['rule_id'] for r in records)

# Through the shared client, which prefers the daemon's unix socket. The hardcoded
# localhost:8765 URL this replaces was one of three python call sites the transport
# census exposed; it is now the client's TCP fallback rather than the only path.
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import writ_daemon_client

for rid in rule_ids:
    status, _body = writ_daemon_client.post_json(
        '/feedback', {'rule_id': rid, 'signal': 'negative'}, timeout=0.3
    )
    if status == 0:
        break
