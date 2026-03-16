# Core Coding Principles

These principles apply globally to all code in this codebase. They are not enforceable rules (those live in `bible/` and `enforcement/`). They are judgment guidelines that the AI applies when no specific rule covers the situation.

## DRY

- **Before writing new code**: search the existing codebase for similar implementations
- **When encountering duplicate code**: consolidate it immediately
- Common functionality must exist in one location only

## SOLID

- **SRP**: if describing a class requires "and", it likely violates SRP. Split it.
- **Open/Closed**: extend behavior through new classes, not by modifying existing ones (see ARCH-EXT-001)
- **Liskov**: subclasses must be substitutable for parent classes without altering correctness
- **ISP**: split large interfaces into smaller, role-specific ones
- **DI**: inject dependencies through constructors; use interfaces/abstractions (see ARCH-DI-001)

## KISS

- If a solution feels complex, step back and find a simpler approach
- Do not add complexity for hypothetical future needs
- Three similar lines of code is better than a premature abstraction

## Composition Over Inheritance

- Before creating inheritance hierarchies, consider if composition achieves the same goal with less coupling
- Prefer "has-a" over "is-a" relationships

## How these relate to the Bible

These principles are assumed knowledge. The Bible's enforceable rules (ARCH-*, PHP-*, FW-M2-*, etc.) are the specific, binary-decision implementations of these principles for this codebase. When a Bible rule exists for a situation, follow the rule. When no rule exists, apply these principles.
