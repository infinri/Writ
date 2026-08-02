# Writ

A Claude Code harness that enforces engineering discipline at the moment the AI acts, and delivers the rules that fit the work in front of it.

## In plain terms

Give a coding AI a long list of rules and two things go wrong. It forgets them as the conversation grows, and it has no obligation to follow them anyway. You can ask an AI to write tests before code. Nothing makes it do so.

Writ changes where the rules live. Instead of putting them in the conversation and hoping, Writ sits between the AI and your files. When the AI tries to write code before you have approved a plan, the write is refused. Not discouraged, refused. And instead of showing the AI all 287 rules every time it does anything, Writ looks at what the AI is actually doing right now and hands it only the rules that apply.

The refusal part is the point. The retrieval part is what makes the refusal affordable when your rulebook grows past a handful of rules.

## The claim, and its limits

**The claim.** Instructions in a prompt decay. They get compacted away, outweighed by more recent text, or quietly reinterpreted. A check that runs at the moment the AI calls a tool does not decay. Writ moves workflow discipline to that boundary, where a violation is refused rather than requested, and moves the rulebook out of the prompt into a search system, so the rulebook can grow without the cost of every turn growing with it.

**The threat model.** Writ assumes a **cooperative AI**: one that uses its tools in the ordinary way and is not trying to defeat the harness. Under that assumption the gates hold. Against an AI actively working around them, they do not, and the gaps are written down rather than hidden:

* Writes made through shell commands are inspected, but a determined workaround (an inline Python one liner, an evaluated string, a heredoc) can slip past the inspector.
* When the background service is unreachable, hooks **allow rather than block**. This is the specification, not a bug. An infrastructure outage must never lock you out of your own repository.
* Sub agents (helper AIs spawned by the main one) skip the write gates by design. Their limits come from the tools their role grants them, not from re-checking work the human already approved.

Two boundaries hold no matter what. Writes to credential files (keys, `.env`, SSH material) are refused in every mode with no server involved. And the approval token cannot be created or spent without a human keystroke, so **advancing the workflow and writing new rules into the rulebook halt even when raw file writes do not.**

If you need enforcement against an AI that is actively adversarial, Writ is not that tool. If you need a cooperative AI to actually follow a process, it is.

## Install

**You will need:** Python 3.11 or newer, Docker (the graph database runs in a container), and the command line tools `jq`, `curl`, and `envsubst`.

```shell
claude plugin marketplace add infinri/Writ
claude plugin install writ@writ

WRIT_DIR=$(claude plugin list --json \
  | python3 -c "import json,sys; print(next(p['installPath'] for p in json.load(sys.stdin) if p['id'].split('@')[0] == 'writ'))")

bash "$WRIT_DIR/scripts/bootstrap-plugin.sh"      # environment, database, rules, service
bash "$WRIT_DIR/scripts/patch-global-config.sh"   # permissions and workflow instructions
```

Restart Claude Code and check it worked with `curl http://localhost:8765/health`.

Nothing breaks while you are partway through setup. Hooks stay out of the way until the install finishes, sessions are never blocked, and the startup hook prints exactly what is still missing. Full install detail, the manual path, and troubleshooting live in [`docs/install.md`](docs/install.md).

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

## How the rules reach the AI

**The floor: rules that can never be dropped.** Thirty three of the 287 shipped rules are marked mandatory. These are deliberately kept **out of the search index entirely** and delivered through a separate channel with its own budget. That means no change to search ranking, no swap of the underlying model, no retuning of anything can cause a critical security rule to fall off the list. A single definition in one file decides what belongs to the floor, and both the delivery code and the validation code read that same definition, so the two can never drift apart. This closed a real bug where two parts of the system checked different fields and left 29 of 32 mandatory rules unreachable by either path.

**Everything else is searched for.** A five stage pipeline runs over a Neo4j graph database: narrow the candidates, keyword search, meaning based search (so a rule about "SQL" surfaces for a question about "database queries"), a walk across the graph to pull in related rules, then weighted ranking. Each stage covers a blind spot the others have. Keyword search catches exact terms. Meaning based search catches paraphrase. The graph walk catches rules that share no words at all but are causally connected. If nothing matches well enough, the pipeline **returns nothing** rather than injecting noise.

