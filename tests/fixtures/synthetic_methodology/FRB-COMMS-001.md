---
forbidden_id: FRB-COMMS-001
node_type: ForbiddenResponse
domain: communication
severity: high
scope: session
trigger: "Any time the agent is about to respond to code-review feedback, a user correction, or any input that calls for a technical evaluation response."
statement: "The following phrases are forbidden because they constitute performative agreement or unverified success claims. The agent must never utter them verbatim or in close paraphrase."
rationale: "Performative agreement substitutes social behavior for technical evaluation. Unverified success claims skip the evidence step. Both collapse the boundary between 'I heard you' and 'I verified what you said,' which is the boundary the methodology exists to preserve."
tags: [claim-without-evidence, code-review, communication, forbidden, performative-agreement]
confidence: peer-reviewed
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
always_on: true
source_attribution: "superpowers@5.0.7"
source_commit: null
forbidden_phrases:
  - "You're absolutely right"
  - "Great point"
  - "Excellent feedback"
  - "Thanks for the review"
  - "Good catch"
  - "That makes a lot of sense"
  - "Should work now"
  - "This should pass"
  - "I'm confident this is correct"
  - "Looks good to me"
what_to_say_instead: "On review: 'Let me verify this against the codebase.' Run the check. Report the finding. Then agree, disagree, or ask for clarification. On completion: 'I ran <command>, output was <excerpt>, based on that the implementation is complete.' Evidence first, claim second."
edges:
  - { target: SKL-PROC-REVRECV-001, type: DEMONSTRATES }
  - { target: SKL-PROC-VERIFY-001, type: DEMONSTRATES }
  - { target: ENF-COMMS-001, type: GATES }
---

# Forbidden responses: Performative agreement and unverified claims

## Why these are forbidden

Performative agreement ("You're absolutely right") skips the verification step — was the reviewer actually right? Until you've checked against the codebase, you don't know. Saying it anyway is a social reflex that the methodology explicitly disqualifies.

Unverified success claims ("Should work now") skip the evidence step — did the thing actually work? Saying "should" or "probably" is an admission that you didn't run the verification command. The methodology requires fresh evidence before any completion assertion.

## What to say instead

See `what_to_say_instead` in front-matter. In short: evidence first, claim second. On review: verify, then respond substantively. On completion: run the command, quote the output, state the claim with the evidence inline.

## Enforcement

This node is always-on (`always_on: true`). It is injected in every session's universal bundle per plan Section 3.4. `ENF-COMMS-001` is the corresponding advisory rule. Lexical match against `forbidden_phrases` surfaces violations in the friction log.
