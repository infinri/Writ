# Support Writ

Writ is free and open source. If it has saved you time, caught a mistake before it shipped, or given you a clearer picture of how coding agents behave, you can help keep it going.

**[☕ Buy me a coffee](https://buymeacoffee.com/infinri)**

Even a single coffee helps. No subscription is expected.

## Who's behind it

I'm Lucio Saldivar, an engineer. Writ is independently built and maintained by me, in my own time: the runtime, the rules, the tests, and the research and documentation around them.

## What you're supporting

Writ does three things for Claude Code:

- **Enforcement.** Checks that run when a tool is called and can block an action, ask you to confirm, or report a finding.
- **Context.** It delivers the rules, methodology, and past decisions that fit the work happening now, instead of everything at once.
- **Continuity.** It keeps project memory and decision history across sessions, with best-effort handoffs when context is compacted.

Support also keeps the research and documentation going. Much of it, such as the architecture notes, the measurements, and the write-ups of how agents actually behave, is useful on its own, even if you never install Writ.

## What contributions would help fund

I currently cover Writ's costs myself. Contributions would help fund maintenance, evaluation and research: the Claude Code subscription used to build and test it, and the experiments below.

With more support, I would like to:

- **Measure instead of guess.** Run controlled API experiments to evaluate retrieval, rule delivery, and whether those changes improve agent behavior.
- **Develop stronger governance and sandboxing.** Build isolated environments to test unauthorized actions, destructive commands, sensitive-data exposure, and attempts to bypass approvals. The goal is to find where controls fail and develop stronger enforcement and containment before those failures affect real projects. This is future research, not a shipped feature.
- **Explore local models.** Investigate how Writ's governance, context delivery, and continuity could work with models you run yourself.

Writ already enforces selected workflow boundaries through hooks. Stronger security containment is a research direction I want to develop alongside those controls.

These are goals that additional funding would help me pursue. Contributions support the project; they do not purchase dedicated support, priority fixes, or specific features.

## Ways to help

Money is one way to help, and not the only one:

- **Financial support:** [buymeacoffee.com/infinri](https://buymeacoffee.com/infinri).
- **Reproducible bug reports:** a transcript showing a rule that did not hold is the most valuable thing you can send. Report anything exploitable privately through [GitHub Security Advisories](https://github.com/infinri/Writ/security/advisories/new).
- **Contributions:** rules, fixes, and documentation. See [`CONTRIBUTING.md`](CONTRIBUTING.md).
- **Sharing examples:** if Writ helped in a real project, telling others how is a real help.

## Thank you

Thank you for reading this far, and for using Writ. Any coffee, bug report or kind word would go straight into making it better.

**[☕ Buy me a coffee](https://buymeacoffee.com/infinri)**
