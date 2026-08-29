# ADR: the post-write analyzer run is NOT a duplicate of the pre-write one, and stays

Status: accepted (plan `dfacff61-23d5-474e-846c-2e2f0f0ea482`, 2026-08-28)

## Context

Two hooks run `bin/run-analysis.sh` on every write. `hooks/scripts/pre-validate-file.sh`
reconstructs the proposed content into a temp file and analyses that BEFORE the write;
`hooks/scripts/validate-file.sh` analyses the target path AFTER it. The approved design's
Tier 1 proposed removing the second run as duplicated work, worth roughly 125 ms per clean
write, on the hypothesis that both runs analyse the same bytes. The design also said to
settle that hypothesis first, because it decides whether the saving exists at all, and gave
the conditional: if the answer is no, the post-write run is the authoritative one and stays.

**The byte claim is CONFIRMED.** The reconstruction produces the bytes that land. The Edit
tool requires `old_string` to be unique unless `replace_all` is set, and the live capture
measures the `replace_all` population directly: of 1,177 Edit payloads, 1,170 carry
`replace_all: false`, 7 carry no `replace_all` field at all, and ZERO carry it true
(`edit_replace_all` in `docs/reference/blackbox-census.json`). This is not re-litigated here.

**The computation claim is REFUTED, and that is what decides redundancy.** Each language
analyzer is invoked as `<tool> "$file"`, so each tool resolves its own CONFIGURATION from the
analysed file's own directory:

    bin/lib/analyzers-lang.sh:21    "$phpstan" analyse "$file" --level="$level" ...
    bin/lib/analyzers-lang.sh:81    "$phpcs" "$file" --standard="$standard" ...
    bin/lib/analyzers-lang.sh:144   "$eslint" "$file" --format=json
    bin/lib/analyzers-lang.sh:181   ruff check "$file" --output-format=json

The two runs pass different paths to those lines. The pre-write run analyses a temp file
under `/tmp` (`hooks/scripts/pre-validate-file.sh:67`, `tempfile.mktemp(..., dir='/tmp')`);
the post-write run analyses the target path (`hooks/scripts/validate-file.sh:35`).

`--project-root` does not close the gap. Every consumer of `PROJECT_ROOT` inside
`bin/lib/analyzers-lang.sh` is either `read_project_config` (lines 13 and 14, which select the
phpstan level and the phpcs standard) or `find_tool` (lines 17, 78 and 141, which select the
tool BINARY). It never reaches the tool's own config discovery. The one exception proves the
rule: the rust analyzer at line 231 runs `cd "$PROJECT_ROOT" && cargo check`, so cargo is the
single analyzer that is already location-correct, and it is also the one that ignores the
file argument entirely.

So the post-write run is the ONLY run that sees a project's own linter configuration, and on
any project that has one it is not a duplicate but the authoritative run.

**This was verified by triggering, not by reading.** `tests/test_prewrite_reconstruction.py::TestPathSensitivityControl`
runs the real `bin/run-analysis.sh` twice on identical bytes, once at a project path and once
at a `/tmp` path:

- with no linter config anywhere, both runs report F401 and the findings sets are EQUAL;
- with a project-level `ruff.toml` carrying `ignore = ["F401"]`, the project path reports
  nothing and the `/tmp` copy still reports F401, so the findings sets DIFFER.

Both halves pass on this machine today. The second one is written so it can genuinely fail:
if it ever comes out equal, this ADR's premise is refuted on that machine.

**This repo is structurally blind to the regression the skip would have caused.** There is no
`[tool.ruff]` section in `pyproject.toml` and no `ruff.toml` at the root, so here the two runs
agree, every test would have stayed green, and the behaviour would have regressed only on the
user's configured projects.

**A second, independent reason the two verdicts already differ.**
`hooks/scripts/pre-validate-file.sh:128-139` re-analyses the file as it stands and keeps only
what the edit ADDS, so a file carrying pre-existing analyzer errors passes the pre-write gate.
`hooks/scripts/validate-file.sh` applies no such filter. For every such file today's pair is
"pre-write allows, post-write reports", and the post-write run is the only thing that reports.
Any skip would have to carve out that whole class as well.

