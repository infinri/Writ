# ADR: Writ ships global settings keys the way it ships hooks

Status: accepted
Date: 2026-09-02
Plan: `.claude/plans/dfacff61-23d5-474e-846c-2e2f0f0ea482/plan.md`
Touches: `bin/lib/writ_install.py`, `writ/session/doctor.py`,
`scripts/patch-global-config.sh`, `docs/install.md`

## What this cycle verifies, and what it cannot

Stated first so it cannot be missed. This cycle proves that the installer WRITES
`outputStyle: "Concise"` into a settings file and that a read-back of that file finds the
key with that value. It does not prove that Claude Code honours the key, and no test in
this repo can. Claude Code ignores an unknown settings key with no error and no warning,
so a misspelled key written by the installer is a no-op that reads as success.

The evidence that `outputStyle` is the right key name is observational and it is the
strongest class available: Claude Code itself wrote that exact key into
`.claude/settings.local.json` when the user ran `/config`. That is an observation, not a
mechanism, so it is pinned in a test as a literal and named here as an observation.

Whether the style is actually applied is verifiable only by a human running a session and
looking at the output. That is marked `(operational)` in capabilities.md so nobody looks
for a test that was never meant to exist.

## Context

Writ is a harness, so any configuration it depends on has to live in the repo and be
applied by the installer, never left to a per-machine manual step. The user's directive:
"I rather have the configuration defined the same way we define our hooks that way on
install our configurations also get applied globally."

`scripts/patch-global-config.sh` already delivers the things a plugin manifest cannot
ship: the permission allow/deny entries, the `statusLine`, and `~/.claude/CLAUDE.md`. A
plugin may ship style DEFINITIONS through an `output-styles/` directory and the
`plugin.json` `outputStyles` field, but not the `outputStyle` VALUE, so a plain top-level
settings key belongs in that same bucket: Writ's own installer.

`templates/settings.json` is deliberately out of scope. It is generated from
`hooks/hooks.json` by `scripts/render-settings-template.py`, and
`tests/test_settings_template_sync.py` asserts that it owns nothing but hooks. Two writers
of one key would fight, so the ownership boundary was extended to name `outputStyle`
rather than crossed.

## Decision 1: the declaration shape

`BASE_ALLOW` and `DENY` do not drift because they are module-level tuples that both the
writer and the checker iterate. The equivalent for plain keys:

    MANAGED_SETTINGS = (
        ("outputStyle", "Concise"),
    )

Adding the next key is one tuple entry. Values are compared with `==` and assigned as-is,
so the shape already accepts any JSON value with no extra code. There is deliberately no
per-key policy field and no type field: every entry follows the one policy in Decision 2,
and the moment a future key genuinely needs a different policy is the moment to add the
field.

One helper reads the document, and it is the only place in the module that inspects a
managed key:

    def _managed_settings_state(doc):
        """[(key, shipped, current)] for every MANAGED_SETTINGS entry; current is _UNSET
        when the key is absent."""

`cmd_settings` and `cmd_check_settings` both call it and branch on `current is _UNSET` /
`current == shipped` / else. Neither function names a key literal.

Stated honestly: Python cannot make a second, drifted copy impossible. What the shape does
is make one DETECTED by construction rather than merely unlikely, through three
properties.

1. The key name `outputStyle` appears exactly once in executable code, inside the tuple.
   A writer that gained a key the checker did not see is not expressible, because the
   writer never spells a key out.
2. Presence classification lives in one function, so the writer and the checker can never
   disagree about which STATE a settings file is in. They deliberately disagree about what
   to DO about a state, and that asymmetry is the point of Decision 2.
3. The drift is proven by EXECUTION, not by reading.
   `tests/test_managed_global_settings.py::TestManagedSettingsDriftIsDetectedByConstruction`
   injects a SYNTHETIC entry that the source does not contain and requires the writer to
   write it and the checker to report it missing. A hardcoded
   `doc["outputStyle"] = "Concise"` in either function passes every count-based check and
   fails this one. Both halves were confirmed red under exactly that mutation before the
   implementation was accepted.

