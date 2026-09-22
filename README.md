# Writ

[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

**Claude Code can forget your rules. Writ can refuse the action.**

Writ is a local governance, context, and continuity runtime for Claude Code. It enforces selected boundaries when tools run, delivers the rules and methodology relevant to the work happening now, and preserves project memory and decision history across sessions.

| Writ provides | What that means |
|---|---|
| **Action** | Tool-time controls can allow, pause, confirm, or refuse an attempted action. |
| **Context** | Relevant rules, skills, and methodology arrive when the current task, file, tool, or workflow phase requires them. |
| **Continuity** | Decisions and project memory persist across sessions, while best-effort handoffs preserve active workflow state across context compaction. |

Ready to try it? Jump to [Install](#install), or read [how enforcement works](#how-enforcement-works).

## Why it exists

An instruction and an enforcement point are different things. A system prompt, a `CLAUDE.md`, a methodology document: all of them ask the model to remember. That works until context fills, a session compacts, or the model decides the rule does not apply this time.

Writ does not replace instructions. It adds the second primitive for the parts of your process you choose to gate, so those parts hold whether or not the model is still paying attention.

## How enforcement works

You tell Writ what kind of work you are doing. That is the mode. Conversation and review remain lightweight. Investigate and debug add evidence tracking and activity-specific controls. In Work mode, source writes are blocked until a human opens the plan and test gates.

Claude tries to write code before a plan has been approved:

```text
[ENF-GATE-PLAN] ALL writes blocked -- plan not yet approved. DO NOT attempt more writes.
Present your plan to the user and say: "Say approved to proceed."
Wait for the user to say "approved" before attempting ANY file writes.
```

You read the plan and type `approved`. That opens the first gate. The second gate requires at least one assertion-bearing test. You review the tests and approve them too. After both gates open, implementation writes are allowed while the remaining safeguards continue to operate.

The same boundary layer also covers credential paths, writes made through the shell, recognized external-data transfers, debugging, review findings, and sub-agent authorization.

**The agent cannot approve itself.** Opening a gate spends a one-time secret, and that secret is created only when *your own typed message* matches an approval phrase. An agent that tries finds nothing to spend, is refused, and the attempt is written to the audit log. Editing the plan after approving it re-arms both gates, so an approval covers the plan you actually read.

## Install

**You will need Claude Code, Python 3.11 or newer, and Docker.** Python is a real requirement, not a packaging convenience: the enforcement logic runs in it. Docker runs the graph database that stores Writ's rules, methodology, project memory, and decision records.

### Let Claude Code do it

You already have an agent that reads instructions and runs commands. Point it at this page:

> Install Writ from https://github.com/infinri/Writ. Verify that Python 3.11+ and Docker are available, follow the plugin installation instructions, run the bootstrap command Writ prints after Claude Code starts, restart Claude Code, and verify that the Writ service is healthy. Stop and explain anything that requires me to install or approve it manually.

Claude Code can do the project setup. It cannot install Docker or Python for you, so if either is missing you will be asked to handle that part yourself.

### Or run it yourself

```shell
claude plugin marketplace add infinri/Writ
claude plugin install writ@writ
```

Open Claude Code once. It notices the un-bootstrapped install and prints a single absolute command on its own line, ready to paste. Run it, then restart Claude Code.

That one script does the rest: environment, database, rules, background service, permissions. It is idempotent, so re-running it after an update is the whole update procedure.

### Check it worked

```shell
writ status
```

Nothing breaks while you are partway through setup. Hooks stay out of the way until the install finishes, sessions are never blocked, and the startup hook prints what is still missing. Both install paths and troubleshooting are in [`docs/install.md`](docs/install.md).

## What Writ controls

### Action

| | |
|---|---|
| **Plan and test gates** | Work mode requires an approved plan, then approved tests, before implementation writes are allowed. |
| **Human-only approval** | The approval token is minted only from your typed words. The agent has no path to creating one, and the attempt is recorded when it tries. |
| **Credential protection** | Writes to keys, `.env` files, and SSH material are refused in every mode. The path is classified without the file ever being opened. |
| **Shell writes** | Writes made through redirection, `tee`, `cp`, or `sed -i` go through the same check as tool writes, not a separate weaker one. |
| **External data** | Outbound commands carrying a payload to a host outside your allowlist ask for confirmation. Writ cannot tell what a payload contains, so it asks rather than guessing. |
| **Debug gate** | In debug mode, source edits are refused until a root cause is written down. The refusal names the file and section that lifts it. |
| **Review escalation** | A serious review finding turns the next commit into a confirmation naming the unresolved findings. The agent cannot clear its own verdict. |
| **Sub-agent authorization** | A helper agent is refused any path its orchestrator would be refused at that moment, and each role carries its own write scope. |
| **Completion checks** | Pending tests, unresolved rule violations, and failed quality verification can stop a turn before the agent declares the work complete. |
| **Audit records** | What was allowed, what was refused, and why, in a stream kept separate from operational logs. |

### Context

Writ delivers both engineering rules and reusable methodology, including skills, playbooks, techniques, and known failure patterns. Delivery is driven by what is happening now: the prompt, the file being written, the tool running, and the workflow phase.

| | |
|---|---|
| **Selective delivery** | Rules arrive when they apply, so the rulebook can grow without every turn growing with it. |
| **A floor that cannot be ranked away** | Mandatory content is held out of the relevance ranking entirely and delivered on its own channel, so no retuning can drop it. |
| **Hybrid retrieval** | Combines exact-term and semantic search, then uses graph relationships to bring in connected guidance. |
| **Abstention** | When nothing is relevant enough, Writ delivers nothing rather than filling the turn with noise. |
| **Tool-output compression** | Large Write and Edit responses are stripped of redundant full-file echoes while preserving the patch and fields Claude still needs. |
| **Budgets** | Per-session and per-channel limits, so context is spent rather than flooded. |

### Continuity

Three different mechanisms, answering three different questions.

| Question | Mechanism |
|---|---|
| *Why was this code changed?* | **Decision memory.** Approved plan, governing rules, changed files, and the resulting commit are linked mechanically, not inferred from the conversation. The record replays as a new-session briefing, as git notes, and on supported pull requests. `writ recall` reads it back. |
| *What project knowledge did Claude save?* | **Auto-memory mirror.** Claude Code's own memory files are mirrored into the graph as they are written, scoped per project. `writ memory backfill`, `writ memory list`, and `writ memory audit` cover existing files, inspection, and scope errors. Deletions are tombstoned rather than destroyed. |
| *Where was the agent when its context disappeared?* | **Best-effort compaction handoff.** When the conversation is compacted, Writ writes a derived record of mode, phase, approvals, the files the plan declared, the files actually written, unfinished items, and the rules that were loaded. The next prompt points the agent at it. |

Writ keys these records by project and scopes its normal recall, memory, and retrieval paths accordingly. The shared rules and methodology corpus remains available across projects, by design.

**The auto-memory mirror is not version history.** A memory record reflects its latest contents. `writ memory audit` reports scope and disk drift but intentionally performs no automatic repair.

## Modes

| Mode | Purpose | Governance behavior |
|---|---|---|
| `conversation` | Discussion and brainstorming | No workflow gates |
| `review` | Evaluate code against applicable rules | Adds no workflow gates and supplies relevant review guidance |
| `investigate` | Research and codebase exploration | Captures web citations and command evidence, and reports whether sources span independent domains |
| `debug` | Diagnose a specific failure | Blocks source edits until a root cause is written down; evidence and narrowing are recorded alongside it, but do not themselves gate the write |
| `work` | Build or modify code | Requires an approved plan, then approved tests, before implementation |

**Dedicated reviewer.** Writ also ships a separate read-only reviewer that examines the actual diff from a fresh context and has no tool capable of editing the code.

Modes can be suggested automatically, set explicitly, or switched temporarily. Switching out of Work and back restores your paused phase and approvals if the plan is unchanged, and re-arms both gates if it changed while you were away.

## How rules evolve

Writ can record feedback and accept agent-proposed rules, but a proposal does not silently become policy. A candidate stays provisional until a human promotes it, and promotion spends the same one-time approval secret as everything else. The agent may propose. It cannot promote its own proposal.

This is a control that exists, not evidence that automatically proposed rules are effective.

## Day-to-day use

For a typical Work-mode change, two approvals: you read the plan and type `approved`, then you read the tests and type `approved`. After that the agent works without interrupting you, with one exception, which is a serious review finding adding a confirmation before the commit lands.

The non-Work modes add no approval steps.

Writ's rule corpus, project records, session state, and logs remain local. Optional Bitbucket Cloud synchronization sends per-file decision context only when configured. Installation downloads Python dependencies and the local embedding model. [`SECURITY.md`](SECURITY.md) documents the complete trust model.

For operators: `writ doctor` diagnoses a live install, `writ trust-ledger` records the skills, agents, and local MCP configuration you have accepted and reports when they change, and typed log streams separate governance decisions from operational noise.

## Who it is for

Engineers and engineering leads who need parts of a coding-agent workflow governed outside the model rather than left as instructions, and who can answer for the code afterward. Writ ships opinionated plan-first and test-driven defaults, with the mode and gate machinery underneath them.

**Who it is not for.** If you need enforcement against an agent that is actively adversarial, this is not that tool. Writ constrains a cooperative agent. [`SECURITY.md`](SECURITY.md) writes the gaps down rather than glossing them.

The shipped rulebook is opinionated and reflects where its author has worked. Treat it as a working example rather than a universal standard; commands exist for adding and editing your own.

## Limits

These change what a block means, so they are stated here rather than buried.

- **When the background service is unreachable, hooks allow rather than block.** This is the specification, not a bug: an infrastructure outage must never lock you out of your own repository. Setting `WRIT_STRICT=1` inverts that for the write path.
- **The gate can tell that a plan exists. It cannot tell whether the plan is any good.** The validators check shape, not thought. A plausible plan and a careful one look identical to a machine. Writ relocates oversight; it does not remove it.
- **Some controls report rather than refuse.** The investigate-mode source check and the trust ledger record what they find and leave the judgment to you. The debug gate and the write gates refuse.
- **The egress guard asks, it does not block.** It cannot tell whether an outbound payload carries repository material, so it surfaces the decision instead of guessing.
- **Shell inspection is pattern-based, not a sandbox.** Writ recognizes common redirections, copy destinations, and interpreter one-liners, but it cannot detect every write assembled through wrappers, variables, modules, or `eval`. The known gaps are documented in [`SECURITY.md`](SECURITY.md).

Two boundaries hold regardless, including when the service is down and inside sub-agents. Writes to credential files are refused in every mode with no server involved. And the approval token cannot be created or spent without a human keystroke, so advancing the workflow and writing new rules into the rulebook halt even when raw file writes do not.

Pull request comments currently support Bitbucket Cloud only. The session briefing and git notes work anywhere.

## Evidence

Writ distinguishes three kinds of claim, and so should you when reading anything here.

- **Mechanisms that are implemented and testable.** The gates, the approval binding, the credential classifier, the handoff. You can read these in the source and trigger them yourself.
- **Measured outcomes.** Retrieval quality, retrieval cost, and how rule text per turn behaves as the rulebook grows. Method, figures, and corrections live in [`SCALE_BENCHMARK_RESULTS.md`](SCALE_BENCHMARK_RESULTS.md), which owns those numbers so this page does not go stale carrying copies.
- **Claims not yet demonstrated.** That an agent handed the right rule complies more often than one handed nothing. The harness exists and reports its own result as insufficient. See [`docs/reference/efficacy-ab.md`](docs/reference/efficacy-ab.md).

Two things are independently checkable before you install anything. [`docs/pressure-runs/`](docs/pressure-runs/) holds adversarial runs against real Claude Code sessions, each with the prompt, the full transcript, every enforcement decision as raw log lines, and a graded analysis of which rules held and which were bypassed, with the failures written up as failures. [`docs/monthly-reviews/`](docs/monthly-reviews/) holds operational reviews built from the system's own audit log.

## Documentation

| | |
|---|---|
| [`HANDBOOK.md`](HANDBOOK.md) | The operator manual. Modes, gates, helper agents, the rulebook, the command line. |
| [`docs/install.md`](docs/install.md) | Both install paths, background service, troubleshooting. |
| [`docs/instructions-vs-enforcement.md`](docs/instructions-vs-enforcement.md) | Why an instruction and an enforcement point are different primitives. |
| [`docs/reference/`](docs/reference/) | Precise contracts: architecture, schema, retrieval, sessions and gates, configuration, logging, decision memory, testing. |
| [`SECURITY.md`](SECURITY.md) | The trust model, what leaves the machine, and how to report a vulnerability. |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Authoring rules, review cadence, triaging agent proposals. |
| [`CHANGELOG.md`](CHANGELOG.md) | Release history. |
| [`ERRATA.md`](ERRATA.md) | Corrections to figures this project has published. |

[`docs/reference/claude-code-blackbox.md`](docs/reference/claude-code-blackbox.md) is reference material that stands on its own: an empirical map of what Claude Code hands a hook and what a hook can hand back, with an evidence tag on every field. Useful whether or not you use Writ.

**Architecture in your browser:**
[overview](https://infinri.github.io/Writ/docs/architecture/index.html) |
[data model](https://infinri.github.io/Writ/docs/architecture/data-model.html) |
[retrieval](https://infinri.github.io/Writ/docs/architecture/retrieval-pipeline.html) |
[injection channels](https://infinri.github.io/Writ/docs/architecture/injection-channels.html) |
[graph explorer](https://infinri.github.io/Writ/docs/architecture/knowledge-graph.html) |
[corpus round trip](https://infinri.github.io/Writ/docs/architecture/corpus-roundtrip.html)

## Contributing and support

Found a bypass, or a case where a rule you relied on did not hold? That is the most valuable thing you can send. Open an issue with the transcript. For anything exploitable, report privately through [GitHub Security Advisories](https://github.com/infinri/Writ/security/advisories/new) rather than in public.

Want to contribute rules or code? [`CONTRIBUTING.md`](CONTRIBUTING.md) covers authoring, the review cadence, and how agent-proposed rules are triaged.

Writ is free and developed in my own time. If it saves you some of yours, you can optionally support its continued development with [a coffee](https://buymeacoffee.com/infinri).

## Acknowledgements

**[Superpowers](https://github.com/obra/superpowers), by Jesse Vincent**, for formalizing the discipline Writ builds enforcement around. The architectural disagreement is argued in [`docs/instructions-vs-enforcement.md`](docs/instructions-vs-enforcement.md#superpowers-and-the-definition-of-mandatory).

**[Jolli](https://www.jolli.ai/), by [JolliAI](https://github.com/jolliai/jolliai)**, for work on preserving development reasoning after a session ends. Writ's digest eviction policy is adapted from Jolli's ContextCompiler, the policy rather than the code, and `writ/session/recall.py` documents the adaptation.

---

License: MIT. Authored by Lucio Saldivar.