## Decision

The post-write analyzer run STAYS. `hooks/scripts/validate-file.sh` is not modified, and its
`DEFERRED_SCRIPTS` entry in the fire drill keeps its existing reason.

No skip of the post-write analysis is built, in any form, however guarded. Anyone who becomes
convinced the skip is safe should stop and put the measurement in front of the user, who is
the only one who may re-open this decision; a control test that agrees with the premise is not
by itself an authorisation to build the skip.

What Tier 1 does ship is the `replace_all` fix, because it is a defect and not an
optimisation. `hooks/scripts/pre-validate-file.sh` hardcoded a replacement count of `1`, so an
Edit carrying `replace_all: true` would have been pre-validated against content that never
lands. The reconstruction now reads:

    ra = ti.get('replace_all')
    content = content.replace(ti['old_string'], ti.get('new_string', ''), -1 if ra is True else 1)

`str.replace` reads a count of -1 as replace-all, so this is one expression and no new branch.
The test is `ra is True` on purpose: an absent field and an explicit `false` both keep today's
single replacement, which is exactly the measured population above.

## Alternatives considered

1. **A marker file keyed on `tool_use_id` carrying a content hash, with the post hook reusing
   the pre-write verdict on a hash match.** Rejected: the hash proves byte identity, which was
   never the doubtful part. It cannot prove configuration identity, so it buys a guarantee
   nobody needed and leaves the actual difference in place.
2. **Moving the pre-write temp file into the target file's own directory so both runs resolve
   the same config.** Rejected: it makes the PRE-write gate stricter on configured projects,
   because path-keyed rules that the temp basename does not match would begin to fire. That is
   a change to a refusing surface, which the cycle's acceptance criterion 1 forbids.
3. **Allowing the skip only when the project declares no linter config, by testing for known
   config filenames.** Rejected as the enumeration trap this program has already hit four
   times: the instrumentation scan answered 21, then 12, then 10, then 8 as spellings were
   added. A missed spelling here silently over-grants the skip and loses findings, and no test
   can see it.
4. **A session-scoped equivalence probe: analyse identical bytes at both locations once per
   session per language and cache the answer.** Sound, and rejected on cost of fragility: five
   moving parts including a write into the user's project directory, to recover about 125 ms
   of a 795 ms write, which the honest ceiling puts at single-digit percent of turn wall clock.
   The standing ruling applies, which is to drop a tier rather than ship a fragile one.

## Consequences

- **About 125 ms per clean write is kept, in exchange for the project's own linter
  configuration staying enforced.** That trade is accepted deliberately.
- **The 125 ms figure is a per-HOOK total, not an analyzer cost, and no later cycle should
  spend it as though it were.** `hooks/scripts/validate-file.sh:50` spawns
  `bin/lib/writ-session.py update` on every write for coverage bookkeeping, which is a full
  Python start plus a package import, and line 85 spawns `emit-summary.py` on the failure
  path. Intra-hook cost attribution is therefore a precondition on Tiers 2 and 3, which are
  the tiers that actually change latency.
- **A residual saving does exist and is deliberately not taken.** On a project that declares
  no linter configuration for the file's language, the post-write run IS a true duplicate.
  `write_target_extensions` in the census artifact is the field that sizes it. It is not built
  here because the only safe way to know the condition holds is to measure it per project,
  which is rejected alternative 4.
- **The fix is proved twice, on purpose.** A unit test pins the three `replace_all` shapes
  against the reconstruction's real source text, extracted verbatim from the script rather
  than hand-copied. The fire drill adds a real `shell-syntax` refusal whose payload carries
  `replace_all: true` and whose reconstructed content fails `bash -n`, so the fix is proved
  through a real subprocess refusal that could not fire correctly before it.

## The census schema decisions, recorded here for the same reason

`writ blackbox-census` is the audit that sized the numbers above, and its schema choices are
load-bearing enough that a later cycle would otherwise re-derive them.

