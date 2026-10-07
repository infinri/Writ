# ADR: attribution and trust records (TrustEvent, Rule trust props, supersede, trust tags)

Status: accepted
Date: 2026-10-06
Plan: `.claude/plans/1c1f801f-c493-4e74-aa60-76c1e69ea30e/plan.md` (knowledge-engine program item 6)
Touches: `writ/graph/schema.py`, `writ/graph/db/_common.py`, `writ/graph/db/record_store.py`,
`writ/graph/db/rule_store.py`, `writ/graph/predicates.py`, `writ/graph/ingest.py`,
`writ/graph/methodology_ingest.py`, `writ/export.py`, `writ/shared/trust.py`,
`writ/session/gate_token.py`, `bin/lib/common.sh`, `writ/authoring.py`, `writ/cli.py`
Sibling: `docs/adr/ADR-ranked-header-fields.md`, whose header slot the trust tags extend.

## Context

The program asks four questions of every rule: who vouched for it, when it was last checked,
whether someone has disputed it, and whether a newer rule replaced it. Before this item the
graph could answer none of them. `authority` records what kind of party vouches (human,
ai-provisional, ai-promoted), not which person or when; `last_validated` is reset on every
markdown parse, so it is a parse clock, not a verify clock; and a `SUPERSEDES` edge existed in
the edge allowlist without any reader acting on it.

Two constraints shaped every decision below. First, the public `writ-corpus.cypher` dump
writes `properties(n)` for every Rule, so any person's name stored on a Rule ships to every
install. Second, the agent must never be able to attribute an approval to anyone, including
itself: identity has to come from the same user keystroke that authorizes the action.

## Decision

### 1. A hybrid record shape: flat summary props on Rule, history on TrustEvent

Rule carries cheap-to-read summary props, and a separate `TrustEvent` record label carries the
history (one node per approval, dispute or verify).

Authored in markdown, MANAGED (reconcile clears one removed from source): `layer`
(`observed`, `inferred`, `unknown`), `basis` (`code`, `document`, `ticket`, `testimony`),
`deliberate` (bool) and `verify_interval_days` (int >= 1, default 180). All three markdown
formats parse them through one helper, and export renders them only when present, so the
round trip is lossless and an unchanged corpus does not churn.

Graph-only (`TRUST_GRAPH_ONLY_PROPS`, unioned into `RUNTIME_EXEMPT_PROPS` and into export's
`GRAPH_ONLY_FIELDS`): `approved_at`, `approval_via`, `last_verified`, `disputed`,
`superseded`. Markdown can never author one: the front-matter parse drops them, and the one
write path (`set_rule_trust_props`) allowlists every key except the derived `superseded`.

`TrustEvent` joins `RECORD_LABELS`, the single list that drives the corpus-wipe preserve set,
the dump-import preserve set and the dump exclusion. It is written through the existing
`_create_record` (MERGE on `(event_id, project)`, `provenance='record'`), so reconcile and
parity already exempt it, and a uniqueness constraint on the MERGE key makes concurrent writes
converge on one node. No edge links an event to its Rule: `rule_id` plus an index is the link,
because a new record edge type would buy no read this item needs.

### 2. Identity is captured at token mint and stays local

The approval hook's token writers (`mint_gate_token` and `write_gate_token_file`) append
line 6 (the OS login) and line 7 (the global git user name), captured by the hook process when
it mints the token, outside the agent's turn. The OS login is the anchor, because the agent
cannot change it; the git name is display only, because an agent with shell access could edit
the global git config. The review command reads the two lines before claiming the token
(the claim deletes the file) and exposes no identity option.

Identity lives ONLY on TrustEvent nodes, which the dump excludes. Rule has no approver field,
and the audit rows these actions write carry no identity either. A token minted by an older
hook has no lines 6 and 7: the action is still authorized and the event records empty
identity, because refusing would break promotion until the plugin release.

### 3. Line 5 binds a rule action, not just a rule

With dispute and verify added, an approval given for promoting X would otherwise be spendable
on disputing X, and the reverse. `rule_action_binding(action, rule_id)` returns the bare rule
id for `promote` (unchanged bytes) and `dispute:<id>` or `verify:<id>` otherwise. The rule id
pattern forbids `:`, so a qualified binding can never equal a real rule id. The identity lines
are not a binding: they never change whether a token is accepted.

