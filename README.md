# Writ

[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

**Governance for coding agents.**

Writ is a governance runtime for Claude Code. It moves important engineering controls outside the model, where they can be enforced, retrieved, and remembered independently of what the model happens to keep in context.

**Enforce. Inform. Remember.**

**Enforce.** Selected workflow boundaries run as code at tool time. A Work-mode implementation can be stopped until a human has approved its plan and its tests, and credential writes and other protected actions have their own guards in every mode.

**Inform.** Rules reach the agent when they apply, based on the task, file, tool, and workflow phase in front of it. Seven universal process and gate rules form a small always-on floor. Other mandatory rules are scoped to the actions they protect, while the rest of the rulebook is retrieved by relevance. Rules that do not apply stay out of context.

**Remember.** Approved plans, the rule IDs that governed them, changed files, and commits become connected provenance: recorded in the graph, pushed onto pull requests and git notes, and compiled into a briefing for future sessions. The record answers a governance question: under what approved plan and governing rules did this change occur, and which files and commit resulted?

Most coding-agent systems ask the model to remember the process. Writ puts selected parts of the process around the model instead.

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

Writ governs three things. **Action**: what the agent is permitted to do. **Context**: which engineering rules govern the current action. **Continuity**: why the action was approved and what future sessions should know. Hooks and gates are the Action mechanism, retrieval over the rule graph is the Context mechanism, and decision provenance is the Continuity mechanism.

Already convinced? [Jump to install](#install). Already installed? [`HANDBOOK.md`](HANDBOOK.md) is the operator manual.

## What using it feels like

You tell Writ what kind of work you are doing. That is the mode. In the read-only modes (conversation, review, investigate) Writ hands over relevant rules and otherwise stays quiet. In **Work mode**, writes to your source code are blocked until two gates open:

```text
[ENF-GATE-PLAN] Write blocked. Approve plan.md first.
```

You read the plan. You type "approved." The gate opens. The next gate wants a test file that actually asserts something. Same pattern: write it, approve it, and the gate opens. After both gates clear, the AI writes implementation code freely.

**The AI cannot approve itself.** Opening a gate consumes a one-time secret written to a temporary file, and that secret is only created when *your typed message* matches an approval phrase. Claiming it is a single filesystem operation that exactly one caller can win, so one approval opens exactly one gate. An AI that tries to open its own gate finds no secret, gets refused, and the attempt is written to the audit log as `agent_self_approval_blocked`.

Worth being blunt about what the gate does and does not check. The validators confirm the plan **exists and has the right shape**. They cannot tell a thoughtful plan from a plausible-looking one. No pattern match can. What makes the gate meaningful is that a person reads the artifact before typing the approval. Writ relocates oversight. It does not remove it.

| Mode | For | What it blocks |
|---|---|---|
| `conversation` | Talking, asking, thinking out loud | Nothing |
| `review` | Judging code against the rules | Nothing |
| `investigate` | Auditing, exploring, researching | Web research cannot be summarized until sources come from two independent sites |
| `debug` | Chasing one specific failure | Source edits, until you have written down a root cause |
| `work` | Building or changing code | Source writes, until the plan gate and the test gate both open |

While Claude works, the hooks watch each write. Editing a file whose code touches SQL can pull the parameterized-query and injection rules into context at that moment, even if your prompt never mentioned SQL.

And when the work is committed, Writ can connect the resulting files back to the approved plan and the rules that governed the session.

## What it costs, and who it is for

**What it costs you.** For a typical Work-mode change, Writ adds two approvals: you read the plan and type "approved", then you read the tests and type "approved". After that the AI writes code without interrupting you again, with one exception: a review finding something serious adds a confirmation before the work is committed. So usually two, occasionally three. The other four modes add no approvals at all. The rulebook lives in a database on your own machine, so you need Docker installed, which is a normal application download. Your rules and your code stay on your machine; [`SECURITY.md`](SECURITY.md) lists the one thing that leaves and when.

**What you get for that.** On Writ's guarded path, configured gates are checked at tool time. Either their conditions have been satisfied or the attempted action is refused. When something is refused, there is a record of what was refused and why, which is the part that matters if you are the person answering for the code rather than writing it.

**Who it is for.** Engineers and engineering leads who need important coding-agent workflows to be governed outside the model rather than left as instructions. Writ ships opinionated plan-first and test-driven defaults, and its mode and gate system provides the machinery underneath them.

Writ deliberately trades some setup and workflow friction (Python, Docker, Neo4j, a background service, hooks, workflow state) for stronger control over agent behavior. The question is whether those guarantees are worth that tradeoff for your work. Everything below documents exactly what Writ controls, what it does not, and the evidence available today.

## What Writ does not guarantee

The limits are stated here rather than further down where they would look buried.

**Writ constrains a cooperative agent; it is not an adversarial sandbox.** Writ assumes the AI uses its tools in the ordinary way and is not deliberately searching for ways around the harness. Under that assumption the gates hold. Against an AI actively working around them they do not, and the gaps are written down here rather than glossed:

* Writes made through shell commands are inspected, and as of 1.7.0 that inspection reads inside interpreter one-liners too (`python -c`, `node -e`, `perl -e`, `ruby -e`, `php -r`, including heredoc and piped forms). The gaps that remain are named in the hook itself rather than left vague: a path assembled from shell variables, `eval` or base64, an `sh -c` wrapper, program text handed to awk or sed, an interpreter reached through a variable or alias, and `python -m MODULE`, which is deliberately unscanned because matching it would refuse every `python -m pytest` run.
* When the background service is unreachable, hooks **allow rather than block**. This is the specification, not a bug. An infrastructure outage must never lock you out of your own repository.
* Subagents (helper AIs spawned by the main one) skip the write gates by design. Their limits come from the tools their role grants them, not from re-checking work the human already approved.

If you need enforcement against an AI that is actively adversarial, Writ is not that tool. What Writ can do is mechanically refuse selected tool actions until configured workflow conditions are satisfied. Whether that produces better engineering outcomes is a different question, and the two limits below are why it is still open.

It can tell that a plan exists. It cannot tell whether the plan is any good. The checks confirm the shape of the thing, not the thought behind it. A plausible plan and a careful one look identical to a machine, so this replaces none of your judgement, and reviewing the work is still your job.

The main claim is not proven yet. Everything measured so far shows what the search costs and how well it ranks. None of it shows that an AI handed the right rule actually behaves better than one handed nothing. That is the whole point of the tool and it is currently unproven, with the reasoning and the missing experiment written up further down.

---

**Everything below this point assumes you write code.** The rest of this document is written for engineers evaluating whether to run it, and stops rationing vocabulary.

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

## Enforcement

You have probably watched this happen. You tell the AI how you want things done: write the test first, follow the pattern already in the file, ask before touching the database. It agrees. It works that way for a while. Then somewhere in a long session it stops, and nothing announces that it stopped. You find out in review, or you find out in production, or you do not find out.

That is not the AI being careless. An instruction in context is still an instruction: it can be compacted away, diluted by newer context, misapplied, or simply ignored, and nothing about putting a rule in the prompt makes violating it mechanically impossible. Instructions and enforcement are different primitives, and only one of them can refuse.

Writ supplies the second primitive for the parts of the process you choose to gate. It sits between the AI and your files. In Work mode, a write attempted before you have approved a plan is refused. Not discouraged, refused, by code that runs whether or not the AI is still paying attention to what you said an hour ago.

**Enforcement solves only half the problem.** A large rulebook cannot simply be pasted into every turn, so Writ also moves rule selection outside the model. It looks at the work happening now and delivers only the rules that apply. Tool-time checks do not depend on the model remembering the process, and contextual delivery lets the rulebook grow without the cost of every turn growing with it.

Two boundaries hold no matter what, including when the background service is down and inside subagents. Writes to credential files (keys, `.env`, SSH material) are refused in every mode with no server involved. And the approval token cannot be created or spent without a human keystroke, so **advancing the workflow and writing new rules into the rulebook halt even when raw file writes do not.**

**What a review finding does.** A recorded CRITICAL verdict turns the next `git commit` into a confirmation prompt naming the unresolved findings. It is a stop and ask, not an absolute block: you can confirm and commit anyway, and that choice is recorded in the audit log. The part that carries the weight is that the AI cannot clear its own verdict. Writing a review record directly is refused outright; verdicts are written only from the reviewer's own output, and the only route an AI has to lifting a block is to fix the findings and earn a fresh clean verdict. An unreadable verdict blocks the same way a critical one does. So the AI can be overruled by you, and cannot overrule you.

## How the rules reach the AI

**The floor: rules that can never be dropped.** Thirty-two of the 288 shipped rules are marked mandatory. These are deliberately kept **out of the search index entirely** and delivered through a separate channel with its own budget. Seven of them carry universal scope and inject on every turn; the other 25 are scoped to writes and keyword-gated, so they arrive the moment a write matches them rather than every turn. That means no change to search ranking, no swap of the underlying model, and no retuning of anything can cause a mandatory rule to fall out of delivery because of ranking. A single definition in one file decides what belongs to the floor, and both the delivery code and the validation code read that same definition, so the two cannot drift apart. This closed a real bug where two parts of the system checked different fields and left 29 of 32 mandatory rules unreachable by either path.

**Everything else is searched for.** Writ currently uses a five-stage retrieval pipeline over a Neo4j knowledge graph: narrow the candidates, keyword search, meaning-based search (so a rule about "SQL" surfaces for a question about "database queries"), a walk across the graph to pull in related rules, then weighted ranking. Those five stages are the ones listed at the top of [`writ/retrieval/pipeline.py`](writ/retrieval/pipeline.py), and each is designed to cover a different retrieval failure mode: keyword search catches exact terms, meaning-based search catches paraphrase, and the graph walk reaches rules that share no words with the query at all but are linked to a match. Keyword and meaning-based retrieval are measured as part of the full system; the incremental contribution of the graph traversal stage has not yet been isolated, and it is listed under Not measured below. If nothing matches well enough, the pipeline **returns nothing** rather than injecting noise.

**The search fires on what is happening, not just what you typed.** Writ's hooks observe the session across the twelve Claude Code events they register for: prompts, file reads, writes, shell commands, subagent start and stop, compaction, and session lifecycle. They attach real context to the query: which file is being written, what is inside it, which tool is running, and what phase the workflow is in. A rule about SQL injection surfaces when the AI writes a file containing a query, not only when someone happens to type the word SQL.

## Why not just use Agent Skills?

For a small number of behaviors, you probably should. [Anthropic's Agent Skills](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills) are simple, portable, and useful. If all you need is a handful of reusable instructions, Writ is unnecessary.

But Skills and governance are not the same mechanism, and the distinction is not something Anthropic's own engineering material leaves obscure.

Anthropic documents that every installed skill contributes metadata to the system prompt, then **Claude decides whether the skill is relevant** before loading its full `SKILL.md` into context. Anthropic also documents the cost of that design in its own [context-engineering guidance](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents): context is finite, recall degrades as context grows, every added token consumes part of the model's attention budget, and good context engineering means finding the smallest high-signal set of tokens that produces the desired behavior.

Anthropic documents the other half of the distinction too. In the Agent Skills article, it notes that some operations need the deterministic reliability of code rather than model generation. Claude Code's own [hooks documentation](https://code.claude.com/docs/en/hooks-guide) exposes `PreToolUse`, which runs before a tool executes and can deny the action outright. A denying hook still blocks the action even when Claude Code is running in a permission-bypass mode.

So the primitives already exist, and the tradeoff is already understood.

A Skill can say:

> Write the test first.

A tool-time gate can say:

> No. This write does not run until the test gate is open.

Those are different guarantees.

### "Mandatory" is not a property an instruction can give itself

A skill can contain `MUST`, `REQUIRED`, `NEVER`, or `MANDATORY` as many times as its author wants. The model still has to discover the skill, load it, retain the relevant instruction, interpret it correctly, and choose the expected action.

That is an instruction with forceful wording. It is not external enforcement.

[Superpowers](https://github.com/obra/superpowers) makes the distinction unusually easy to see. It describes its workflows as mandatory, yet its own [porting guide](https://github.com/obra/superpowers/blob/main/docs/porting-to-a-new-harness.md) says the full bootstrap is injected into model context at the start of every session, calls that bootstrap "the entire integration," and states that without it the skill files are inert. The same guide treats automatic session-start injection as a non-negotiable requirement for a supported harness.

That is not a criticism of the quality of its methodology. It is the architectural boundary of an instruction-driven methodology.

A workflow does not become mandatory because the instructions describing it say that it is mandatory.

Writ draws the boundary somewhere else. If a checkpoint is important enough to call mandatory, Writ's position is that the model should not be the final authority over whether it happened. Selected checkpoints run outside the model and can refuse the action.

### The token incentive is worth saying out loud

Anthropic's own engineering guidance says context is a finite resource, additional tokens consume attention, and unnecessary context should be reduced. Its Skills design uses progressive disclosure specifically to avoid loading everything at once.

Anthropic's [API pricing](https://platform.claude.com/docs/en/about-claude/pricing) also bills input tokens.

That does **not** prove why Anthropic chose the product boundary it chose, and Writ makes no claim about anyone's private motive. It does create an incentive tension that users are allowed to notice: users benefit when governance requires less model context, while a token-priced API vendor earns revenue from inference usage.

Maybe the boundary exists because Skills prioritize simplicity and portability. Maybe it reflects product philosophy. Maybe economics are part of the picture. The public evidence cannot tell us which.

What the public evidence *can* tell us is harder to dismiss: Anthropic knows context is costly, knows model-side instruction following is not the same thing as deterministic code, and already ships a tool-time mechanism capable of hard denial. Writ therefore does not treat the instruction-versus-enforcement distinction as an obscure limitation nobody could have seen.

Readers can decide for themselves why the product boundary remains where it is.

### The trigger problem is larger than wording

Skills are discovered from context and selected by the model. Writ can also react to what the agent is actually doing.

A rule that should fire because Claude is editing a controller containing a raw SQL query does not need the user to have typed "SQL." Writ can observe the write, inspect the file and tool context, and deliver the relevant rule at that moment.

That moves rule selection away from:

> Does the model realize this skill is relevant?

toward:

> What action is actually happening right now?

For small skill counts, discrete behaviors, model-side selection, and zero infrastructure, Skills are the simpler answer.

For large rulebooks, action-sensitive rules, human approval boundaries, and workflows whose mandatory steps must be capable of refusing an action, Writ is solving a different problem.

## Decision provenance: why each file changed

This is not conversational memory. Writ does not read your chat history and guess what mattered. It builds the record mechanically, from things that already exist.

When you commit, a git hook joins the commit's files against the **approved plan**, the rules each session and helper AI actually looked up for each file, and any earlier open decisions. It writes three kinds of record into the same graph as the rulebook, connected by typed relationships, with identifiers derived from content so that amending a commit updates the record instead of duplicating it. The hook never blocks a commit and does nothing harmful when the service is down. A backfill command reconstructs the history for commits made before you installed it.

Each file-change record carries the reason for the change, the rules the AI was shown, and the rules it cited. That last grounding is the distinguishing property: every decision stays tied to the rule identifiers that governed it, and those survive every round of trimming when the record gets too large.

The record plays back in three places:

* **A session briefing.** Recent decisions get compiled into a size-limited digest and the top of it is injected into your first message of a new session, so the AI starts knowing what was decided and why. Under pressure the reasoning trims first, then the per-file notes, then the oldest decisions. Identifiers, titles, and governing rules are never trimmed.
* **Pull request comments.** One comment per changed file: why it changed, which rules the AI was shown, and which it cited. It updates its own comments rather than piling up duplicates. Reviewers read the reasoning next to the diff instead of reconstructing it.
* **Git notes.** The same content is written into git itself, which needs no server and travels with the repository anywhere.

**Be clear on what this is.** It is an attribution trail: what the AI was shown and what it claimed to apply. It is not proof that a rule was followed. That is still a reviewer's job, which is exactly why the pull request channel exists. And it is only possible because Writ owns the approval gate. A memory layer bolted onto an AI has no approved plan to join against.

Pull request comments currently support Bitbucket Cloud only, and self-hosted Bitbucket Server is explicitly rejected rather than silently broken. The briefing and git notes channels work anywhere. Full detail in [`docs/reference/decision-memory.md`](docs/reference/decision-memory.md).

## Measured

**Evidence today.** Every figure below is a dated measurement, not a live readout, taken on one developer machine with an uncapped database container, so your numbers will differ.

* **Search quality.** 0.923 hit rate at 5 across the 169 index-eligible questions of the gold set, and 0.608 mean reciprocal rank at 5 across the 47 deliberately ambiguous ones (2026-08-06).
* **Search cost.** A warm 95th percentile of 0.827 ms in the published synthetic run against 10,000 rules (2026-08-01).
* **Rule text per turn stays roughly flat as the rulebook grows.** About 2,000 tokens against the live 287-rule corpus (2026-08-05), about 1,590 against the 10,000-rule synthetic one (2026-08-01).
* **The floors are gates, not aspirations.** Seventeen benchmark targets run in continuous integration on every push and every pull request, and they passed 17 of 17 on 2026-08-14.

Full dated measurements, the methodology behind each one, the corrections, and the historical runs live in [`SCALE_BENCHMARK_RESULTS.md`](SCALE_BENCHMARK_RESULTS.md).

## Not measured

Everything above measures what the search **costs** and how well it ranks. None of it measures whether an AI given the right rule **actually complies** more often than one given nothing. That is Writ's central claim and it is currently unproven.

The harness to test it exists. It runs matched Claude Code sessions with Writ on and Writ off against a deliberately planted security defect, scoring whether the defect was caught and at what cost. It has not been run at a scale that proves anything. At one repetition the result is reported as insufficient by design, because a single run cannot beat the randomness in how AI sessions unfold. What is still needed: many repetitions with a noise floor, a defect suite broader than the single planted case, and a cheaper scoring judge.

A second thing is unproven, and smaller only by comparison. The search runs five stages, one of which walks the rule graph, and **that stage is the reason this project needs a graph database at all**. Its individual contribution has never been isolated. Nobody has run the test set with graph traversal disabled and compared the ranking quality, so the honest position is that the dependency is justified by design reasoning rather than by a measurement. The nondeterminism finding makes this more pressing rather than less: if iteration order was quietly deciding thirty questions' results, per-stage attribution was even shakier than it looked. The number will be published wherever it lands, including at or near zero.

Until those experiments exist, treat the enforcement claim as a designed mechanism with an honestly documented failure posture, not a demonstrated outcome.

What **is** independently checkable today lives in the repository rather than in assertions. [`docs/pressure-runs/`](docs/pressure-runs/) contains adversarial test runs against real Claude Code sessions: the exact prompt used, the full transcript, every enforcement decision as raw log lines, and a graded analysis scoring each targeted rule as held or bypassed, including the failures, documented as failures. [`docs/monthly-reviews/`](docs/monthly-reviews/) contains operational reviews built from the system's own audit log.

## The rulebook is opinionated

288 rules ship in the box: 76 security, 45 code quality, 28 architecture, 21 testing, 19 performance, 19 process, and smaller sets besides. The shape reflects where its author has worked. There are 12 Magento 2 rules and exactly one PHP typing rule, which tells you something true about where it came from.

Treat the shipped rulebook as a working example, not a universal standard. Commands for adding and editing rules exist so you can grow your own, and there is a full lifecycle for rules the AI itself proposes: a proposed rule lands marked provisional, gets promoted to a review queue only after enough real-world evidence accumulates, and enters the canonical rulebook only through a human approval that requires the same one-time secret as everything else. The statistics never promote anything on their own.

## Where Writ sits against other approaches

These are approaches to coding-agent governance, not products. Each is a reasonable way to give an agent rules, and each runs into a structural limit that shaped Writ's design.

* **Rules stuffed into the context.** Cost grows with the rulebook and the signal gets buried in it. Writ retrieves instead, so the per-turn cost stays roughly flat as the rulebook grows.
* **Static skill files.** Point-in-time bundles with no relationships between them. Writ keeps rules in a knowledge graph with typed links, so a matched rule can pull in its neighbors, including ones that share no words with what you asked.
* **Per-repo rules as code.** Nothing propagates between repositories, and each copy drifts on its own. Writ keeps one shared graph with per-project isolation.
* **An AI validator on every diff.** A model call per change, and the same code can be judged differently twice. Writ's gates are code, so an ordinary turn costs no model call at all.
* **Rules in the system prompt.** Editing the rulebook changes the prefix every request shares. Writ injects per turn instead, and keeps rule ordering stable so the shared prefix does not churn.

## Research and reference artifacts

Two things in this repository are reference material rather than product documentation, and both stand on their own.

### The Claude Code hook black box

[`docs/reference/claude-code-blackbox.md`](docs/reference/claude-code-blackbox.md) is a version-pinned, empirical map of exactly what Claude Code hands a hook script and exactly what a script can hand back. Captured live on build 2.1.220 and compared against 2.1.183. Every single field carries an evidence tag: observed in real data, documented but not seen, or unverified. The build pin covers the original capture, and the file has kept growing since: it also carries findings observed on 2026-08-11 and 2026-08-14, each stamped with its own date. Read the tag next to a claim rather than the version at the top.

It records five events that moved from documented only to actually observed, payload fields the public changelog never announced, and the mechanism that lets a script rewrite a tool call before it runs without the AI ever seeing the change. It is written so a non-engineer can follow the idea in Part 1 and an engineer can build against the detail in Part 2.

It is useful whether or not you use Writ. It is the reference this project wishes had existed.

### Architecture, in your browser

Six self-contained pages with interactive diagrams and a live explorer for the graph itself:

[overview](https://infinri.github.io/Writ/docs/architecture/index.html) |
[data model](https://infinri.github.io/Writ/docs/architecture/data-model.html) |
[retrieval](https://infinri.github.io/Writ/docs/architecture/retrieval-pipeline.html) |
[injection channels](https://infinri.github.io/Writ/docs/architecture/injection-channels.html) |
[graph explorer](https://infinri.github.io/Writ/docs/architecture/knowledge-graph.html) |
[corpus round trip](https://infinri.github.io/Writ/docs/architecture/corpus-roundtrip.html)

## Where to go next

* [`HANDBOOK.md`](HANDBOOK.md): the operator manual. Modes, gates, helper AIs, the rulebook, the command line, day-to-day use.
* [`docs/reference/`](docs/reference/): precise contracts. Architecture, graph schema, retrieval, sessions and gates, configuration, logging, decision memory, testing.
* [`docs/install.md`](docs/install.md): both install paths, running it as a background service, and troubleshooting.
* [`CONTRIBUTING.md`](CONTRIBUTING.md): how to author rules, the review cadence, and triaging AI proposals.
* [`CHANGELOG.md`](CHANGELOG.md): release history through v1.7.0.
* [`SECURITY.md`](SECURITY.md): the trust model stated plainly, how to report a vulnerability, and why auditing what you install stays your job.
* [`ERRATA.md`](ERRATA.md): corrections to figures this project has published, including the ones with no consumer that are deliberately kept out of this file.

## Status

**v1.7.0, released 2026-08-08.** Installs end to end as a Claude Code plugin. The hook system was audited and hardened: two gates that were failing open now hold, session identity is never guessed, destructive database operations need explicit permission, and the test suite's isolation is enforced rather than assumed. Search numbers were re-measured on 08-05 and 08-06 after a nondeterminism defect was found and fixed.

Every number in this file is either measured and dated, or derived from the current source tree. Where this file and the code disagree, the code wins.

## Acknowledgements

**[Superpowers](https://github.com/obra/superpowers), by Jesse Vincent.** Superpowers is a polished methodology built on the exact architectural contradiction discussed in [Why not just use Agent Skills?](#why-not-just-use-agent-skills): it calls workflows mandatory while leaving the final enforcement authority inside the model.

Its own [porting guide](https://github.com/obra/superpowers/blob/main/docs/porting-to-a-new-harness.md) makes the dependency explicit. The model must receive a bootstrap, discover the relevant skill, load its instructions, retain them through the session, and follow them. The guide calls that bootstrap the entire integration, says the skills are inert without it, and treats automatic session-start injection as a hard requirement.

That is a useful methodology. It is not the definition of mandatory Writ accepts.

If planning, TDD, review, and verification matter enough to be mandatory, selected checkpoints should be able to refuse the action without asking the model whether it remembers the rule. Superpowers formalized the discipline. Writ was built around the part that formalization still leaves optional.

**[Jolli](https://www.jolli.ai/), by [JolliAI](https://github.com/jolliai/jolliai).** Jolli has done thoughtful work on preserving the reasoning behind AI-assisted development after a coding session ends. That work helped inform parts of Writ's own [decision-provenance system](#decision-provenance-why-each-file-changed), particularly the idea that useful development context should survive the conversation instead of disappearing with it.

Writ's digest eviction policy is adapted from Jolli's ContextCompiler. The policy, not the code, and `writ/session/recall.py` documents the adaptation.

Writ uses that idea inside its own governance model by connecting approved plans, governing rule IDs, changed files, and commits so later sessions can recover the governance context surrounding a change.

If preserving development reasoning is your main problem and you do not need Writ's workflow gates, Jolli is worth a look. It is focused on that problem, and it is good at it.

License: MIT. Authored by Lucio Saldivar.