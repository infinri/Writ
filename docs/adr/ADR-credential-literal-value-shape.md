# ADR: the credential scanner judges the VALUE's shape, and gives up one thing to do it

Status: accepted
Date: 2026-09-14
Plan: `.claude/plans/2412ba38-51e1-4b73-895b-7b240a3c21d3/plan.md`
Touches: `bin/lib/analyzers-regex.sh` (`IDENT_ASSIGN`, `IDENT_ASSIGN_LOWER`,
`PLACEHOLDER`, `NAMESPACE_PREFIX`), read through
`hooks/scripts/pre-validate-file.sh`, `hooks/scripts/validate-file.sh` and
`bin/audit-region.sh`

## What this cycle gives up, stated first

This change deliberately opens a false negative in a security scanner. A real secret that
BEGINS with a lowercase letter, continues in lowercase letters, digits, hyphen and
underscore, and ENDS in a bare `-` or `_`, assigned to a credential-named identifier, is no
longer reported by the identifier predicate. `API_KEY = "correct-horse-battery-"` is the
shape. (A value opening on a digit, `"3f9a2b7c...-"`, is still caught: `NAMESPACE_PREFIX`
requires a letter first. That detail matters because most generated hex and base64 secrets
open on a digit a good fraction of the time.)

That is a permanent, accepted gap, not an oversight and not a bug to fix later by widening
a regex. It is pinned by a strict xfail at
`tests/test_credential_scanner_predicates.py::TestResidualBlindSpot`, which will redden the
day anything closes it, so the marker is deleted rather than quietly outlived.

### The instance of it worth naming: a VENDOR-ADJACENT value

Measured, not reasoned:

    API_TOKEN = "sk_live_aaaaaaaaaaaaaaaaaaaa-"     ADMITTED  (no finding at all)
    API_TOKEN = "sk_live_aaaaaaaaaaaaaaaaaaaa"      stripe-live + credential-literal-assign
    API_TOKEN = "sk_live_AAAAAAAAAAAAAAAAAAAA"      stripe-live + credential-literal-assign

A single trailing hyphen turns a caught Stripe-shaped value into a silent one, and it takes
BOTH halves of the scanner to do it. The vendor pattern is
`['\"](sk_live_[A-Za-z0-9]{16,})['\"]`, which requires the closing quote IMMEDIATELY after
the alphanumeric run, so a trailing separator falls outside it; the identifier predicate was
the thing that caught that value anyway, and `NAMESPACE_PREFIX` now exempts it. So this is
not a new blind spot, it is the disclosed one landing on a value that looks like a vendor
secret, and the vendor loop's own anchoring is why the second line of defence is not there.

Real-world likelihood is low: vendor suffixes are drawn from mixed-case alphabets, and a
single uppercase character anywhere in the value defeats `NAMESPACE_PREFIX`. It is named
here rather than left for someone to rediscover, because "the vendor patterns are untouched"
reads like a stronger guarantee than it is whenever the value's tail is not what the vendor
regex expects.

## Context

`SEC-CRYPTO-KEY-001` in `bin/lib/analyzers-regex.sh` has two halves. The vendor half is a
list of seven literal prefixes (`sk_live_`, `sk_test_`, `AKIA`, `xox[baprs]-`, `ghp_`,
`gho_`, PEM headers) matched against the whole line. The identifier half is a pair of
regexes that catch "a credential-named identifier is assigned a string literal of eight or
more characters", with a prefix-anchored `PLACEHOLDER` allowlist of obvious dummy words.

Three defects were live in the identifier half.