- **`records` stays the SINGLE artifact key.** A previous cycle removed a dual-key fallback on
  purpose and wrote down why (`writ/shared/delivery.py`, `_census_record_classes`): two
  accepted spellings for one artifact means a typo in either producer reads as an empty census,
  which tags genuinely observed entries unproven and looks like missing evidence rather than a
  broken reader. `synthetic_records` is a SIBLING of `records`, never an alternative to it, and
  `writ/shared/delivery.py` needed no change at all.
- **Classification is three-way, on a POSITIVE fingerprint.** A row is synthetic when it
  carries no `hook_event_name` at all, harness when it carries `hook_event_name` plus
  `transcript_path` or `prompt_id`, and undetermined otherwise. Filtering on `hook_event_name`
  alone would have been necessary but not sufficient, and the artifact proved it: the
  `PreToolUse|pre-validate-file|in` class showed 99 rows carrying `hook_event_name` against 78
  carrying `transcript_path`, so 21 probe rows would have been filed as real traffic. `cwd`,
  `session_id` and `tool_use_id` are excluded from the fingerprint because a hand-built probe
  plausibly carries all three.
- **Undetermined rows STAY in `records`.** Moving them out would strip every OUT record class
  of its evidence, because an OUT row's origin is inherited and often unresolvable, and would
  turn every provenance entry unproven for a reason that is about capture coverage rather than
  about delivery. The residual stays visible as data instead: `origin_counts` globally and
  `origins` per entry, each key present with an explicit zero.
- **An OUT row inherits the origin of the most recent IN row sharing (hook, pid, session),
  and is undetermined with no such row.** One hook process handles one envelope, so
  same-process is the correct join, it costs nothing during a single ordered fold, and it is
  falsifiable: a log interleaving a probe and a real envelope for the same hook must attribute
  each OUT row to its own process. Recording OUT origin at capture time, rather than inheriting
  it, belongs to the deferred OUT-capture-coverage cycle.
- **One known classification exception, recorded rather than hidden.** The fire drill's
  comms-gate setup does build a `transcript_path`, so a drill row reaching the real capture log
  would classify as harness. That cannot happen today because the drill pins `HOME` and
  `TestRealCaptureLogNeverTouched` snapshots the real log around a hook run. The classification
  depends on that guard staying in place.
- **`record_count` and `capture_window` span BOTH `records` and `synthetic_records`.** They
  describe the capture log, and narrowing them alongside `records` would silently change what
  two already-published numbers mean. The `writ blackbox-census` summary line prints the
  harness, undetermined and synthetic split beside `record_count` for the same reason: without
  it, a reader takes the total for a count of real Claude Code traffic, which overstates the
  evidence by roughly a quarter. EVERY COUNT IN THIS ADR IS A DATED SNAPSHOT, NOT A FACT:
  capture was live while this was written and the artifact grew between two runs of the
  same command (7,970 rows, then 8,046, then 9,274). Re-derive rather than cite these:
  `writ blackbox-census --json`. The argument in this ADR does not rest on any of them.
- **Absence is stated as data, never implied by a missing key.** `hooks_never_captured`,
  `directions_never_observed`, `edit_replace_all`, `write_target_extensions` and
  `write_rows_without_file_path` are each present with an explicit zero or empty value even for
  an absent or empty log. `directions_never_observed` is derivable from the `records` keys by
  any reader and is included anyway, because a missing OUT class is precisely a missing key.
- **One per-event roll-up was deliberately NOT added**: a union of fields across hooks. It is
  derivable from `records`, and duplicating a presence aggregate creates a second population
  that can disagree with the first. The absence statements above are the different case, since
  their whole purpose is that they cannot be inferred from what is missing.
- **One consequence of routing by ORIGIN rather than by event name.** An OUT row whose payload
  names no event and whose process cannot be resolved is filed under an `unknown` key inside
  `records`, not inside `synthetic_records`, because it is undetermined rather than proven
  synthetic. The real log carries exactly one such class today
  (`unknown|writ-pre-write-dispatch|out`, no mechanisms, so it can satisfy no
  provenance claim either way). The property that holds without exception is the narrower one:
  a row that CARRIED a `hook_event_name` never appears under `unknown`.
