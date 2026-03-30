# plan.md -- Structured Capabilities Format

Every module's `plan.md` must contain a machine-readable capabilities block.
This block is what `bin/verify-matrix.sh` parses to produce the completion matrix.
Without it, ENF-GATE-FINAL verification is prose-based and unreliable.

---

## Format

Use a fenced code block with the `capabilities` language tag:

````markdown
```capabilities
- id: CAP-001
  phase: A
  description: REST endpoint for order accrual
  files:
    - Api/AccrualInterface.php
    - Model/AccrualService.php

- id: CAP-002
  phase: B
  description: Accrual balance is non-negative at all persistence boundaries
  files:
    - Model/AccrualService.php
    - Model/ResourceModel/Accrual.php

- id: CAP-003
  phase: C
  description: Plugin on OrderManagement to trigger accrual after invoice
  files:
    - Plugin/Sales/OrderManagementPlugin.php
    - etc/di.xml

- id: CAP-004
  phase: D
  description: Async accrual processing via message queue
  files:
    - Model/Consumer/AccrualConsumer.php
    - etc/queue_consumer.xml
    - etc/queue_topology.xml
    - etc/communication.xml
```
````

---

## Field Reference

| Field | Required | Description |
|---|---|---|
| `id` | Yes | Unique capability identifier. Convention: `CAP-NNN`. |
| `phase` | Yes | Which phase declared this: `A`, `B`, `C`, or `D`. |
| `description` | Yes | What the capability does -- one line. |
| `files` | Yes | Files that must exist on disk for this capability to be PRESENT. Paths are relative to the module root (where plan.md lives). |

---

## Rules

1. **Every Phase A-D declaration maps to at least one capability.** If Phase B declares an invariant, there must be a `CAP-*` entry with `phase: B` and the files that enforce it.

2. **File paths are relative to the module directory** (the directory containing plan.md). `verify-matrix.sh` resolves them from there.

3. **A capability is PRESENT when all its files exist on disk.** If any file is MISSING, the entire capability is MISSING.

4. **No capability may have an empty files list.** That produces a `NO_FILES_DECLARED` status -- a hard failure.

5. **Capabilities are additive per slice.** When a slice adds files, the plan.md capabilities block should already list them. Slices don't modify the block -- it's written during planning, before implementation.

---

## Alternative: YAML Front-Matter

If your plan.md uses YAML front-matter, you can place capabilities there instead:

```markdown
---
capabilities:
  - id: CAP-001
    phase: A
    description: REST endpoint for order accrual
    files:
      - Api/AccrualInterface.php
      - Model/AccrualService.php
---

# Module Plan

(rest of plan.md content...)
```

`verify-matrix.sh` checks for the fenced block first, then falls back to front-matter.

---

## Example Output from verify-matrix.sh

```json
{
  "plan_path": "app/code/Vendor/Accrual/plan.md",
  "project_root": "/home/user/project",
  "matrix": [
    {
      "id": "CAP-001",
      "phase": "A",
      "description": "REST endpoint for order accrual",
      "status": "PRESENT",
      "files": {
        "Api/AccrualInterface.php": "PRESENT",
        "Model/AccrualService.php": "PRESENT"
      }
    },
    {
      "id": "CAP-004",
      "phase": "D",
      "description": "Async accrual processing via message queue",
      "status": "MISSING",
      "files": {
        "Model/Consumer/AccrualConsumer.php": "PRESENT",
        "etc/queue_consumer.xml": "MISSING",
        "etc/queue_topology.xml": "MISSING",
        "etc/communication.xml": "MISSING"
      }
    }
  ],
  "missing": [
    { "id": "CAP-004", "...": "..." }
  ],
  "total": 4,
  "present": 3,
  "complete": false
}
```
