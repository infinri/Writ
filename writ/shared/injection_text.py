"""The one-line rule pointer every collapsed render uses (always-on block, methodology floor).

Lowest layer on purpose: writ.retrieval and writ.session both render it and neither may
import the other.
"""


def pointer_line(rule_id: str, trigger: str) -> str:
    return f"[{rule_id}] WHEN: {(trigger or '').strip()}"
