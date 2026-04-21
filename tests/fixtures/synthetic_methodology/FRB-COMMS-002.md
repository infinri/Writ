---
forbidden_id: FRB-COMMS-002
node_type: ForbiddenResponse
domain: communication
severity: critical
scope: session
trigger: "Any time the agent is about to state that something works, passes, is complete, or succeeds."
statement: "Unverified success claim phrases are forbidden. They substitute confidence for evidence. The agent must never utter them without inline verification output."
rationale: "Claims without evidence erode trust with users. One false 'done' requires three recoveries. The discipline makes confidence-as-evidence syntactically impossible."
tags: [claim-without-evidence, communication, completion, forbidden, verification]
confidence: battle-tested
authority: human
last_validated: 2026-04-21
staleness_window: 365
evidence: doc:superpowers
always_on: true
source_attribution: "superpowers@5.0.7"
source_commit: null
forbidden_phrases:
  - "Should work now"
  - "This should pass"
  - "I'm confident this is correct"
  - "Looks good to me"
  - "Done!"
  - "Perfect!"
  - "Great!"
  - "All set"
what_to_say_instead: "State: 'I ran <command>, the output was <excerpt>, based on which the claim holds.' Evidence inline with the claim, not separable from it."
edges:
  - { target: SKL-PROC-VERIFY-001, type: DEMONSTRATES }
  - { target: ENF-PROC-VERIFY-001, type: GATES }
---

# Forbidden: Unverified success claims

## Why these are forbidden

"Should", "probably", "looks good" — each is an admission that verification did not happen. The discipline inverts: state the evidence first, then the claim derives from it. Claims that cannot trace to evidence are unstated.

## What to say instead

See `what_to_say_instead` in front-matter.
