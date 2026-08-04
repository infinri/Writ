# Plan: <one line naming the change, e.g. "automatic memory-to-graph mirroring">

<One short paragraph: the user's directive in their terms, including anything they
amended at approval. Written so a reviewer can tell whether the plan below answers
what was actually asked.>

## Files

<One bullet per file, in the exact grammar below. Nothing else in this section is a
bullet that starts with a backtick-path, because the gate checks EVERY such bullet:

    - `path/from/repo/root.py` (change_type) -- why this file must change

Three parts, all required:
  * `path` in backticks, relative to the repo root.
  * (change_type) in parentheses: create, modify, or delete.
  * a reason after ` -- `: what this file does for the feature. A path with no
    reason is the single most common gate rejection, because a reason-less line
    leaves the commit harvester nothing to record.

The bold variant is equally accepted: a bold change type first, then the backtick path,
then the same ` -- reason`. Every file the implementation touches goes here; a file
written but never planned shows up in the commit audit as unplanned.>

- `path/to/first/file.py` (create) -- what this file is for and why it is needed
- `path/to/second/file.py` (modify) -- what changes in it and why
- `tests/test_the_feature.py` (create) -- pins the behaviors listed under ## Capabilities

## Analysis

<The design, in prose. What the change does and why this shape and not another:
contracts and signatures, integration points, the data/graph shape, failure and
fallback behavior, and anything deliberately left OUT of scope. Name the existing
pattern being reused instead of inventing a parallel one. This section is what the
user reads to decide whether to approve, so state the load-bearing decisions and the
tradeoffs plainly rather than restating the file list.>

## Rules Applied

<One bullet per rule that actually shaped a decision above, with a sentence on HOW it
applied -- not a list of every id that scrolled past.

Cite ONLY ids that appeared in an injected `--- WRIT RULES ---` block this session.
The gate compares every cited id against what the session actually loaded, so an id
recalled from memory or invented to look thorough is rejected as hallucinated and
SPENDS the user's approval. Abstraction ids from a `[ABSTRACT: ...]` block are
injected ids too, so they are citable the same way.

Shape:

    - **<RULE-ID>** -- how the rule shaped a decision in this plan

If no rules were injected in your context at all, this section must instead read
exactly: No matching rules.>

- **<RULE-ID>** -- the decision above that this rule dictated, in one sentence
- **<RULE-ID>** -- likewise

## Capabilities

<One checkbox per OBSERVABLE behavior, each one a thing a test can assert. Boxes start
UNCHECKED: the gate rejects a plan whose boxes arrive already ticked, because a
pre-ticked capability claims verification that has not happened. They are ticked off
after the implementation proves them. Include the boundary cases (empty input, missing
input, the failure path), not just the happy path. Copy these same items into
capabilities.md.>

- [ ] <observable behavior, stated so a test can assert it>
- [ ] <the failure/fallback path: what happens when the dependency is down or absent>
- [ ] <a boundary case: empty, missing, or malformed input>