**The search fires on what is happening, not just what you typed.** Thirty seven small scripts watch the session and attach real context to the query: which file is being written, what is inside it, which tool is running, what phase the workflow is in. A rule about SQL injection surfaces when the AI writes a file containing a query, not only when someone happens to type the word SQL.

## "Couldn't you just use skill files?"

For a dozen behaviors, yes, and you should. Anthropic's Agent Skills format solves its problem well. A folder with a markdown file, its name and description pre loaded so the AI knows when the contents apply. Easy to write, no infrastructure, nothing to run. If that covers your case, Writ is overkill and you should not install it.

Two things a markdown file structurally cannot do.

**It cannot refuse a write.** This is a category difference, not a matter of degree. A skill file can describe test driven development in loving detail. Nothing in the format interrupts the moment the AI calls its write tool. The AI decides whether the skill applies, and an AI that decides wrong fails silently, which is the worst kind of failure because you never find out. Writ's gates are refusals made by code, in a process the AI does not control, and every refusal is a logged event rather than an absence you would have to notice.

**The trigger has to be in your message.** Skills work by matching their pre loaded descriptions against what you typed. A rule that must fire when the AI writes a controller containing a raw SQL string cannot work that way, because the thing that should trigger it is the file content at write time, which nobody typed.

The search argument matters too, but treat it as secondary because it is weaker. Pre loaded descriptions blur together as their count grows, overlapping descriptions cause the AI to pick one and silently skip the other, and a request touching several areas gives it multiple plausible matches with nothing to break the tie. At 287 rules, designed to scale into the thousands, the matching decision has to move out of the AI. None of this is a flaw in the Agent Skills spec. It is the boundary of what description matching can do.

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

Live rulebook of 287 rules, measured 2026-08-01, on a 16 thread AMD Ryzen 9 7940HS with 31 GiB of RAM and a database container with no memory cap. Your numbers will differ on other hardware. The full disclosure is in [`SCALE_BENCHMARK_RESULTS.md`](SCALE_BENCHMARK_RESULTS.md).

| | Live (287 rules) | Synthetic (10,000 rules) |
|---|---:|---:|
| Search time, 95th percentile | 0.6 ms | 0.827 ms |
| Rule text delivered per turn | about 1,900 tokens | about 1,590 tokens |

The property that matters is the second row. The amount of rule text sent per turn stays roughly flat as the rulebook grows.

**An honest note on the baseline.** The often quoted "749 times less context" is measured against pasting the entire 10,000 rule corpus (1.19 million tokens) into every single message. That is a theoretical ceiling, not something anyone does, since no context window holds it. Against the realistic comparison, a hand curated instructions file of about 5,000 tokens, Writ's per turn cost is roughly comparable while covering 287 rules instead of a dozen. The advantage grows with the size of your rulebook rather than with the size of the claim.

Search quality against a 193 question test set (47 of them deliberately ambiguous). The floors are automated gates the build fails below, set deliberately under the measured values:

| Metric | Floor | Measured 2026-08-01 |
|---|---|---|
| Mean reciprocal rank at 5 (ambiguous, n=47) | at least 0.45 | 0.5681 |
| Hit rate at 5 (all 193) | at least 0.75 | 0.7824 |
| Domain hit rate at 5 | at least 0.90 | 0.9323 |
| nDCG at 10 | at least 0.65 | 0.7071 |

## Not measured

Everything above measures what the search **costs** and how well it ranks. None of it measures whether an AI given the right rule **actually complies** more often than one given nothing. That is Writ's central claim and it is currently unproven.

The harness to test it exists. It runs matched Claude Code sessions with Writ on and Writ off against a deliberately planted security defect, scoring whether the defect was caught and at what cost. It has not been run at a scale that proves anything. At one repetition the result is reported as insufficient by design, because a single run cannot beat the randomness in how AI sessions unfold. What is still needed: many repetitions with a noise floor, a defect suite broader than the single planted case, and a cheaper scoring judge.

