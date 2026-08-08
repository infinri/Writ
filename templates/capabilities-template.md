# Capabilities: <the same one-line change name used in plan.md>

<Testable behaviors for `plan.md`. This file is the plan's ## Capabilities section
standing on its own so the test writer can work from it directly; keep the two lists
identical, item for item.

Every item is one OBSERVABLE behavior a test can assert. Boxes start UNCHECKED and are
ticked off only after the implementation proves them. Mark an item `(operational)` when
it can only be verified by running the thing rather than by a test, so nobody looks for
a test that was never meant to exist.>

- [ ] <observable behavior, stated so a test can assert it>
- [ ] <the failure/fallback path: what happens when the dependency is down or absent>
- [ ] <a boundary case: empty, missing, or malformed input>
- [ ] <operational, if any: what must be run by hand once the code lands>