## Decision 2: never clobber, and what the installer prints

Writ never overwrites an existing managed key.

The `statusLine` precedent informs and leaves a foreign value untouched, and the case here
is stronger rather than merely equal. `statusLine` is Writ's own machinery, which is why
that branch can also REFRESH a stale `writ-statusline.sh` path: Writ can recognise its own
value. `outputStyle` is a user preference with no ownership marker, so "refresh ours" is
not even definable, and the only two options are clobber or leave. The installer runs on
every bootstrap and on every `writ doctor --fix`, so a clobbering writer would silently
revert a deliberate `/config` choice repeatedly, with no record. Writ's north star is to
relocate oversight, not to remove it; a config writer that overrides the human's expressed
preference removes it.

Three states, three behaviors. A genuinely absent key and a key the user set to the same
value are distinguished, as they must be.

- ABSENT: write it, then print
  `[settings] Set outputStyle to "Concise" (Writ's default; change it any time with /config).`
- PRESENT AND EQUAL: do nothing and print nothing about it. The existing whole-document
  comparison already reports "No changes needed" for an otherwise unchanged file, and
  per-entry silence matches how the permission merge behaves.
- PRESENT AND DIFFERENT: leave it and print two lines, mirroring the statusLine inform
  pattern:
  `[settings] outputStyle is set to "Explanatory"; leaving your choice untouched.`
  `[settings] Writ ships "Concise" for this key; change it with /config if you want Writ's default.`

One placement detail that is a correctness point rather than a cosmetic one. The
"Set outputStyle" line prints AFTER the write succeeds, never before the `--dry-run`
branch. Printed earlier it would claim an action during a preview that writes nothing,
which is the same class of defect as a successful write being read as a working setting.
The dry-run path prints nothing extra, because the unified diff it already emits shows the
added line. The inform lines for the DIFFERENT state are statements of fact rather than of
action, so they print in both modes.

Presence is tested with `in`, not with `.get()` truthiness. A key present with an explicit
`null` is the user's value and is left alone, reported like any other divergence. Absence
is not a policy: treating `null` as absent would let Writ overwrite something a user or
another tool wrote deliberately.

## Decision 3: the read-back, and the exit-code asymmetry

`cmd_check_settings` verifies key AND value by re-reading the FILE. That is the read-back,
and it is the only part of the chain Writ owns. Its policy matches the writer's, which is
the load-bearing detail:

- ABSENT is a finding: exit `EXIT_PRECONDITION`, with the missing key and its value
  printed on a BARE line.
- DIFFERENT is informational: the exit code is unaffected and the line carries the
  `[check-settings]` prefix.
- EQUAL is silent and counted in the summary.

The prefix is the mechanism, not decoration. `writ doctor` parses stdout by dropping
`[check-settings]`-prefixed lines and treating the rest as findings, so the DIFFERENT
state needs no doctor change at all.

Getting that asymmetry backwards is the hazard this decision exists to avoid. A checker
that reported DIFFERENT as missing would leave `writ doctor` permanently red on every
machine where the user chose another style, and `writ doctor --fix` would offer a fix that
by policy cannot fix it. That is the same hazard the checker's docstring already names for
the two DERIVED path entries it deliberately does not check.

## Alternatives rejected

- **Clobber, and let the checker be strict.** Rejected by Decision 2: the installer runs
  on every bootstrap, so this silently reverts a deliberate `/config` choice on a
  schedule.
- **A per-key policy field in the declaration.** Rejected as speculative. One policy
  covers every key shipped today; the field goes in when a key genuinely needs a different
  one.
- **A second doctor check for settings keys.** Rejected: the diagnosis differs while the
  remedy is identical, and the doctor's contract is (detect, offer fix). One check with a
  detail that names both kinds of item.
- **An allowlist of VALUES.** Rejected. Beyond `Concise` the value list is
  documentation-only, and Writ must not enforce a set it has not observed. One shipped
  value per key, and a user's differing value is never validated against a docs-derived
  set.
