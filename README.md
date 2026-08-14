# Writ

[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

**Governance for coding agents.**

Writ is a governance runtime for Claude Code. It moves important engineering controls outside the model, where they can be enforced, retrieved, and remembered independently of what the model happens to keep in context.

**Enforce. Inform. Remember.**

**Enforce.** Selected workflow boundaries run as code at tool time. A Work-mode implementation can be stopped until a human has approved its plan and its tests, and credential writes and other protected actions have their own guards in every mode.

**Inform.** Rules reach the agent by relevance: the ones matching the task, file, tool, and workflow phase in front of it, on top of a small always on floor of seven process and gate rules that injects every turn. Everything else, the mandatory security rules included, stays out of context until the work matches it.

**Remember.** Approved plans, the rule ids that governed them, changed files, and commits become connected provenance: recorded in the graph, pushed onto pull requests and git notes, and compiled into a briefing for future sessions. The record answers a governance question: under what approved plan and governing rules did this change occur, and which files and commit resulted.

Most coding-agent systems ask the model to remember the process. Writ puts selected parts of the process around the model instead.

```
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

You tell Writ what kind of work you are doing. That is the mode. In the read only modes (conversation, review, investigate) Writ hands over relevant rules and otherwise stays quiet. In **Work mode**, writes to your source code are blocked until two gates open:

```
[ENF-GATE-PLAN] Write blocked. Approve plan.md first.
```

You read the plan. You type "approved." The gate opens. The next gate wants a test file that actually asserts something. Same pattern: write it, approve, gate opens. After both gates clear, the AI writes implementation code freely.

**The AI cannot approve itself.** Opening a gate consumes a one time secret written to a temporary file, and that secret is only created when *your typed message* matches an approval phrase. Claiming it is a single filesystem operation that exactly one caller can win, so one approval opens exactly one gate. An AI that tries to open its own gate finds no secret, gets refused, and the attempt is written to the audit log as `agent_self_approval_blocked`.

Worth being blunt about what the gate does and does not check. The validators confirm the plan **exists and has the right shape**. They cannot tell a thoughtful plan from a plausible looking one. No pattern match can. What makes the gate meaningful is that a person reads the artifact before typing the approval. Writ relocates oversight. It does not remove it.

| Mode | For | What it blocks |
|---|---|---|
| `conversation` | Talking, asking, thinking out loud | Nothing |
| `review` | Judging code against the rules | Nothing |
| `investigate` | Auditing, exploring, researching | Web research cannot be summarized until sources come from two independent sites |
| `debug` | Chasing one specific failure | Source edits, until you have written down a root cause |
| `work` | Building or changing code | Source writes, until the plan gate and the test gate both open |

While Claude works, the hooks watch each write: editing a file whose code touches SQL can pull the parameterized query and injection rules into context at that moment, even if your prompt never mentioned SQL.

And when the work is committed, Writ can connect the resulting files back to the approved plan and the rules that governed the session.

## What it costs, and who it is for

**What it costs you.** For a typical Work-mode change, Writ adds two approvals: you read the plan and type "approved", then you read the tests and type "approved". After that the AI writes code without interrupting you again, with one exception, which is that a review finding something serious adds a confirmation before the work is committed. So usually two, occasionally three. The other four modes add no approvals at all. The rulebook lives in a database on your own machine, so you need Docker installed, which is a normal application download. Your rules and your code stay on your machine; [`SECURITY.md`](SECURITY.md) lists the one thing that leaves and when.

**What you get for that.** The gated steps either happen or the tool call is refused, rather than happening for the first hour and then quietly not. And when something is refused, there is a record of what was refused and why, which is the part that matters if you are the person answering for the code rather than writing it.

**Who it is for.** Engineers and engineering leads who need important coding-agent workflows to be governed outside the model rather than left as instructions. Writ ships opinionated plan-first and test-driven defaults, and its mode and gate system provides the machinery underneath them.

Writ deliberately trades some setup and workflow friction (Python, Docker, Neo4j, a background service, hooks, workflow state) for stronger control over agent behavior. The question is whether those guarantees are worth that tradeoff for your work. Everything below documents exactly what Writ controls, what it does not, and the evidence available today.

## What Writ does not guarantee

The limits, stated here rather than further down where they would look buried.

**It stops an AI that is going along with the process, not one working around it.** This is the threat model: Writ assumes a **cooperative AI**, one that uses its tools in the ordinary way and is not trying to defeat the harness. Under that assumption the gates hold. Against an AI actively working around them they do not, and the gaps are written down here rather than glossed:

* Writes made through shell commands are inspected, and as of 1.7.0 that inspection reads inside interpreter one liners too (`python -c`, `node -e`, `perl -e`, `ruby -e`, `php -r`, including heredoc and piped forms). The gaps that remain are named in the hook itself rather than left vague: a path assembled from shell variables, `eval` or base64, an `sh -c` wrapper, program text handed to awk or sed, an interpreter reached through a variable or alias, and `python -m MODULE`, which is deliberately unscanned because matching it would refuse every `python -m pytest` run.
* When the background service is unreachable, hooks **allow rather than block**. This is the specification, not a bug. An infrastructure outage must never lock you out of your own repository.
* Sub agents (helper AIs spawned by the main one) skip the write gates by design. Their limits come from the tools their role grants them, not from re-checking work the human already approved.

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

Refusing writes is only affordable if the AI can be handed the right rules cheaply, so Writ keeps the rulebook in a search system instead of the conversation, and looks at what the AI is doing right now to decide what to hand it. A check that runs at the moment the AI calls a tool does not decay, and the rulebook can grow without the cost of every turn growing with it.

Two boundaries hold no matter what, including when the background service is down and inside sub agents. Writes to credential files (keys, `.env`, SSH material) are refused in every mode with no server involved. And the approval token cannot be created or spent without a human keystroke, so **advancing the workflow and writing new rules into the rulebook halt even when raw file writes do not.**

**What a review finding does.** A recorded CRITICAL verdict turns the next `git commit` into a confirmation prompt naming the unresolved findings. It is a stop and ask, not an absolute block: you can confirm and commit anyway, and that choice is recorded in the audit log. The part that carries the weight is that the AI cannot clear its own verdict. Writing a review record directly is refused outright; verdicts are written only from the reviewer's own output, and the only route an AI has to lifting a block is to fix the findings and earn a fresh clean verdict. An unreadable verdict blocks the same way a critical one does. So the AI can be overruled by you, and cannot overrule you.

## How the rules reach the AI

**The floor: rules that can never be dropped.** Thirty two of the 288 shipped rules are marked mandatory. These are deliberately kept **out of the search index entirely** and delivered through a separate channel with its own budget. Seven of them carry universal scope and inject on every turn; the other 25 are scoped to writes and keyword gated, so they arrive the moment a write matches them rather than every turn. That means no change to search ranking, no swap of the underlying model, no retuning of anything can cause a critical security rule to fall off the list. A single definition in one file decides what belongs to the floor, and both the delivery code and the validation code read that same definition, so the two can never drift apart. This closed a real bug where two parts of the system checked different fields and left 29 of 32 mandatory rules unreachable by either path.

**Everything else is searched for.** Writ currently uses a five stage retrieval pipeline over a Neo4j knowledge graph: narrow the candidates, keyword search, meaning based search (so a rule about "SQL" surfaces for a question about "database queries"), a walk across the graph to pull in related rules, then weighted ranking. Those five stages are the ones listed at the top of [`writ/retrieval/pipeline.py`](writ/retrieval/pipeline.py), and each is designed to cover a different retrieval failure mode: keyword search catches exact terms, meaning based search catches paraphrase, and the graph walk reaches rules that share no words with the query at all but are linked to a match. Keyword and meaning based retrieval are measured as part of the full system; the incremental contribution of the graph traversal stage has not yet been isolated, and it is listed under Not measured below. If nothing matches well enough, the pipeline **returns nothing** rather than injecting noise.

**The search fires on what is happening, not just what you typed.** Writ's hooks observe the session across the twelve Claude Code events they register for: prompts, file reads, writes, shell commands, sub agent start and stop, compaction, and session lifecycle. They attach real context to the query: which file is being written, what is inside it, which tool is running, what phase the workflow is in. A rule about SQL injection surfaces when the AI writes a file containing a query, not only when someone happens to type the word SQL.

## "Couldn't you just use skill files?"

For a dozen behaviors, yes, and you should. Anthropic's Agent Skills format solves its problem well. A folder with a markdown file, its name and description pre loaded so the AI knows when the contents apply. Easy to write, no infrastructure, nothing to run. If that covers your case, Writ is overkill and you should not install it.

Two things a markdown file structurally cannot do.

**It cannot refuse a write.** This is a category difference, not a matter of degree. A skill file can describe test driven development in loving detail. Nothing in the format interrupts the moment the AI calls its write tool. The AI decides whether the skill applies, and an AI that decides wrong fails silently, which is the worst kind of failure because you never find out. Writ's gates are refusals made by code, in a process the AI does not control, and every refusal is a logged event rather than an absence you would have to notice.

**The trigger has to be in your message.** Skills work by matching their pre loaded descriptions against what you typed. A rule that must fire when the AI writes a controller containing a raw SQL string cannot work that way, because the thing that should trigger it is the file content at write time, which nobody typed.

The search argument matters too, but treat it as secondary because it is weaker. Pre loaded descriptions blur together as their count grows, overlapping descriptions cause the AI to pick one and silently skip the other, and a request touching several areas gives it multiple plausible matches with nothing to break the tie. At 288 rules, designed to scale into the thousands, the matching decision has to move out of the AI. None of this is a flaw in the Agent Skills spec. It is the boundary of what description matching can do.

**Where the line falls.** Small skill counts, discrete hand written behaviors, AI side matching acceptable, zero setup a priority: use Agent Skills. Large rulebooks that must be enforced, matching that has to leave the AI, triggers that fire on file content and tool calls, and process that must be *refused* rather than requested: that is Writ. Same problem space, different tradeoffs.

## Decision provenance: why each file changed

This is not conversational memory. Writ does not read your chat history and guess what mattered. It builds the record mechanically, from things that already exist.

When you commit, a git hook joins the commit's files against the **approved plan**, the rules each session and helper AI actually looked up for each file, and any earlier open decisions. It writes three kinds of record into the same graph as the rulebook, connected by typed relationships, with identifiers derived from content so that amending a commit updates the record instead of duplicating it. The hook never blocks a commit and does nothing harmful when the service is down. A backfill command reconstructs the history for commits made before you installed it.

Each file change record carries the reason for the change, the rules the AI was shown, and the rules it cited. That last grounding is the distinguishing property: every decision stays tied to the rule identifiers that governed it, and those survive every round of trimming when the record gets too large.

The record plays back in three places:

* **A session briefing.** Recent decisions get compiled into a size limited digest and the top of it is injected into your first message of a new session, so the AI starts knowing what was decided and why. Under pressure the reasoning trims first, then the per file notes, then the oldest decisions. Identifiers, titles, and governing rules are never trimmed.
* **Pull request comments.** One comment per changed file: why it changed, which rules the AI was shown, and which it cited. It updates its own comments rather than piling up duplicates. Reviewers read the reasoning next to the diff instead of reconstructing it.
* **Git notes.** The same content written into git itself, which needs no server and travels with the repository anywhere.

**Be clear on what this is.** It is an attribution trail: what the AI was shown and what it claimed to apply. It is not proof that a rule was followed. That is still a reviewer's job, which is exactly why the pull request channel exists. And it is only possible because Writ owns the approval gate. A memory layer bolted onto an AI has no approved plan to join against.

Pull request comments currently support Bitbucket Cloud only, and self hosted Bitbucket Server is explicitly rejected rather than silently broken. The briefing and git notes channels work anywhere. Full detail in [`docs/reference/decision-memory.md`](docs/reference/decision-memory.md).

## Measured

**Evidence today.** Every figure below is a dated measurement, not a live readout, taken on one developer machine with an uncapped database container, so your numbers will differ.

* **Search quality.** 0.923 hit rate at 5 across the 169 index eligible questions of the gold set, and 0.608 mean reciprocal rank at 5 across the 47 deliberately ambiguous ones (2026-08-06).
* **Search cost.** A warm 95th percentile of 0.827 ms in the published synthetic run against 10,000 rules (2026-08-01).
* **Rule text per turn stays roughly flat as the rulebook grows.** About 2,000 tokens against the live 287 rule corpus (2026-08-05), about 1,590 against the 10,000 rule synthetic one (2026-08-01).
* **The floors are gates, not aspirations.** 17 benchmark targets run in continuous integration on every push and every pull request, and they passed 17 of 17 on 2026-08-14.

Full dated measurements, the methodology behind each one, the corrections, and the historical runs live in [`SCALE_BENCHMARK_RESULTS.md`](SCALE_BENCHMARK_RESULTS.md).

## Not measured

Everything above measures what the search **costs** and how well it ranks. None of it measures whether an AI given the right rule **actually complies** more often than one given nothing. That is Writ's central claim and it is currently unproven.

The harness to test it exists. It runs matched Claude Code sessions with Writ on and Writ off against a deliberately planted security defect, scoring whether the defect was caught and at what cost. It has not been run at a scale that proves anything. At one repetition the result is reported as insufficient by design, because a single run cannot beat the randomness in how AI sessions unfold. What is still needed: many repetitions with a noise floor, a defect suite broader than the single planted case, and a cheaper scoring judge.

A second thing is unproven, and smaller only by comparison. The search runs five stages, one of which walks the rule graph, and **that stage is the reason this project needs a graph database at all**. Its individual contribution has never been isolated. Nobody has run the test set with graph traversal disabled and compared the ranking quality, so the honest position is that the dependency is justified by design reasoning rather than by a measurement. The nondeterminism finding makes this more pressing rather than less: if iteration order was quietly deciding thirty questions' results, per stage attribution was even shakier than it looked. The number will be published wherever it lands, including at or near zero.

Until those exist, treat the enforcement claim as a designed mechanism with an honestly documented failure posture, not a demonstrated outcome.

What **is** independently checkable today lives in the repository rather than in assertions. [`docs/pressure-runs/`](docs/pressure-runs/) contains adversarial test runs against real Claude Code sessions: the exact prompt used, the full transcript, every enforcement decision as raw log lines, and a graded analysis scoring each targeted rule as held or bypassed, including the failures, documented as failures. [`docs/monthly-reviews/`](docs/monthly-reviews/) contains operational reviews built from the system's own audit log.

## The rulebook is opinionated

288 rules ship in the box: 76 security, 45 code quality, 28 architecture, 21 testing, 19 performance, 19 process, and smaller sets besides. The shape reflects where its author has worked. There are 12 Magento 2 rules and exactly one PHP typing rule, which tells you something true about where it came from.

Treat the shipped rulebook as a working example, not a universal standard. Commands for adding and editing rules exist so you grow your own, and there is a full lifecycle for rules the AI itself proposes: a proposed rule lands marked provisional, gets promoted to a review queue only after enough real world evidence accumulates, and enters the canonical rulebook only through a human approval that requires the same one time secret as everything else. The statistics never promote anything on their own.

## Where Writ sits against other approaches

These are approaches to coding agent governance, not products. Each is a reasonable way to give an agent rules, and each runs into a structural limit that shaped Writ's design.

* **Rules stuffed into the context.** Cost grows with the rulebook and the signal gets buried in it. Writ retrieves instead, so the per turn cost stays roughly flat as the rulebook grows.
* **Static skill files.** Point in time bundles with no relationships between them. Writ keeps rules in a knowledge graph with typed links, so a matched rule can pull in its neighbors, including ones that share no words with what you asked.
* **Per repo rules as code.** Nothing propagates between repositories, and each copy drifts on its own. Writ keeps one shared graph with per project isolation.
* **An AI validator on every diff.** A model call per change, and the same code can be judged differently twice. Writ's gates are code, so an ordinary turn costs no model call at all.
* **Rules in the system prompt.** Editing the rulebook changes the prefix every request shares. Writ injects per turn instead, and keeps rule ordering stable so the shared prefix does not churn.

## Research and reference artifacts

Two things in this repository are reference material rather than product documentation, and both stand on their own.

### The Claude Code hook black box

[`docs/reference/claude-code-blackbox.md`](docs/reference/claude-code-blackbox.md) is a version pinned, empirical map of exactly what Claude Code hands a hook script and exactly what a script can hand back. Captured live on build 2.1.220 and compared against 2.1.183. Every single field carries an evidence tag: observed in real data, documented but not seen, or unverified. The build pin covers the original capture, and the file has kept growing since: it also carries findings observed on 2026-08-11 and 2026-08-14, each stamped with its own date. Read the tag next to a claim rather than the version at the top.

It records five events that moved from documented only to actually observed, payload fields the public changelog never announced, and the mechanism that lets a script rewrite a tool call before it runs without the AI ever seeing the change. It is written so a non engineer can follow the idea in Part 1 and an engineer can build against the detail in Part 2.

It is useful whether or not you use Writ. It is the reference this project wishes had existed.

### Architecture, in your browser

Six self contained pages with interactive diagrams and a live explorer for the graph itself:
[overview](https://infinri.github.io/Writ/docs/architecture/index.html) |
[data model](https://infinri.github.io/Writ/docs/architecture/data-model.html) |
[retrieval](https://infinri.github.io/Writ/docs/architecture/retrieval-pipeline.html) |
[injection channels](https://infinri.github.io/Writ/docs/architecture/injection-channels.html) |
[graph explorer](https://infinri.github.io/Writ/docs/architecture/knowledge-graph.html) |
[corpus round trip](https://infinri.github.io/Writ/docs/architecture/corpus-roundtrip.html)

## Where to go next

* [`HANDBOOK.md`](HANDBOOK.md): the operator manual. Modes, gates, helper AIs, the rulebook, the command line, day to day use.
* [`docs/reference/`](docs/reference/): precise contracts. Architecture, graph schema, retrieval, sessions and gates, configuration, logging, decision memory, testing.
* [`docs/install.md`](docs/install.md): both install paths, running it as a background service, and troubleshooting.
* [`CONTRIBUTING.md`](CONTRIBUTING.md): how to author rules, the review cadence, and triaging AI proposals.
* [`CHANGELOG.md`](CHANGELOG.md): release history through v1.7.0.
* [`SECURITY.md`](SECURITY.md): the trust model stated plainly, how to report a vulnerability, and why auditing what you install stays your job.
* [`ERRATA.md`](ERRATA.md): corrections to figures this project has published, including the ones with no consumer that are deliberately kept out of this file.

## Status

**v1.7.0, released 2026-08-08.** Installs end to end as a Claude Code plugin. The hook system audited and hardened: two gates that were failing open now hold, session identity is never guessed, destructive database operations need explicit permission, and the test suite's isolation is enforced rather than assumed. Search numbers re-measured on 08-05 and 08-06 after a nondeterminism defect was found and fixed.

Every number in this file is either measured and dated, or derived from the current source tree. Where this file and the code disagree, the code wins.

## Acknowledgements

**[Superpowers](https://github.com/obra/superpowers), by Jesse Vincent.** Superpowers is a skills library for Claude Code, and reading it revealed real gaps in Writ's own methodology coverage. Watching its design choices work in practice is also what pushed Writ to think seriously about where description based skill matching holds up and where it runs out, which became the "Couldn't you just use skill files?" section above.

**[Jolli](https://www.jolli.ai/), by [JolliAI](https://github.com/jolliai/jolliai).** Jolli Memory turns AI coding sessions into structured development documentation attached to every commit, capturing the reasoning that would otherwise vanish the moment you commit. Writ's decision provenance family (capturing decisions to the graph, briefing new sessions with them, and pushing per file reasoning onto commits and pull requests) is adapted from concepts JolliAI pioneered. The digest eviction policy in particular is adapted from Jolli's ContextCompiler: the policy, not the code, and `writ/session/recall.py` documents the adaptation. Writ's addition on top is rule grounding. Every captured decision carries the rule identifiers that governed it, and those never get evicted.

If decision capture is what you are actually after and you do not need enforcement gates, go look at Jolli first. It is the more focused tool for that job and it works across every major git host.

License: MIT. Authored by Lucio Saldivar.
