# Instructions and enforcement

An instruction and an enforcement point are different primitives, and only one of them can refuse. This page is the argument behind that sentence: where Writ sits against the other ways of giving a coding agent rules, what Agent Skills are and are not, and why a workflow does not become mandatory because the instructions describing it say that it is.

## Where Writ sits against other approaches

These are approaches to coding-agent governance, not products. Each is a reasonable way to give an agent rules, and each runs into a structural limit that shaped Writ's design.

* **Rules stuffed into the context.** Cost grows with the rulebook and the signal gets buried in it. Writ retrieves instead, so the per-turn cost stays roughly flat as the rulebook grows.
* **Static skill files.** Point-in-time bundles with no relationships between them. Writ keeps rules in a knowledge graph with typed links, so a matched rule can pull in its neighbors, including ones that share no words with what you asked.
* **Per-repo rules as code.** Nothing propagates between repositories, and each copy drifts on its own. Writ keeps one shared graph with per-project isolation.
* **An AI validator on every diff.** A model call per change, and the same code can be judged differently twice. Writ's gates are code, so an ordinary turn costs no model call at all.
* **Rules in the system prompt.** Editing the rulebook changes the prefix every request shares. Writ injects per turn instead, and keeps rule ordering stable so the shared prefix does not churn.

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

## Superpowers and the definition of mandatory

Superpowers is built on the exact architectural contradiction discussed in [Why not just use Agent Skills?](#why-not-just-use-agent-skills): it calls workflows mandatory while leaving the final enforcement authority inside the model. The credit for the methodology itself sits in the README's acknowledgements, where it belongs; this is the disagreement, separated out.

Its own [porting guide](https://github.com/obra/superpowers/blob/main/docs/porting-to-a-new-harness.md) makes the dependency explicit. The model must receive a bootstrap, discover the relevant skill, load its instructions, retain them through the session, and follow them. The guide calls that bootstrap the entire integration, says the skills are inert without it, and treats automatic session-start injection as a hard requirement.

That is a useful methodology. It is not the definition of mandatory Writ accepts.

If planning, TDD, review, and verification matter enough to be mandatory, selected checkpoints should be able to refuse the action without asking the model whether it remembers the rule.

## The same argument, from the user's side

You have probably watched this happen. You tell the AI how you want things done: write the test first, follow the pattern already in the file, ask before touching the database. It agrees. It works that way for a while. Then somewhere in a long session it stops, and nothing announces that it stopped. You find out in review, or you find out in production, or you do not find out.

That is not the AI being careless. An instruction in context is still an instruction: it can be compacted away, diluted by newer context, misapplied, or simply ignored, and nothing about putting a rule in the prompt makes violating it mechanically impossible. Instructions and enforcement are different primitives, and only one of them can refuse.
