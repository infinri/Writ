---
name: writ
description: >
  Hybrid RAG knowledge retrieval service for AI coding rule enforcement.
  Replaces flat-file rule loading with graph-native retrieval via a five-stage
  pipeline (domain filter, BM25 keyword, ANN vector, graph traversal, RRF ranking).
  Rules are injected automatically into Claude's context via hooks -- no manual
  loading required. Activates on ALL software engineering tasks: code generation,
  code review, design, research, auditing, debugging, testing, planning, and
  architecture decisions. The knowledge base is always queried -- task complexity
  only determines the level of ceremony, not whether Writ activates.
metadata:
  author: lucio-saldivar
  version: "1.0"
---

# Writ -- Hybrid RAG Rule Retrieval

## How rules reach you

Rules are injected automatically by the `writ-rag-inject.sh` hook on every user turn.
You do not need to read rule files manually. The hook:

1. Takes the user's prompt as a natural language query
2. POSTs to `http://localhost:8765/query` with session state (budget, loaded IDs)
3. Writ's pipeline ranks rules by relevance (BM25 + semantic + graph proximity)
4. Returns rules formatted as a `--- WRIT RULES ---` block in your context

If no rules appear, either the server is down (you'll see a warning) or the prompt
was too short (< 10 chars) or budget was exhausted.

## What the rules look like

```
--- WRIT RULES (N rules, <mode> mode) ---

[RULE-ID] (severity, authority, domain) score=N.NNN
WHEN: trigger condition
RULE: what must be done
VIOLATION: example of doing it wrong
CORRECT: example of doing it right

--- END WRIT RULES ---
```

## Rule authority

- **human** -- highest trust, manually authored
- **ai-promoted** -- AI-proposed, graduated via frequency tracking (n>=50, ratio>=0.75)
- **ai-provisional** -- AI-proposed, not yet graduated, lowest trust

Human rules outrank AI rules at equal relevance (hard preference rule).

## Session deduplication

A session cache tracks which rules have already been loaded. Subsequent turns
exclude previously loaded rules via `exclude_rule_ids`. The token budget starts
at 8000 and decrements per query. When exhausted, the hook silently skips.

## Development enforcement

Writ ships a complete hook-based development framework that works with any language
or framework. Hooks enforce gate sequencing (Phases A-D), static analysis, handoff
validation, and context hygiene. See `.claude/CLAUDE.md` for the full hook inventory.

Gate categories support: PHP, Python, JavaScript, TypeScript, Go, Rust, Java, Ruby,
and framework-specific patterns for Magento 2, Django, Rails, Spring, NestJS, Express,
Laravel. New languages and frameworks are added via `bin/lib/gate-categories.json`.

Static analysis routes by file extension: PHPStan, ESLint, ruff, xmllint, cargo check,
go vet. New analyzers are added via `bin/run-analysis.sh`.

## Server requirements

Writ requires:
- Neo4j running at `bolt://localhost:7687`
- Writ server: `writ serve` (default: `localhost:8765`)
- Bible rules ingested: `writ ingest`
- Install hooks: `bash scripts/install-skill.sh`

## Proposing new rules

When you encounter a pattern not covered by existing rules, propose it via
`POST /propose`. See `.claude/CLAUDE.md` for the full request format. Proposed
rules enter as `ai-provisional` and must pass the structural gate before ingestion.

## Architecture reference

- Server: `writ/server.py` (FastAPI, async)
- Pipeline: `writ/retrieval/pipeline.py` (5-stage hybrid)
- Schema: `writ/graph/schema.py` (Rule, Abstraction, Edge models)
- Session: `writ/retrieval/session.py` (client-side budget tracker)
- Config: `writ.toml` (all tunable parameters)
- Hooks: `.claude/hooks/` (RAG injection, gate enforcement, validation)
- Verification: `bin/` (static analysis, gate checks, dependency scanning)
- Gate categories: `bin/lib/gate-categories.json` (language/framework patterns)
- Full spec: `RAG_arch_handbook.md`