- **Renaming `_missing_allow_entries`.** Rejected. The name is now slightly historical,
  but renaming it edits 11 call sites across `tests/test_doctor.py` and
  `tests/test_hygiene_cycle_d.py`, two large modules whose subject is not this feature. A
  corrected docstring delivers the accuracy at the one place a reader lands.
- **Mocking `subprocess.run` for the doctor test.** Rejected in favour of an optional
  `target` parameter, following the existing convention in this repo
  (`WRIT_SETTINGS_TARGET`, `WRIT_PLUGIN_LIST_CMD`, the `docs=` keyword on
  `emdash_substitute_hits`). Asserting against a mock's own return value would prove
  nothing about the diagnosis path.
- **Detecting or rewriting project-local settings files.** Rejected. The installer is a
  GLOBAL config writer by contract; reaching into arbitrary project directories to delete
  a key the user or Claude Code wrote is precisely the clobbering behavior Decision 2
  rejects, aimed at a file Writ owns even less; and Claude Code's precedence between a
  project-local settings file and the global one is UNVERIFIED here, so Writ cannot
  honestly claim a project-local value is harmful.

## The doctor's `target` seam

`_missing_allow_entries` gained an optional `target` parameter whose default IS the real
per-user settings path, because `writ doctor` with no argument has to diagnose the actual
machine. The function is read-only: it shells out to `check-settings`, which writes
nothing, ever. The default is pinned by a test so nobody can silently repoint it or
silently un-default it, and every test in this cycle passes an explicit temp path.

## Recorded, not fixed: that default is bound once, at import

`writ/session/doctor.py:420` writes the default inline:

    def _missing_allow_entries(
        target: Path = Path.home() / ".claude" / "settings.json",
    ) -> list[str]:

Standard Python default-argument semantics apply: the expression is evaluated ONCE when
the module is imported, not per call, so the `Path` object is fixed for the lifetime of
the process.

Safe today, and the reasons are specific rather than general. There is one call site
(`doctor.py:1084`, zero-argument), `writ doctor` runs as a fresh process every time, and
every test in this cycle passes an explicit temp path instead of relying on the default.
The function is read-only, so a stale value could at worst produce a wrong DIAGNOSIS,
never a wrong write.

The exact condition that would make it unsafe: `_missing_allow_entries` being called from
a long-lived process (the daemon) after `HOME` changed within that process, or after the
module was imported under a different `HOME` than the one in effect at call time. Then the
default would point at the settings file of the wrong user, and the check would report
findings about a machine state nobody is looking at.

Deliberately NOT fixed, and the obvious fix is the wrong one. Switching to `target=None`
and resolving inside the body would break
`tests/test_managed_global_settings.py::TestNoDefaultEverResolvesToTheRealSettingsFile::test_doctor_seams_default_is_pinned_to_the_real_path`,
which reads the signature and pins the default deliberately, so nobody can silently
repoint or silently un-default the one function in this feature allowed to name the real
file. That pin is worth more than the latent hazard: the hazard needs a second call site
in a long-lived process before it can bite, and this note is what turns "add a caller and
find out" into "add a caller and read this first".

## Queued, not built: a positive signal for "the style is actually applied"

One path to a genuine POSITIVE signal was identified and is deliberately not built here.
Writ already runs a `statusLine` command that receives a JSON envelope on stdin, and if
that envelope carries the effective output style, Writ could record what Claude Code is
actually USING rather than what Writ asked for.

Nothing in this repo has ever observed such a field: `hooks/scripts/writ-statusline.sh`
reads `context_window` only, and the synthetic envelope in `tests/test_pol5a_statusline.py`
carries `model`, `workspace`, `version` and `context_window`. So the field is unverified
and may not exist. The minimal probe is to tee the statusLine stdin to a file for one
session and read it back. Until somebody runs that probe, the read-back in Decision 3 is
the whole of what Writ verifies, and this document says so rather than implying more.
