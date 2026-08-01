# Efficacy A/B runbook

`writ efficacy-ab` measures whether Writ actually changes agent behavior (catches a planted defect) and at what total cost, by running real, controlled Writ-on vs Writ-off Claude Code sessions. Unlike `writ token-audit` (which reads existing transcripts for free), a live run **spends your Claude Code quota**: the same auth as interactive `claude`, no separate key. Nothing runs automatically; this is the procedure for the day a specific lever needs efficacy evidence.

## When to run

Only when shipping an efficacy-affecting lever (summary-mode default, gate on/off) that must prove it holds defect-catching while lowering cost. Periodic, never per-change; do not wire into CI or a loop.

## Cost and guardrails

- The CLI **defaults to a dry run**: it prints the plan and a cost estimate and spawns nothing. `--live` is the only path that spawns real `claude` runs (`--reps` defaults to 1).
- The shipped suite (`tests/efficacy_suite/`: a planted-IDOR defect arm and a clean cost-of-presence arm) at 1 rep = 4 runs, roughly a dollar-plus equivalent; the smallest honest loop-proof is 1 task x 2 variants = 2 runs.

## Procedure

```bash
cd <install root>

# Step 0: dry run (free; expect "DRY RUN: 4 runs ...", exit 0, no spawn)
.venv/bin/writ efficacy-ab tests/efficacy_suite

# Step 1: build-spikes, confirm once before trusting --live (~2 trivial runs of spend)
#  B1: does `claude --settings <file>` MERGE with ~/.claude/settings.json or REPLACE it?
#      Run a trivial prompt with and without the generated no-hooks settings and check
#      the transcript for Writ hook activity. The generated file carries its own
#      permission allowlist, so it is safe under either semantics.
#  B2: does the writ-ON arm actually inject? Restart the daemon
#      (systemctl --user restart writ-server), run one writ-on task, and confirm a
#      rag_query / always_on_inject event in that run's isolated friction log.
#      No injection event = the comparison is measuring nothing.

# Step 2: live
.venv/bin/writ efficacy-ab tests/efficacy_suite --live         # add --judge, --json as needed
```

Prerequisite for the writ-on arm: the daemon must be up.

## Reading the result

- `defect/writ-on` vs `defect/writ-off`: was the planted IDOR caught? The `signal` label matters: `gate` (a mandatory rule denied the write: binary, trustworthy) vs `judge` (the LLM read the diff: softer, audit it).
- `clean/*` is cost-of-presence; a gate firing there is a false positive (`spurious_gate`).
- At `--reps 1` the verdict is `insufficient_n` **by design**: one draw cannot beat agentic non-determinism. A single run proves the loop works, never a lever.
- **Do not misread the generic verdict** ("pass = cost drops and defect-caught holds"). That semantic fits a summary-vs-full lever; it does not fit writ-on/off, where Writ deliberately spends more to buy coverage. For on/off, read the two numbers (catch-rate delta at what cost premium), not the flag.

## Deferred before this is a real gate

N-rep distributions with a noise floor and confidence interval; a broader defect suite beyond IDOR; full-vs-summary and placement levers as variant profiles (needs a server-side render-mode parameter that does not exist); per-lever verdict semantics; a cheaper, calibrated LLM judge.