### 4. superseded is a derived flag

`refresh_superseded_flags(project)` sets `superseded` from incoming `SUPERSEDES` edges (the
edge runs from the replacement to the deprecated rule, so the target is superseded), writing
only rows whose value changes. It runs after every live ingest, again after reconcile's edge
prune (so a removed declaration clears the flag), and after `writ add` or `writ edit` creates
a `SUPERSEDES` edge. Both retrieval predicates gain `coalesce(r.superseded, false) = false`,
fully parenthesized, and the Python replicas (the BM25 build skip, the BM25 cache key,
`always_on_bundle_cost`) and the integrity checks (live mandatory set, excluded equals
mandatory union superseded, `detect_redundant` over the shared predicate) move with them.

### 5. last_verified is the verify clock

`last_verified` is separate from `last_validated`, which every parse resets. A live ingest
seeds it to today only where it is missing (an idempotent `WHERE r.last_verified IS NULL`
statement), so the first ingest after this lands starts every rule's clock on that date and
later ingests touch only rules new to the graph. An approval and a verify set it to today.

### 6. STALE and DELIBERATE ride the existing header slot

A rule is STALE when today is past `last_verified` plus its interval (a missing or
unparseable value is never stale) and DELIBERATE when it declares `deliberate: true`. Both are
defined once in `writ/shared/trust.py`. The ranked channel computes `stale` per query in
`_final_rank` (the daemon runs for days, so the tag must follow the clock without a reload)
and carries both through `_HEADER_FIELDS`: `[X-001] (high, STALE) score=0.912`. The always-on
channel renders `[ENF-X-001] (STALE) WHEN: ...` through one shared head-line function. A tag
renders only when true: an untagged rule is byte-identical to before, and tags count against
the existing character limit.

### 7. Edit events are deferred

The event kinds stop at approval, dispute and verify. An ingest-time edit event would need a
corpus-wide pre-image read on every ingest, and `writ edit` runs without an approval token, so
any identity it recorded would be unattested. Adding `edit` when a gated edit path exists is a
one-line change to `TRUST_EVENT_KINDS`.

### 8. Not yet tagged: the write-time rule block

`hooks/scripts/writ-pre-write-dispatch.sh` renders its own APPLICABLE RULES block from the
/always-on rows with an inline formatter, so it still prints `[ID] WHEN: ...` without the
STALE or DELIBERATE tag. The rows already carry `stale` and `deliberate`; tagging that block
needs a parity test against `always_on_head_line`, because the bash formatter cannot import
it. Until then a tagged rule shows its tag in the prompt-time block only.

## Alternatives rejected

- **Identity on Rule (`approved_by`).** The cheapest read, but the dump ships every Rule prop,
  so a person's name would land in the public corpus.
- **History only, no flat props.** Every injection would need a per-rule history read to
  decide a tag; the flat props keep the hot path at zero added statements.
- **A pattern predicate for supersede** (`NOT EXISTS { ()-[:SUPERSEDES]->(r) }`). It needs no
  flag, but the Python replicas see rule dicts, not edges, so each would need its own edge
  read; the derived flag keeps every replica a dict lookup.
- **Identity from the CLI process or its environment.** `USER` and `GIT_AUTHOR_NAME` are set
  by whoever runs the command, which may be the agent; the token is written by the hook on the
  user's keystroke.
- **Reusing `last_validated` as the clock.** It resets on every parse, so no rule could ever
  go stale.

## Trade-offs

- A dump replay (`import-cypher` wipes Rules) resets the Rule flat props to the dump's values
  while the TrustEvent history survives; re-deriving flat props from history is out of scope.
- The git name is unattested display data; only the OS login is an anchor.
- `disputed` is never cleared by this item and has no effect on ranking: resolving a dispute
  is a later decision, and the program never adjudicates.
- A rule created by `writ add` has no `last_verified` until the next ingest, and is never
  STALE meanwhile.
- Each live ingest issues two more statements (the seed and the refresh), each a label scan of
  the project's Rules that writes zero rows in steady state.
