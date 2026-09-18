# Writ

[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

**Claude Code can forget your rules. Writ can refuse the action.**

Writ is a governance runtime for Claude Code. It moves important engineering controls outside the model, where they can be enforced, retrieved, and remembered independently of what the model happens to keep in context.

* **Enforce.** Selected workflow boundaries run as code at tool time, so an action can be refused rather than discouraged.
* **Inform.** Rules reach the agent when they apply, based on the task, file, tool, and workflow phase in front of it.
* **Remember.** Approved plans, the rule IDs that governed them, changed files, and commits become connected provenance.

Most coding-agent systems ask the model to remember the process. Writ puts selected parts of the process around the model instead.

Those are mechanism claims, and you do not have to take them on faith. [`docs/pressure-runs/`](docs/pressure-runs/) holds adversarial runs against real Claude Code sessions, each with the prompt used, the full transcript, every enforcement decision as raw log lines, and a graded analysis of which rules held and which were bypassed, with the failures written up as failures. [`docs/monthly-reviews/`](docs/monthly-reviews/) holds operational reviews built from the system's own audit log. Both are in the repository, dated, and readable before you install anything.

Every number in this file is either measured and dated, or derived from the current source tree. Where this file and the code disagree, the code wins.

## See it refuse, in about a minute

You tell Writ what kind of work you are doing. That is the mode. In the read-only modes (conversation, review, investigate) Writ hands over relevant rules and otherwise stays quiet. In **Work mode**, writes to your source code are blocked until two gates open.

**The refusal.** Claude attempts a write in Work mode before a plan has been approved. This is exactly what the gate returns, quoted byte for byte from the string literal at [`writ/session/gates.py`](writ/session/gates.py) lines 803-806, inside the gate arm at 801-809:

```text
[ENF-GATE-PLAN] ALL writes blocked -- plan not yet approved. DO NOT attempt more writes.
Present your plan to the user and say: "Say approved to proceed."
Wait for the user to say "approved" before attempting ANY file writes.
```

**The human opening the gate.** You read `plan.md` and type "approved", or you run `/writ-approve`, the one slash command Writ ships. The next gate wants a test file that actually asserts something: write it, approve it, and it opens the same way. After both gates clear, the AI writes implementation code freely. **The AI cannot approve itself.** Opening a gate consumes a one-time secret written to a temporary file, and that secret is created only when *your typed message* matches an approval phrase. An AI that tries to open its own gate finds no secret, gets refused, and the attempt is written to the audit log as `agent_self_approval_blocked`. [`HANDBOOK.md` section 7](HANDBOOK.md#7-anti-self-approval-security-model) carries the whole model.

**The provenance.** Afterwards, what was approved, which rules governed it, which files changed, and which commit resulted reads back through either of these:

```shell
writ recall
git log --notes=writ-decisions
```

The first reads the project's recent rule-grounded decisions back from the graph. The second reads the same content out of git itself, with no server involved.

That block is not unconditional, in three named ways: when the background service is unreachable hooks allow rather than block, subagents skip the write gates by design, and the gate can tell that a plan exists but not whether the plan is any good. [Evidence and limits](#evidence-and-limits) states each one in full.

## Install

**You do not have to install this yourself.** If you are reading this you already use Claude Code, which means you already have something that reads instructions and runs commands. Point it at this page and ask it to install Writ. It handles the setup; the one piece you may need to do by hand is installing Docker, the same way you would install any other application.

**You will need:** Python 3.11 or newer and Docker (the graph database runs in a container). That is the whole list. `jq` and `curl` are used when present and fall back to Python when absent, so a machine without them installs fine.

```shell
claude plugin marketplace add infinri/Writ
claude plugin install writ@writ
```

Open Claude Code once. It detects the un-bootstrapped install and prints one absolute command on its own line, ready to paste:

```shell
bash /path/it/prints/scripts/bootstrap-plugin.sh
```

Run it and restart Claude Code. That one script does everything: environment, database, rules, background service, permissions, and workflow instructions. It is idempotent, and re-running it after an update is the whole update procedure. Check it worked with `curl http://localhost:8765/health`.

Nothing breaks while you are partway through setup. Hooks stay out of the way until the install finishes, sessions are never blocked, and the startup hook prints exactly what is still missing. Full install detail, the manual path, and troubleshooting live in [`docs/install.md`](docs/install.md). Once it is running, [`HANDBOOK.md`](HANDBOOK.md) is the operator manual: modes, gates, helper AIs, the rulebook, and the command line.

## What Writ controls

Writ governs three things. **Action**: what the agent is permitted to do. **Context**: which engineering rules govern the current action. **Continuity**: why the action was approved and what future sessions should know. Hooks and gates are the Action mechanism, retrieval over the rule graph is the Context mechanism, and decision provenance is the Continuity mechanism.

```text
                         W R I T

  User request
      |
      v
  Engineering rules -----+
  Human approvals -------+
  Prior decisions -------+--> Writ --> Claude Code
                                         |
                                         | tries an action
                                         v
                                  Writ checks the action
                                         |
                                 allow / ask / refuse
                                         |
                                         v
                                     Repository
                                         |
                                         v
                                 decision provenance
```

### Action

Selected workflow boundaries run as code at tool time. A Work-mode implementation can be stopped until a human has approved its plan and its tests, and credential writes and other protected actions have their own guards in every mode.

Instructions and enforcement are different primitives, and Writ supplies the second one for the parts of the process you choose to gate. It sits between the AI and your files. In Work mode, a write attempted before you have approved a plan is refused. Not discouraged, refused, by code that runs whether or not the AI is still paying attention to what you said an hour ago. Why that distinction is architectural rather than rhetorical is argued in [`docs/instructions-vs-enforcement.md`](docs/instructions-vs-enforcement.md).

| Mode | For | What it blocks |
|---|---|---|
| `conversation` | Talking, asking, thinking out loud | Nothing |
| `review` | Judging code against the rules | Nothing |
| `investigate` | Auditing, exploring, researching | Web research cannot be summarized until sources come from two independent sites |
| `debug` | Chasing one specific failure | Source edits, until you have written down a root cause |
| `work` | Building or changing code | Source writes, until the plan gate and the test gate both open |

### Context

Rules reach the agent when they apply, based on the task, file, tool, and workflow phase in front of it. Seven universal process and gate rules form a small always-on floor. Other mandatory rules are scoped to the actions they protect, while the rest of the rulebook is retrieved by relevance. Rules that do not apply stay out of context.

**Enforcement solves only half the problem.** A large rulebook cannot simply be pasted into every turn, so Writ also moves rule selection outside the model. It looks at the work happening now and delivers only the rules that apply. Tool-time checks do not depend on the model remembering the process, and contextual delivery lets the rulebook grow without the cost of every turn growing with it.

**The floor: rules that can never be dropped.** Thirty-two of the 288 shipped rules are marked mandatory. These are deliberately kept **out of the search index entirely** and delivered through a separate channel with its own budget, so no change to ranking, no swap of the underlying model, and no retuning of anything can cause a mandatory rule to fall out of delivery because of ranking. Seven of them carry universal scope and inject on every turn; the other 25 are scoped to writes and keyword-gated, so they arrive the moment a write matches them rather than every turn. Both counts are derived from [`writ-corpus.cypher`](writ-corpus.cypher), the tracked canonical dump, and the mechanics are in [`docs/reference/retrieval.md`](docs/reference/retrieval.md).

**Everything else is searched for.** Writ currently uses a five-stage retrieval pipeline over a Neo4j knowledge graph: narrow the candidates, keyword search, meaning-based search (so a rule about "SQL" surfaces for a question about "database queries"), a walk across the graph to pull in related rules, then weighted ranking. Those five stages are the ones listed at the top of [`writ/retrieval/pipeline.py`](writ/retrieval/pipeline.py), and each is designed to cover a different retrieval failure mode: keyword search catches exact terms, meaning-based search catches paraphrase, and the graph walk reaches rules that share no words with the query at all but are linked to a match. If nothing matches well enough, the pipeline **returns nothing** rather than injecting noise.

**The search fires on what is happening, not just what you typed.** Writ's hooks observe the session across the twelve Claude Code events they register for: prompts, file reads, writes, shell commands, subagent start and stop, compaction, and session lifecycle. They attach real context to the query: which file is being written, what is inside it, which tool is running, and what phase the workflow is in. So while Claude works, editing a file whose code touches SQL can pull the parameterized-query and injection rules into context at that moment, even if your prompt never mentioned SQL.

### Continuity

Approved plans, the rule IDs that governed them, changed files, and commits become connected provenance: recorded in the graph, pushed onto pull requests and git notes, and compiled into a briefing for future sessions. The record answers a governance question: under what approved plan and governing rules did this change occur, and which files and commit resulted?

This is not conversational memory. Writ does not read your chat history and guess what mattered; it builds the record mechanically, from things that already exist. When you commit, a git hook joins the commit's files against the **approved plan**, the rules each session and helper AI actually looked up for each file, and any earlier open decisions. The hook never blocks a commit and does nothing harmful when the service is down, and a backfill command reconstructs the history for commits made before you installed it. Every decision stays tied to the rule identifiers that governed it, and those survive every round of trimming when the record gets too large.

The record plays back in three places:

* **A session briefing.** Recent decisions get compiled into a size-limited digest, and the top of it is injected into your first message of a new session, so the AI starts knowing what was decided and why.
* **Pull request comments.** One comment per changed file: why it changed, which rules the AI was shown, and which it cited. It updates its own comments rather than piling up duplicates, so reviewers read the reasoning next to the diff instead of reconstructing it.
* **Git notes.** The same content is written into git itself, which needs no server and travels with the repository anywhere.

Full detail in [`docs/reference/decision-memory.md`](docs/reference/decision-memory.md).

## One ordinary feature, end to end

This is the sequence [`HANDBOOK.md` section 2](HANDBOOK.md#2-a-single-turn-end-to-end) and [section 6](HANDBOOK.md#6-the-work-mode-gates) describe, with the real artifacts and strings named.

1. **You ask for the feature.** The prompt hook health-checks the daemon, reads the session cache once, and makes a single `POST /prompt-bundle` call that runs the ranked query, the always-on floor, and the methodology companion together. What comes back is printed as a `--- WRIT RULES ---` block followed by a status line, `[Writ: mode=work, phase=implementation, gates=[], violations=0]`.
2. **`mode set work`.** The phase becomes `planning`, and writes to source are blocked.
3. **The plan.** `plan.md` must exist with four sections: `## Files` (each entry a path, change type, and reason), `## Analysis`, `## Rules Applied` (citing real rule IDs from the rules actually loaded this session; invented IDs are flagged as hallucinated), and `## Capabilities` (unchecked `- [ ]` checkboxes; pre-checked boxes are rejected). You read it and type "approved". Gate `phase-a` opens and the phase becomes `testing`. One approval advances exactly one gate, and a validation failure *spends* the token, so a changed artifact needs a fresh approval.
4. **The test skeletons.** At least one test file with real assertions must exist. You read them and approve. Gate `test-skeletons` opens, the phase becomes `implementation`, and source writes flow freely.
5. **Implementation.** The hooks keep watching. `PreToolUse` on Write, Edit and NotebookEdit goes through one combined `/pre-write-check` call carrying the gate decision, the denial escalation, and file-context retrieval, and denials carry a `[RULE-ID]`-prefixed reason. Bash-mediated writes (`>`, `tee`, `cp`, `sed -i`) go through the same check, and credential paths are denied in every mode.
6. **The review.** A recorded CRITICAL verdict turns the next `git commit` into a confirmation prompt naming the unresolved findings. It is a stop and ask, not an absolute block: you can confirm and commit anyway, and that choice is recorded in the audit log. The AI cannot clear its own verdict, and an unreadable verdict blocks the same way a critical one does, so the only route it has to lifting a block is to fix the findings and earn a fresh clean verdict. [`HANDBOOK.md` section 6](HANDBOOK.md#6-the-work-mode-gates) carries the rest.
   **What makes the reviewer worth listening to is structural, not a count of reviewers.** It reads the diff between two commit SHAs, so it judges what actually changed rather than what it was told changed. It starts from an empty context: it inherits no conversation from the AI that wrote the code, so it cannot adopt the author's framing of why a shortcut was fine. It holds no Write or Edit tool (`tools: Read Glob Grep Bash` in [`agents/writ-reviewer.md`](agents/writ-reviewer.md)), so it can report a problem and cannot quietly fix one. And it checks the spec before the style, because polishing code that builds the wrong thing is wasted work. Those four are readable in the agent file; whether that independence catches more defects than a same-context review is a separate question, and it is unmeasured.
7. **The commit.** The post-commit hook joins the commit's files against the approved plan and the rules that governed the session, and writes the records into the same graph as the rulebook.

## Who should use this, and who should not

**What it costs you.** For a typical Work-mode change, Writ adds two approvals: you read the plan and type "approved", then you read the tests and type "approved". After that the AI writes code without interrupting you again, with one exception: a review finding something serious adds a confirmation before the work is committed. So usually two, occasionally three. The other four modes add no approvals at all. The rulebook lives in a database on your own machine, so you need Docker installed, which is a normal application download. Your rules and your code stay on your machine; [`SECURITY.md`](SECURITY.md) lists the one thing that leaves and when.

**What you get for that.** On Writ's guarded path, configured gates are checked at tool time. Either their conditions have been satisfied or the attempted action is refused. When something is refused, there is a record of what was refused and why, which is the part that matters if you are the person answering for the code rather than writing it.

**Who it is for.** Engineers and engineering leads who need important coding-agent workflows to be governed outside the model rather than left as instructions. Writ ships opinionated plan-first and test-driven defaults, and its mode and gate system provides the machinery underneath them.

**Who it is not for.** If you need enforcement against an AI that is actively adversarial, Writ is not that tool. Writ constrains a cooperative agent; it is not an adversarial sandbox, and [`SECURITY.md`](SECURITY.md) writes the gaps down rather than glossing them.

Writ deliberately trades some setup and workflow friction (Python, Docker, Neo4j, a background service, hooks, workflow state) for stronger control over agent behavior. The question is whether those guarantees are worth that tradeoff for your work. [What Writ controls](#what-writ-controls) documents exactly what it controls; [Evidence and limits](#evidence-and-limits) documents what it does not, and the evidence available today.

**The rulebook is opinionated.** 288 rules ship in the box: 76 security, 45 code quality, 28 architecture, 21 testing, 19 performance, 19 process, and smaller sets besides. The shape reflects where its author has worked. There are 12 Magento 2 rules and exactly one PHP typing rule, which tells you something true about where it came from. Treat the shipped rulebook as a working example, not a universal standard: commands for adding and editing rules exist so you can grow your own, and there is a full lifecycle for rules the AI itself proposes, described in [`HANDBOOK.md` section 12](HANDBOOK.md#12-how-rules-grow-propose-graduate-promote), which ends in a human approval requiring the same one-time secret as everything else. The statistics never promote anything on their own.

## Evidence and limits

The gates and the retrieval are measured, and the figures below are dated. The central claim, that an agent handed the right rule complies more often than one handed nothing, is not measured. The A/B harness exists; at one repetition it reports its own result as insufficient by design.

* **Search quality.** 0.923 hit rate at 5 across the 169 index-eligible questions of the gold set, and 0.608 mean reciprocal rank at 5 across the 47 deliberately ambiguous ones (2026-08-06).
* **Search cost.** A warm 95th percentile of 0.827 ms in the published synthetic run against 10,000 rules (2026-08-01).
* **Rule text per turn stays roughly flat as the rulebook grows.** About 2,000 tokens against the live 287-rule corpus (2026-08-05), about 1,590 against the 10,000-rule synthetic one (2026-08-01).
* **The floors are gates, not aspirations.** Seventeen benchmark targets run in continuous integration on every push and every pull request, and they passed 17 of 17 on 2026-08-14.

Every figure above is a dated measurement, not a live readout, taken on one developer machine with an uncapped database container, so your numbers will differ.

Three limits on what a block means, stated here rather than further down where they would look buried:

* **When the background service is unreachable, hooks allow rather than block.** This is the specification, not a bug. An infrastructure outage must never lock you out of your own repository.
* **Subagents (helper AIs spawned by the main one) skip the write gates by design.** Their limits come from the tools their role grants them, not from re-checking work the human already approved.
* **The gate can tell that a plan exists. It cannot tell whether the plan is any good.** The validators confirm the shape of the thing, not the thought behind it. A plausible plan and a careful one look identical to a machine, so this replaces none of your judgement, and reviewing the work is still your job. Writ relocates oversight. It does not remove it.

Two boundaries hold no matter what, including when the background service is down and inside subagents. Writes to credential files (keys, `.env`, SSH material) are refused in every mode with no server involved. And the approval token cannot be created or spent without a human keystroke, so **advancing the workflow and writing new rules into the rulebook halt even when raw file writes do not.**

Pull request comments currently support Bitbucket Cloud only, and self-hosted Bitbucket Server is explicitly rejected rather than silently broken. The briefing and git notes channels work anywhere.

A second thing was unproven and is no longer. The graph traversal stage was the reason this project needed a graph database at all, and its individual contribution had never been isolated. It was isolated on 2026-09-18, and it landed near zero, which is where this paragraph previously promised to publish it: removing the graph's ranking term outright costs one query out of 193, with no change to MRR@5 and no statistical separation from chance. That does NOT mean the database can go, and the difference matters: the corpus itself lives in the graph, so the measurement retires a claim about RANKING, not the storage. It also found that the weight had never been tuned, and tuning it was worth about five queries. Method, arms and caveats: [`benchmarks/NEO4J-ABLATION-2026-09-18.md`](benchmarks/NEO4J-ABLATION-2026-09-18.md).

Until those experiments exist, treat the enforcement claim as a designed mechanism with an honestly documented failure posture, not a demonstrated outcome. What **is** independently checkable today lives in the repository rather than in assertions. [`docs/pressure-runs/`](docs/pressure-runs/) contains adversarial test runs against real Claude Code sessions: the exact prompt used, the full transcript, every enforcement decision as raw log lines, and a graded analysis scoring each targeted rule as held or bypassed, including the failures, documented as failures. [`docs/monthly-reviews/`](docs/monthly-reviews/) contains operational reviews built from the system's own audit log.

The rest of the evidence: [`SCALE_BENCHMARK_RESULTS.md`](SCALE_BENCHMARK_RESULTS.md) holds the full dated measurements, the methodology behind each one, the corrections, and the historical runs. [`docs/reference/efficacy-ab.md`](docs/reference/efficacy-ab.md) holds the A/B harness and what it has not shown. [`SECURITY.md`](SECURITY.md) holds the trust model, including the inventory of shell-write gaps that remain.

## Architecture and deeper reading

* [`HANDBOOK.md`](HANDBOOK.md): the operator manual. Modes, gates, helper AIs, the rulebook, the command line, day-to-day use.
* [`docs/instructions-vs-enforcement.md`](docs/instructions-vs-enforcement.md): why an instruction and an enforcement point are different primitives, where Writ sits against other approaches to coding-agent governance, and what that means for Agent Skills and for instruction-driven methodologies.
* [`docs/reference/`](docs/reference/): precise contracts. Architecture, graph schema, retrieval, sessions and gates, configuration, logging, decision memory, testing.
* [`docs/install.md`](docs/install.md): both install paths, running it as a background service, and troubleshooting.
* [`SCALE_BENCHMARK_RESULTS.md`](SCALE_BENCHMARK_RESULTS.md) and [`docs/reference/efficacy-ab.md`](docs/reference/efficacy-ab.md): the measurements, and the harness for the claim that is not measured yet.
* [`CONTRIBUTING.md`](CONTRIBUTING.md): how to author rules, the review cadence, and triaging AI proposals.
* [`CHANGELOG.md`](CHANGELOG.md): release history through v1.7.0.
* [`SECURITY.md`](SECURITY.md): the trust model stated plainly, how to report a vulnerability, and why auditing what you install stays your job.
* [`ERRATA.md`](ERRATA.md): corrections to figures this project has published, including the ones with no consumer that are deliberately kept out of this file.

Two things here are reference material rather than product documentation, and both stand on their own.

* [`docs/reference/claude-code-blackbox.md`](docs/reference/claude-code-blackbox.md): a version-pinned, empirical map of exactly what Claude Code hands a hook script and exactly what a script can hand back, with an evidence tag on every field. It is useful whether or not you use Writ.
* **Architecture, in your browser.** Six self-contained pages with interactive diagrams and a live explorer for the graph itself:

[overview](https://infinri.github.io/Writ/docs/architecture/index.html) |
[data model](https://infinri.github.io/Writ/docs/architecture/data-model.html) |
[retrieval](https://infinri.github.io/Writ/docs/architecture/retrieval-pipeline.html) |
[injection channels](https://infinri.github.io/Writ/docs/architecture/injection-channels.html) |
[graph explorer](https://infinri.github.io/Writ/docs/architecture/knowledge-graph.html) |
[corpus round trip](https://infinri.github.io/Writ/docs/architecture/corpus-roundtrip.html)

## Status

**v1.7.0, released 2026-08-08.** Installs end to end as a Claude Code plugin. The hook system was audited and hardened: two gates that were failing open now hold, session identity is never guessed, destructive database operations need explicit permission, and the test suite's isolation is enforced rather than assumed. Search numbers were re-measured on 08-05 and 08-06 after a nondeterminism defect was found and fixed.

Every number in this file is either measured and dated, or derived from the current source tree. Where this file and the code disagree, the code wins.

## Acknowledgements

**[Superpowers](https://github.com/obra/superpowers), by Jesse Vincent.** Superpowers is a polished methodology, and a useful one. The architectural disagreement, about where the final authority over a mandatory checkpoint sits, is argued in [`docs/instructions-vs-enforcement.md`](docs/instructions-vs-enforcement.md#superpowers-and-the-definition-of-mandatory) rather than here. Superpowers formalized the discipline. Writ was built around the part that formalization still leaves optional.

**[Jolli](https://www.jolli.ai/), by [JolliAI](https://github.com/jolliai/jolliai).** Jolli has done thoughtful work on preserving the reasoning behind AI-assisted development after a coding session ends. That work helped inform parts of Writ's own [decision-provenance system](#continuity), particularly the idea that useful development context should survive the conversation instead of disappearing with it.

Writ's digest eviction policy is adapted from Jolli's ContextCompiler. The policy, not the code, and `writ/session/recall.py` documents the adaptation.

Writ uses that idea inside its own governance model by connecting approved plans, governing rule IDs, changed files, and commits so later sessions can recover the governance context surrounding a change.

If preserving development reasoning is your main problem and you do not need Writ's workflow gates, Jolli is worth a look. It is focused on that problem, and it is good at it.

License: MIT. Authored by Lucio Saldivar.
