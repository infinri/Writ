<!-- GENERATED FILE - do not edit. Source: writ/server routes. Regenerate with `make docs` (scripts/render-docs.py). -->


# HTTP API reference

All 49 endpoints on `http://localhost:8765`, generated from the FastAPI route table. JSON bodies; no auth (binds localhost only). Logical failures return HTTP 200 with an `error` key; 422 is request validation.

## decision_memory

| Method | Path | Purpose |
|---|---|---|
| POST | `/commit/capture` | Create the Commit + FileChange records for a landed commit (Phase 1d) |
| POST | `/memory-record` | Upsert one auto-memory file as a Memory record (the graph mirror) |
| POST | `/recall` | Compile the project's recent rule-grounded Decisions (Phase 2 recall) |

## explorer

| Method | Path | Purpose |
|---|---|---|
| GET | `/dashboard` | Server-rendered HTML dashboard. No JS framework, auto-refreshes via meta |
| GET | `/explore` | Single-page interactive showcase of Writ |
| GET | `/graph` | Project-scoped node + edge subgraph for the graph explorer (read-only) |
| GET | `/node/{node_id}` | Full props + incident edges for one node (read-only) |

## gate

| Method | Path | Purpose |
|---|---|---|
| POST | `/pre-write-check` | Combined gate check + final-gate check + RAG query for Write/Edit |
| POST | `/session/{session_id}/advance-phase` | Advance to the next workflow phase |
| POST | `/session/{session_id}/promote-candidate` | 6.3c: human-gated, edit-capable promotion of a graduation_pending candidate to canon |

## git_hooks

| Method | Path | Purpose |
|---|---|---|
| POST | `/git-hooks/auto-install` | Install the Writ git hooks into a repo on first work-mode entry (Phase 1d) |

## query

| Method | Path | Purpose |
|---|---|---|
| GET | `/always-on` | Return rules flagged always_on=true for injection into every session |
| POST | `/analyze` | Analyze code against retrieved rules. Returns structured compliance verdict |
| POST | `/conflicts` | CONFLICTS_WITH edges between provided rules |
| POST | `/feedback` | Record positive or negative feedback for a rule |
| GET | `/health` | Service status, rule count, index state, last ingestion timestamp |
| POST | `/methodology-companion` | Methodology by workflow-state (floor u push u pull) -- CHANNEL 2 (1.5) |
| POST | `/prompt-bundle` | #8: the three per-prompt injection channels in ONE warm call |
| POST | `/propose` | Propose an AI-generated rule. Runs structural gate, ingests if accepted |
| POST | `/query` | Ranked list of matching domain rules. Mandatory rules excluded |
| GET | `/rule/{rule_id}` | Full rule node. Optionally includes 1-hop graph context |
| GET | `/subagent-role/{name}` | Return a SubagentRole node's canonical prompt template from the graph |

## session_state

| Method | Path | Purpose |
|---|---|---|
| POST | `/session/format` | Format a query response for injection into Claude's context |
| GET | `/session/{session_id}` | Read the full session cache |
| GET | `/session/{session_id}/active-playbook` | Read the session's active playbook + phase + history |
| POST | `/session/{session_id}/active-playbook` | Set active playbook and phase. body: {playbook_id, phase_id, total_steps?} |
| POST | `/session/{session_id}/add-pending-violation` | Add a pending violation to the session |
| POST | `/session/{session_id}/auto-feedback` | Trigger auto-feedback correlation for the session |
| POST | `/session/{session_id}/can-write` | Check whether a file write is allowed |
| GET | `/session/{session_id}/check-escalation` | Check whether escalation is needed |
| POST | `/session/{session_id}/clear-pending-violations` | Clear pending violations for the session |
| POST | `/session/{session_id}/clear-rules-for-compaction` | Clear loaded_rules from cache before compaction (PreCompact) |
| POST | `/session/{session_id}/context-percent` | Set context_percent for the session |
| GET | `/session/{session_id}/coverage` | Get rule coverage for the session |
| GET | `/session/{session_id}/current-phase` | Get the current phase for the session |
| POST | `/session/{session_id}/invalidate-gate` | Invalidate a gate: record the cycle, delete the .approved file, check escalation |
| GET | `/session/{session_id}/mode` | Get the current mode for the session |
| POST | `/session/{session_id}/mode` | Set the mode for the session |
| GET | `/session/{session_id}/pending-violations` | Get pending violations for the session |
| GET | `/session/{session_id}/prompt-state` | Everything the RAG hook asks about a session, in one call and one cache read |
| GET | `/session/{session_id}/quality-judgment` | Read all quality judgments plus the override count for the session |
| POST | `/session/{session_id}/quality-judgment` | Record a Gate 5 Tier 2 (self-scored) quality score for an artifact |
| POST | `/session/{session_id}/reset-after-compaction` | Reset budget and clear phase exclusion list after compaction (PostCompact) |
| GET | `/session/{session_id}/review-findings` | The latest recorded reviewer verdict and whether it blocks a commit |
| POST | `/session/{session_id}/review-findings` | Record a reviewer verdict for the session. The latest one wins |
| GET | `/session/{session_id}/should-skip` | Check whether RAG queries should be skipped for this session |
| POST | `/session/{session_id}/update` | Update a single key in the session cache |
| GET | `/session/{session_id}/verification-evidence` | Read verification evidence. Pass ?todo_id=X for a single entry, omit for all |
| POST | `/session/{session_id}/verification-evidence` | Record verification evidence for a completion claim |