Until that exists, treat the enforcement claim as a designed mechanism with an honestly documented failure posture, not a demonstrated outcome.

What **is** independently checkable today lives in the repository rather than in assertions. [`docs/pressure-runs/`](docs/pressure-runs/) contains adversarial test runs against real Claude Code sessions: the exact prompt used, the full transcript, every enforcement decision as raw log lines, and a graded analysis scoring each targeted rule as held or bypassed, including the failures, documented as failures. [`docs/monthly-reviews/`](docs/monthly-reviews/) contains operational reviews built from the system's own audit log.

## The rulebook is opinionated

287 rules ship in the box: 76 security, 45 code quality, 28 architecture, 21 testing, 19 performance, 18 process, and smaller sets besides. The shape reflects where its author has worked. There are 12 Magento 2 rules and exactly one PHP typing rule, which tells you something true about where it came from.

Treat the shipped rulebook as a working example, not a universal standard. Commands for adding and editing rules exist so you grow your own, and there is a full lifecycle for rules the AI itself proposes: a proposed rule lands marked provisional, gets promoted to a review queue only after enough real world evidence accumulates, and enters the canonical rulebook only through a human approval that requires the same one time secret as everything else. The statistics never promote anything on their own.

## Also in here: the Claude Code hook black box

[`docs/reference/claude-code-blackbox.md`](docs/reference/claude-code-blackbox.md) is a version pinned, empirical map of exactly what Claude Code hands a hook script and exactly what a script can hand back. Captured live on build 2.1.220 and compared against 2.1.183. Every single field carries an evidence tag: observed in real data, documented but not seen, or unverified.

It records five events that moved from documented only to actually observed, payload fields the public changelog never announced, and the mechanism that lets a script rewrite a tool call before it runs without the AI ever seeing the change. It is written so a non engineer can follow the idea in Part 1 and an engineer can build against the detail in Part 2.

It is useful whether or not you use Writ. It is the reference this project wishes had existed.

## Architecture, in your browser

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
* [`CHANGELOG.md`](CHANGELOG.md): release history through v1.6.0.

## Status

**v1.6.0, released 2026-08-01.** Installs end to end as a Claude Code plugin. Every published number re-measured on disclosed hardware. The 37 script hook system audited end to end. The Claude Code contract re-pinned to build 2.1.220.

Every number in this file is either measured and dated, or derived from the current source tree. Where this file and the code disagree, the code wins.

One small thing this document practices rather than describes: Writ ships a rule that blocks the AI's own output when it contains an em dash, an en dash, or a double hyphen in prose. That rule is enforced by a hook at the end of every turn, and this README is written to it.

## Acknowledgements

**[Superpowers](https://github.com/obra/superpowers), by Jesse Vincent.** Superpowers is a skills library for Claude Code, and reading it revealed real gaps in Writ's own methodology coverage. Watching its design choices work in practice is also what pushed Writ to think seriously about where description based skill matching holds up and where it runs out, which became the "Couldn't you just use skill files?" section above.

**[Jolli](https://www.jolli.ai/), by [JolliAI](https://github.com/jolliai/jolliai).** Jolli Memory turns AI coding sessions into structured development documentation attached to every commit, capturing the reasoning that would otherwise vanish the moment you commit. Writ's decision provenance family (capturing decisions to the graph, briefing new sessions with them, and pushing per file reasoning onto commits and pull requests) is adapted from concepts JolliAI pioneered. The digest eviction policy in particular is adapted from Jolli's ContextCompiler: the policy, not the code, and `writ/session/recall.py` documents the adaptation. Writ's addition on top is rule grounding. Every captured decision carries the rule identifiers that governed it, and those never get evicted.

If decision capture is what you are actually after and you do not need enforcement gates, go look at Jolli first. It is the more focused tool for that job and it works across every major git host.

License: MIT. Authored by Lucio Saldivar.
