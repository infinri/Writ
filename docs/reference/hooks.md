<!-- GENERATED FILE - do not edit. Source: hooks/hooks.json. Regenerate with `make docs` (scripts/render-docs.py). -->


# Hook registration matrix

44 registrations across 12 events wiring 40 scripts under `hooks/scripts/`, generated from `hooks/hooks.json` (the single source; `templates/settings.json` is rendered from the same file). `writ-statusline.sh` is wired through the settings `statusLine` channel, not a hook event. Behavior and blocking semantics: `HANDBOOK.md` section 14.

## SessionStart

| Matcher | Script |
|---|---|
| `(all)` | `writ-blackbox-capture.sh` |
| `(all)` | `session-start-bootstrap.sh` |

## UserPromptSubmit

| Matcher | Script |
|---|---|
| `(all)` | `writ-manual-test-grant.sh` |
| `(all)` | `auto-approve-gate.sh` |
| `(all)` | `writ-rag-inject.sh` |

## SubagentStart

| Matcher | Script |
|---|---|
| `(all)` | `writ-subagent-start.sh` |

## SubagentStop

| Matcher | Script |
|---|---|
| `(all)` | `writ-blackbox-capture.sh` |
| `(all)` | `writ-subagent-stop.sh` |

## Stop

| Matcher | Script |
|---|---|
| `(all)` | `friction-logger.sh` |
| `(all)` | `enforce-violations.sh` |
| `(all)` | `writ-verify-before-claim.sh` |
| `(all)` | `writ-run-pending-tests.sh` |
| `(all)` | `writ-comms-output-gate.sh` |

## PostToolUseFailure

| Matcher | Script |
|---|---|
| `.*` | `writ-blackbox-capture.sh` |

## PreCompact

| Matcher | Script |
|---|---|
| `(all)` | `writ-precompact.sh` |

## PostCompact

| Matcher | Script |
|---|---|
| `(all)` | `writ-blackbox-capture.sh` |
| `(all)` | `writ-postcompact.sh` |

## SessionEnd

| Matcher | Script |
|---|---|
| `(all)` | `writ-session-end.sh` |
| `(all)` | `writ-pressure-audit.sh` |

## CwdChanged

| Matcher | Script |
|---|---|
| `(all)` | `writ-blackbox-capture.sh` |
| `(all)` | `writ-cwd-changed.sh` |

## PreToolUse

| Matcher | Script |
|---|---|
| `ExitPlanMode` | `validate-exit-plan.sh` |
| `Read` | `writ-read-junk-gate.sh` |
| `Read` | `writ-read-rag.sh` |
| `Grep|Read|Glob` | `writ-debug-code-gate.sh` |
| `Write|Edit|NotebookEdit` | `writ-state-write-gate.sh` |
| `Write|Edit|NotebookEdit` | `writ-pre-write-dispatch.sh` |
| `Write|Edit` | `pre-validate-file.sh` |
| `Task` | `writ-dispatch-discipline.sh` |
| `Bash` | `writ-worktree-safety.sh` |
| `Bash` | `writ-bash-write-gate.sh` |
| `Write` | `validate-test-file.sh` |
| `Write` | `validate-design-doc.sh` |
| `Write` | `writ-memory-policy-guard.sh` |

## PostToolUse

| Matcher | Script |
|---|---|
| `Bash` | `inject-tier-workflow.sh` |
| `WebFetch|WebSearch` | `writ-web-capture.sh` |
| `Write|Edit` | `validate-file.sh` |
| `Write|Edit` | `writ-bible-authoring-push.sh` |
| `Write|Edit` | `validate-handoff.sh` |
| `Write|Edit` | `validate-rules.sh` |
| `Write|Edit|NotebookEdit` | `writ-posttool-rag.sh` |
| `Write` | `writ-quality-judge.sh` |
| `Write|Edit` | `writ-mark-pending-test.sh` |
| `Write|Edit` | `writ-memory-capture.sh` |