**A bypass.** Both identifier predicates required the opening quote to sit immediately
after the assignment operator:

    \s*[:=]\s*['\"]([^'\"]{8,})['\"]

so `API_TOKEN = "<real secret>"` matched and `API_TOKEN = f"<real secret>"`,
`r"<real secret>"`, `rb"<real secret>"` and C#'s `@"<real secret>"` and `$"<real secret>"`
did not. Every string-literal prefix was a free pass with the secret still sitting in the
source.

**Two false positives.** A lowercase namespace prefix (`phase3b-`, `approve-`,
`evidence-`, `reviewpromote-`, `advance-from-complete-`) assigned to a `TOKEN`-named
constant was refused at write time, which blocked two test modules from declaring the
gate-token namespace their own leak sweeper reads. And `bin/lib/test_paths.py:150`
(`_SESSION_CACHE_TOKEN = "{session_cache}"`) was refused because `PLACEHOLDER` exempted
`<.+>` but had no brace equivalent.

## Decision

Four edits, one commit, widening first.

1. Both identifier predicates accept an optional string-literal prefix:
   `[A-Za-z_$@]{0,2}` between the assignment operator and the opening quote. This closes
   the bypass for `f`, `r`, `b`, `rb`, `u`, `fr`, `@` and `$`.
2. `PLACEHOLDER` is rebuilt as a WHOLE-VALUE judgment and gains brace templates. See
   "`PLACEHOLDER` is anchored at both ends" below, which is the part of this change that
   removes an existing hole rather than adding an exemption.
3. A new predicate, `NAMESPACE_PREFIX = re.compile(r"[a-z][a-z0-9_-]*[-_]")`, checked with
   `fullmatch` on the CAPTURED value.
4. The dispatch gains one conjunct in the identifier branch only:
   `if not PLACEHOLDER.fullmatch(literal) and not NAMESPACE_PREFIX.fullmatch(literal):`.

### `PLACEHOLDER` is anchored at both ends, and that closed a live laundering hole

`PLACEHOLDER` was a `^`-anchored pattern consulted with `.match()`. `.match()` anchors only
the START of the captured value, so a decoy prefix laundered an arbitrary real secret.
Reproduced against the first cut of this cycle, with the same body in every row:

    API_TOKEN = "{name}<real secret>"            ADMITTED
    API_TOKEN = "<placeholder><real secret>"     ADMITTED
    API_TOKEN = "{session_cache}"                ADMITTED   (the intended exemption)
    API_TOKEN = "<real secret>"                  FLAGGED    (control)

No shape restriction applied: any case, any length, any entropy, laundered by six
characters. The `<.+>` half was PRE-EXISTING; the brace twin, added in this cycle by
analogy with it, was a second instance of a live defect. The word entries carried the same
hole: `"test" + <real secret>` was admitted for the same reason.

A blanket `fullmatch` is NOT the fix, and that is worth stating because it is the obvious
move. The word entries are deliberately prefix matches: `"test-mode-engine"` and
`"your-api-key-here"` are real admitted values in this tree, and a blanket `fullmatch`
refuses both. The shape that holds is:

* WORD entries stay prefix matches, followed by TAIL, a deliberately narrow continuation
  alphabet: `[a-z0-9._-]`. No uppercase, which is exactly what refuses
  `"test" + <mixed-case secret>`. The words are therefore made case-insensitive through a
  SCOPED `(?i:...)` group rather than a global `re.IGNORECASE` flag, because a global flag
  makes `[a-z]` match uppercase too and silently re-opens the hole it is there to close.
  This is the one detail most likely to be undone by a later "simplification".
* BRACKET entries (`<...>`, `{...}`) become TEMPLATE shapes: the value must consist of
  `<...>` / `{...}` interpolations and TAIL glue and nothing else, with at least one
  interpolation. A plain `fullmatch` of `\{.+\}` would have refused
  `f"{_TEST_SCOPE}-"` (`tests/test_decision_memory_capture.py:86`) and broken an
  already-wired module, which is why this is a segment grammar and not one greedy `.+`.
  The INSIDE of an interpolation is unrestricted on purpose: it is a variable name the
  source already contains, never the literal's payload.

Both directions are pinned at
`tests/test_credential_scanner_predicates.py::TestPlaceholderExemptionIsWholeValue`, for the
brace form, the angle form and three word entries: the pure value stays admitted, the same
form glued to a payload is reported. The refusal half was proven CONDITIONAL by mutation:
reverting the one word `fullmatch` back to `match` turns all six refusal cases red.

### Why the order inside the commit is not cosmetic

A commit that only narrowed would be strictly worse than the state it replaced: a scanner
that refuses `approve-` and admits `API_TOKEN = f"<real secret>"`. And the two are
mechanically coupled. `tests/test_decision_memory_capture.py:86` is
`GATE_TOKEN_SESSION_PREFIX = f"{_TEST_SCOPE}-"`, which passed only because of the `f`
prefix bypass. The moment the prefix is accepted, that line's captured value is
`{_TEST_SCOPE}-`, fourteen characters, and the line becomes unwritable unless the brace
exemption lands in the same change. Shipping the widening alone would have broken an
already-wired module.

### Why `NAMESPACE_PREFIX` is a separate object and not a `PLACEHOLDER` entry

`PLACEHOLDER` is a prefix-anchored list of placeholder WORDS, consulted with `match`. The
namespace test is a whole-value SHAPE test. Folding a `$`-anchored alternative into a
`re.match` word list would make the constant's name a lie and hide the anchoring difference
from the next reader. `fullmatch` is used rather than `match` with a trailing `$` because
`$` also matches before a trailing newline, and the whole value has to satisfy the shape
for the judgment to hold.

### Why the prefix class excludes `(` and `[`

Widening to those would match `API_TOKEN = os.environ.get("MY_SERVICE_TOKEN")` and
`API_TOKEN = cfg["APPLICATION_TOKEN"]`, where the captured value is an environment or key
NAME, and would turn every credential lookup done correctly into a finding.

### The vendor loop stays unconditional

Both exemptions are consulted only inside the identifier branch, against `m.group(1)` and
nothing else on the line. A value that is both namespace-shaped and vendor-shaped is still
reported: `SLACK_TOKEN = "xoxb-1234567890-"` satisfies `NAMESPACE_PREFIX` (lowercase,
hyphen, trailing separator) and also satisfies `xox[baprs]-[A-Za-z0-9-]{10,}`, and the
finding comes out of the vendor loop as `writ-crypto-scan/slack-token`. That case is
pinned at `TestVendorWinsOverShapeExemption`.

## The alternatives that were tried and are DISPROVEN

Both are recorded here so nobody re-derives them. Both numbers below were produced by
running the computation, not by reading the code.

### Entropy thresholding: the populations overlap in BOTH directions

The obvious idea is "exempt low-entropy values, flag high-entropy ones". Shannon entropy in
bits per character, measured:

| value                       | bits/char |
| --------------------------- | --------- |
| `password123456`            | 3.664     |
| `advance-from-complete-`    | 3.664     |
| `admin1234567`              | 3.585     |
| `correcthorsebatterystaple` | 3.364     |
| `correct-horse-battery-`    | 3.266     |
| `reviewpromote-`            | 3.182     |
| `phase3b-`                  | 3.000     |
| `approve-`                  | 2.750     |
| `evidence-`                 | 2.642     |
| `aaaaaaaa`                  | 0.000     |

`password123456`, a password that must be caught, and `advance-from-complete-`, a namespace
prefix that must be admitted, tie EXACTLY at 3.664. No threshold separates them at any
precision. In the other direction `aaaaaaaa` scores 0.000, below every single prefix in the
admitted set, so any threshold low enough to admit `evidence-` (2.642) admits `aaaaaaaa`
too. Entropy is not a discriminator on this population and must not be reintroduced.

### A name-suffix carve-out: it is the "rename the constant" evasion, generalized

The second idea is to exempt by the IDENTIFIER instead of the value: skip any constant
whose name ends `_PREFIX`, `_NAME` or `_FIELD`. That exempts

    DB_PASSWORD_PREFIX = "Tr0ub4dor&3xyz..."

which is a real password that no vendor pattern covers, and it hands every future writer a
one-word rename that turns the scanner off. Renaming the constant is one of the three
evasions this repo explicitly forbids; a suffix allowlist is that evasion with a rule
behind it. Rejected.

The other two forbidden narrowings, recorded for the same reason: shortening the value
below the eight-character floor, and adding a path or name allowlist so a specific file or
constant stops being scanned. Neither appears in this change.

## Why the accepted blind spot is the cheapest of the options

1. **No generator of real secrets produces that shape.** Measured, not reasoned: 200,000
   freshly generated samples of each of six realistic secret shapes (32-char hex,
   base64url, mixed-alphabet password, AWS access key id, AWS 40-char secret, uuid4),
   1,200,000 samples in total, produced ZERO `NAMESPACE_PREFIX` matches. A trailing bare
   separator is an artifact of hand-writing a namespace, not of generating a key.
2. **The protection given up is barely-measured protection.** Across this tree the
   identifier predicate produces five findings. Four are false positives (three shell files
   assigning a `/tmp` path, one test sentinel); one, `writ.toml.example:8`, is the scanner
   working. None of the five is touched by `NAMESPACE_PREFIX`, so the exemption gives up
   nothing this tree currently catches. The false positives it removes, by contrast, were
   measured and were blocking work.
3. **The vendor patterns are untouched, which is NOT the same as "every vendor-shaped value
   is still caught".** All seven patterns are unchanged and are pinned by
   `tests/test_credential_scanner_predicates.py::TestVendorPatternsPinned`. But each one
   requires the closing quote immediately after its alphanumeric run, so a vendor-shaped
   value with a trailing separator (`"sk_live_aaaaaaaaaaaaaaaaaaaa-"`) does not match the
   vendor pattern at all, and the identifier predicate that used to catch it anyway now
   exempts it. See "The instance of it worth naming" above. Claim 3 covers values whose
   tail is what the vendor regex expects, and nothing more.
4. **The exemption is conditional and narrow.** `Approve-`, `APPROVE-`, `approve_x`,
   `approve.name-` and `approve- ` (trailing space) are each still reported; each differs
   from a genuine namespace prefix in exactly one property, and each is pinned at
   `TestNamespaceExemptionIsConditional`.

## What would close the blind spot later, and the precondition it needs

Not a cleverer value regex. Guessing a secret's shape is what produced this blind spot in
the first place, and a second guess produces a second one. The positive form is a rule that
a credential-named constant must be assigned FROM `os.environ`, a secrets loader or a
config object, and never from a string literal of any shape. That predicate does not depend
on what the value looks like, so it has no value-shaped hole.

Independently, and this is the precondition any such detector needs: the scanner is not run
at commit time, in CI, or in a pre-commit hook. It runs only through
`hooks/scripts/pre-validate-file.sh`, `hooks/scripts/validate-file.sh` and
`bin/audit-region.sh`, all of which are per-file and per-edit, so nothing sweeps the tree on
a schedule. A file that already contains a secret is never re-examined. That gap is
pre-existing and was explicitly out of scope for this cycle; a value-independent rule is
worth little until something runs it over the whole tree.

## Consequences

- `tests/test_no_tool_prereqs.py` and `tests/test_phase3b_approval_rewrap.py` can now
  declare `GATE_TOKEN_SESSION_PREFIX` at module level, which is what
  `tests/_gate_token_leak.py` reads to scope its sweep. Both were wired in this commit, so
  the strict xfail that existed for this day was deleted rather than left carrying a reason
  that had stopped being true.
- `tests/_gate_token_leak.py` may now bind `writ-gate-token-` to a module constant; that
  refactor is deliberately not done here, because `tests/conftest.py` imports that module
  at conftest import time and its fixtures run at every module boundary in every suite
  chunk.
- The tree-wide `SEC-CRYPTO-KEY-001` population, measured over `git ls-files` before and
  after, goes from five findings to five. `bin/lib/test_paths.py:150` is removed (the brace
  template this cycle exempts) and `tests/test_doctor.py:691` is added (a value the
  anchoring hole was laundering; see below). The four that do not move are:

      hooks/scripts/auto-approve-gate.sh:321     GATE_TOKEN_FILE="/tmp/writ-gate-token-${SESSION_ID}"
      hooks/scripts/auto-approve-gate.sh:416     GATE_TOKEN_FILE="/tmp/writ-gate-token-${SESSION_ID}"
      hooks/scripts/writ-state-write-gate.sh:34  TOKEN_PREFIX="/tmp/writ-gate-token-"
      writ.toml.example:8                        password = "writdevpass"

  THREE of those are shell files and the fourth is NOT: `writ.toml.example:8` is a TOML
  example config carrying a local-dev Neo4j password. All four are untouched by this
  change, and for the same reason in the first three cases (a value containing `/`
  satisfies neither exemption) and a different one in the fourth (`writdevpass` is
  lowercase but does not end in a separator, so `NAMESPACE_PREFIX` does not reach it). The
  first three are false positives of the same family; the fourth is the scanner working.
  All four are out of scope for this cycle.

- **One finding was uncovered, not created**: `tests/test_doctor.py:691`,
  `SENTINEL_TOKEN = "test-token-DO-NOT-LOG-abc123"`. It was admitted before this change
  because it starts with the word `test` and `PLACEHOLDER` only checked the start; with
  `PLACEHOLDER` anchored at both ends the uppercase `DO-NOT-LOG` falls outside TAIL and the
  value is reported. In substance it is a deliberate test sentinel, not a credential, so
  this is a false positive. It is left STANDING and named here rather than exempted,
  because every way to silence it is one of the three evasions this cycle forbids (rename
  the constant, change the value, allowlist the path) and because `tests/test_doctor.py` is
  not in this plan's Files section. It does not block edits to that file: the pre-write
  hook filters findings that are already present in the base version
  (`bin/lib/filter-new-findings.py`), and this one is. Whoever next owns that module should
  decide whether the sentinel should be lowercase.
