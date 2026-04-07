"""LLM client for rule compliance analysis.

Constructs prompts from code + rules + phase context, calls the Anthropic API,
and parses structured JSON responses into Finding objects.

Per invariant 5: async with 10s timeout. Failures produce status="uncertain".
Per invariant 6: deterministic prompts (temperature=0).
Per invariant 7: strict JSON parsing. No free-form text in output.
Per invariant 8: max 10 rules per call.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from writ.analysis import Finding

logger = logging.getLogger(__name__)

MAX_RULES_PER_CALL = 10
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
PLANNING_MODEL = "claude-sonnet-4-6-20250514"
LLM_TIMEOUT = 10.0

_PROMPT_TEMPLATE_WITH_FINDINGS = """You are a code compliance checker. Analyze the following code against the provided rules.

Pattern matching has already found potential violations (listed below). Verify each and check for additional violations the patterns may have missed.

## Code
File: {file_path}
Phase: {phase}

```
{code}
```

## Pattern findings to verify
{pattern_findings_text}

## Rules to check
{rules_text}

## Instructions
For each rule, determine if the code violates it. Return a JSON array of findings:
```json
[
  {{"rule_id": "RULE-ID", "status": "violated|pass|uncertain", "line": 42, "evidence": "what was found", "suggestion": "how to fix"}}
]
```

Return ONLY the JSON array. No other text."""

_PROMPT_TEMPLATE_NO_FINDINGS = """You are a code compliance checker. Analyze the following code against the provided rules.

Pattern matching found no violations. Analyze the code from scratch against all provided rules.

## Code
File: {file_path}
Phase: {phase}

```
{code}
```

## Rules to check
{rules_text}

## Instructions
For each rule, determine if the code violates it. Return a JSON array of findings:
```json
[
  {{"rule_id": "RULE-ID", "status": "violated|pass|uncertain", "line": 42, "evidence": "what was found", "suggestion": "how to fix"}}
]
```

Return ONLY the JSON array. No other text."""


def _format_rules(rules: list[dict]) -> str:
    """Format rules for the LLM prompt."""
    parts = []
    for r in rules[:MAX_RULES_PER_CALL]:
        parts.append(f"[{r.get('rule_id', '?')}]")
        trigger = r.get("trigger", "")
        if trigger:
            parts.append(f"WHEN: {trigger}")
        statement = r.get("statement", "")
        if statement:
            parts.append(f"RULE: {statement}")
        violation = r.get("violation", "")
        if violation:
            parts.append(f"VIOLATION: {violation}")
        pass_example = r.get("pass_example", "")
        if pass_example:
            parts.append(f"CORRECT: {pass_example}")
        parts.append("")
    return "\n".join(parts)


def _format_pattern_findings(findings: list[Finding]) -> str:
    """Format pattern findings for the LLM prompt."""
    if not findings:
        return ""
    parts = []
    for f in findings:
        parts.append(
            f"- {f.rule_id} at line {f.line}: {f.evidence} (confidence: {f.confidence})"
        )
    return "\n".join(parts)


def build_prompt(
    code: str,
    file_path: str,
    phase: str,
    rules: list[dict],
    pattern_findings: list[Finding],
) -> str:
    """Construct the LLM prompt. Deterministic given inputs."""
    rules_text = _format_rules(rules)

    if pattern_findings:
        return _PROMPT_TEMPLATE_WITH_FINDINGS.format(
            file_path=file_path,
            phase=phase,
            code=code[:8000],  # cap code length for prompt budget
            pattern_findings_text=_format_pattern_findings(pattern_findings),
            rules_text=rules_text,
        )
    return _PROMPT_TEMPLATE_NO_FINDINGS.format(
        file_path=file_path,
        phase=phase,
        code=code[:8000],
        rules_text=rules_text,
    )


def select_model(phase: str, config_model: str | None = None) -> str:
    """Select LLM model based on phase. Planning uses Sonnet, others use Haiku."""
    if config_model:
        return config_model
    if phase == "planning":
        return PLANNING_MODEL
    return DEFAULT_MODEL


def parse_llm_response(raw: str) -> list[dict[str, Any]]:
    """Parse LLM response as JSON array. Raises ValueError on failure."""
    text = raw.strip()
    # Handle markdown code fences
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.startswith("```")]
        text = "\n".join(lines).strip()

    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError(f"Expected JSON array, got {type(data).__name__}")
    return data


def findings_from_llm_response(raw_findings: list[dict[str, Any]]) -> list[Finding]:
    """Convert parsed LLM response dicts to Finding objects."""
    results = []
    for item in raw_findings:
        results.append(Finding(
            rule_id=item.get("rule_id", "UNKNOWN"),
            source="llm",
            status=item.get("status", "uncertain"),
            line=item.get("line"),
            confidence=None,
            evidence=item.get("evidence", ""),
            suggestion=item.get("suggestion", ""),
        ))
    return results


class LlmAnalyzer:
    """Async LLM client for rule compliance analysis."""

    def __init__(
        self,
        api_key: str | None = None,
        timeout: float = LLM_TIMEOUT,
        config_model: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._config_model = config_model
        self._client = None

    def _get_client(self):
        """Lazy-init the Anthropic async client."""
        if self._client is None:
            try:
                import anthropic
                self._client = anthropic.AsyncAnthropic(
                    api_key=self._api_key,
                    timeout=self._timeout,
                )
            except ImportError:
                logger.warning("anthropic SDK not installed; LLM analysis unavailable")
                return None
        return self._client

    async def analyze(
        self,
        code: str,
        rules: list[dict],
        phase: str,
        file_path: str = "",
        pattern_findings: list[Finding] | None = None,
    ) -> list[Finding]:
        """Run LLM analysis on code against rules.

        Returns findings. On timeout/error, returns uncertain findings.
        """
        if not rules:
            return []

        client = self._get_client()
        if client is None:
            return [Finding(
                rule_id=r.get("rule_id", "UNKNOWN"),
                source="llm",
                status="uncertain",
                evidence="LLM analysis unavailable (SDK not installed)",
                suggestion="",
            ) for r in rules[:MAX_RULES_PER_CALL]]

        prompt = build_prompt(
            code=code,
            file_path=file_path,
            phase=phase,
            rules=rules[:MAX_RULES_PER_CALL],
            pattern_findings=pattern_findings or [],
        )

        model = select_model(phase, self._config_model)

        try:
            response = await client.messages.create(
                model=model,
                max_tokens=2000,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )
            raw_text = response.content[0].text
            parsed = parse_llm_response(raw_text)
            return findings_from_llm_response(parsed)

        except Exception as e:
            logger.warning("LLM analysis failed: %s", e)
            return [Finding(
                rule_id=r.get("rule_id", "UNKNOWN"),
                source="llm",
                status="uncertain",
                evidence=f"LLM analysis failed: {type(e).__name__}",
                suggestion="",
            ) for r in rules[:MAX_RULES_PER_CALL]]
