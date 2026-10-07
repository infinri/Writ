"""Program item 5 (workstream D): the fifth UserPromptSubmit hook, writ-inject-documents.sh.

The documents hook is the recall hook's shape (docs/adr/ADR-prompt-injection-split.md,
docs/adr/ADR-document-retrieval.md): it runs the shared body
`writ_prompt_section_main documents writ-inject-documents`, asks POST /prompt-bundle for
sections=["documents"], and prints only the response's `documents_block`. Unlike recall it
is not gated to the master session (sub-agents get documents) and it keeps the default
minimum-prompt-length gate.

Modeled on tests/test_rag_inject_recall.py: source-shape guards plus subprocess runs against
the loopback StubDaemon in tests/_stub_daemon.py. No test here touches a graph. The
server-side rendering is covered by tests/test_prompt_injection_ceiling.py; registration,
counts and the five-hook stub matrix by tests/test_prompt_hooks_split.py.

Run: .venv/bin/python -m pytest tests/test_inject_documents_hook.py

Capability map (plan capabilities (D) 27 and 28):
  [hook-docs-1]  registered after the recall hook in hooks.json and templates/settings.json
  [hook-docs-2]  exists, executable (100755), shared-body call, shared-body field case
  [hook-docs-3]  prints only documents_block, one request naming only the documents section
  [hook-docs-4]  prompts under the minimum length print nothing and send nothing
  [hook-docs-5]  runs for sub-agents
  [hook-docs-6]  output never exceeds 9,500 characters, even for a 20,000 character field
  [hook-docs-7]  fail-open: unreachable daemon, error response and empty block print nothing
"""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest

from tests._stub_daemon import STUB_HOST, StubDaemon

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "scripts" / "writ-inject-documents.sh"
RECALL_HOOK = REPO / "hooks" / "scripts" / "writ-inject-recall.sh"
SECTION_LIB = REPO / "bin" / "lib" / "writ-prompt-section.sh"
HOOKS_JSON = REPO / "hooks" / "hooks.json"
SETTINGS_TEMPLATE = REPO / "templates" / "settings.json"

CEILING = 9500
LONG_PROMPT = "how does the retrieval budget get split between the injection sections"
DOCS_BLOCK = ("--- WRIT DOCUMENTS (1 chunks) ---\n## Retrieval ADR (docs/adr/ADR-x.md)\n"
              "[docs/adr/ADR-x.md #0001] Retrieval ADR > Budget sim=0.612\n"
              "The documents section has its own 2,700 token budget.\n"
              "--- END WRIT DOCUMENTS ---")


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""


def _bundle(**fields) -> dict:
    body = {"error": False, "skipped": False, "always_on_block": "", "rules_text": "",
            "methodology_block": "", "recall_block": "", "documents_block": "",
            "nudge": "", "nudge_text": "", "broad_meta": None, "ao_meta": None,
            "method_meta": None}
    body.update(fields)
    return body


def _envelope(sid: str, *, prompt: str = LONG_PROMPT, agent_id: str = "") -> str:
    payload = {"session_id": sid, "hook_event_name": "UserPromptSubmit", "prompt": prompt}
    if agent_id:
        payload["agent_id"] = agent_id
    return json.dumps(payload)


def _run(tmp_path: Path, bundle: dict | int | None, *, prompt: str = LONG_PROMPT,
         agent_id: str = "", unreachable: bool = False):
    """Run the hook against a stub (or an unreachable port); return (proc, requests)."""
    sid = f"docs-hook-{uuid.uuid4().hex[:8]}"
    cache_dir = tmp_path / "writ-cache"
    cache_dir.mkdir(exist_ok=True)
    env = os.environ.copy()
    env.pop("WRIT_SOCKET", None)
    env.update({"WRIT_CACHE_DIR": str(cache_dir), "WRIT_NO_AUTOSTART": "1", "WRIT_HOST": STUB_HOST})
    if agent_id:
        env["AGENT_ID"] = agent_id
    if unreachable:
        env["WRIT_PORT"] = "19999"
        proc = subprocess.run(["bash", str(HOOK)], input=_envelope(sid, prompt=prompt, agent_id=agent_id),
                              capture_output=True, text=True, cwd=str(REPO), env=env, timeout=20)
        return proc, []
    with StubDaemon(routes={("POST", "/prompt-bundle"): bundle}) as stub:
        env["WRIT_PORT"] = str(stub.port)
        proc = subprocess.run(["bash", str(HOOK)], input=_envelope(sid, prompt=prompt, agent_id=agent_id),
                              capture_output=True, text=True, cwd=str(REPO), env=env, timeout=20)
        requests = stub.matching("POST", "/prompt-bundle")
    return proc, requests


class TestSourceShape:
    def test_the_hook_exists_and_is_executable_100755(self):
        assert HOOK.is_file(), f"{HOOK} does not exist"
        assert os.access(HOOK, os.X_OK)
        assert HOOK.stat().st_mode & 0o777 in (0o755, 0o775)

    def test_the_hook_runs_the_shared_body_for_the_documents_section(self):
        src = _read(HOOK)
        assert "writ_prompt_section_main documents writ-inject-documents" in src
        assert src.startswith("#!/usr/bin/env bash")
        assert "exit 0" in src

    def test_the_hook_is_the_recall_hooks_shape_with_only_its_name_and_section_changed(self):
        documents = [ln for ln in _read(HOOK).splitlines() if not ln.lstrip().startswith("#")]
        recall = [ln.replace("recall", "documents") for ln in _read(RECALL_HOOK).splitlines()
                  if not ln.lstrip().startswith("#")]
        assert documents == recall

    def test_the_shared_body_maps_the_documents_section_to_documents_block(self):
        assert "documents) field=documents_block ;;" in _read(SECTION_LIB)

    def test_documents_is_not_gated_to_the_master_session(self):
        lib = _read(SECTION_LIB)
        gate_start = lib.index('case "$section" in\n        recall)')
        gate = lib[gate_start:lib.index("esac", gate_start)]
        assert "documents)" not in gate, "documents must fall in the default minimum-length branch"
        assert 'recall) [ -z "$agent" ] || return 0' in gate

    def test_the_hook_does_not_own_state_or_start_the_daemon(self):
        src = _read(HOOK)
        for token in ("--mark-shown", "injection_shown", "writ_ensure_server", "nohup"):
            assert token not in src


class TestRegistration:
    @staticmethod
    def _blocks(path: Path) -> list[dict]:
        return json.loads(path.read_text())["hooks"]["UserPromptSubmit"]

    @pytest.mark.parametrize("path", [HOOKS_JSON, SETTINGS_TEMPLATE])
    def test_documents_has_its_own_block_directly_after_the_recall_hook(self, path):
        blocks = self._blocks(path)
        commands = [[h["command"] for h in b["hooks"]] for b in blocks]
        owner = [i for i, cmds in enumerate(commands) if any("writ-inject-documents.sh" in c for c in cmds)]
        recall = [i for i, cmds in enumerate(commands) if any("writ-inject-recall.sh" in c for c in cmds)]
        assert len(owner) == 1 and len(recall) == 1
        assert len(commands[owner[0]]) == 1, "the documents hook shares a block with another command"
        assert owner[0] == recall[0] + 1

    def test_hooks_json_uses_the_plugin_root_command_form_and_an_empty_matcher(self):
        block = next(b for b in self._blocks(HOOKS_JSON)
                     if any("writ-inject-documents.sh" in h["command"] for h in b["hooks"]))
        assert block["matcher"] == ""
        assert block["hooks"][0]["command"] == 'bash "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/writ-inject-documents.sh"'


class TestBehavior:
    def test_it_prints_only_the_documents_block_when_every_field_is_filled(self, tmp_path):
        bundle = _bundle(documents_block=DOCS_BLOCK, always_on_block="AO-LEAK-1", rules_text="RANKED-LEAK-2",
                         methodology_block="METH-LEAK-3", recall_block="RECALL-LEAK-4",
                         nudge="NO_RULES", nudge_text="[Writ: NUDGE-LEAK-5]")
        proc, requests = _run(tmp_path, bundle)
        assert proc.returncode == 0, proc.stderr
        assert DOCS_BLOCK in proc.stdout
        for leak in ("AO-LEAK-1", "RANKED-LEAK-2", "METH-LEAK-3", "RECALL-LEAK-4", "NUDGE-LEAK-5"):
            assert leak not in proc.stdout
        assert proc.stdout == f"\n{DOCS_BLOCK}\n"

    def test_it_sends_one_request_naming_only_the_documents_section(self, tmp_path):
        _proc, requests = _run(tmp_path, _bundle(documents_block=DOCS_BLOCK))
        assert len(requests) == 1
        body = requests[0].json_body
        assert body["sections"] == ["documents"]
        assert body["prompt"] == LONG_PROMPT
        assert "project_root" in body and "mode" in body and "session_id" in body
        assert body.get("reserve_chars", 0) == 0

    def test_a_prompt_under_the_minimum_length_prints_nothing_and_sends_nothing(self, tmp_path):
        proc, requests = _run(tmp_path, _bundle(documents_block=DOCS_BLOCK), prompt="ok")
        assert proc.returncode == 0
        assert proc.stdout == ""
        assert requests == []

    def test_a_sub_agent_still_gets_the_documents_block(self, tmp_path):
        proc, requests = _run(tmp_path, _bundle(documents_block=DOCS_BLOCK), agent_id="agent-7")
        assert proc.returncode == 0, proc.stderr
        assert len(requests) == 1
        assert DOCS_BLOCK in proc.stdout

    def test_output_never_exceeds_9500_characters_for_a_20000_character_block(self, tmp_path):
        huge = "\n".join(f"DC-{i:04d} " + "x" * 90 for i in range(220))[:20000]
        proc, _requests = _run(tmp_path, _bundle(documents_block=huge))
        assert proc.returncode == 0, proc.stderr
        assert 0 < len(proc.stdout) <= CEILING

    def test_an_empty_documents_block_prints_nothing(self, tmp_path):
        proc, requests = _run(tmp_path, _bundle(documents_block=""))
        assert proc.returncode == 0
        assert proc.stdout == ""
        assert len(requests) == 1

    def test_an_error_response_prints_nothing(self, tmp_path):
        proc, _requests = _run(tmp_path, _bundle(documents_block=DOCS_BLOCK, error=True))
        assert proc.returncode == 0
        assert proc.stdout == ""

    def test_a_failed_route_prints_nothing_and_exits_zero(self, tmp_path):
        proc, _requests = _run(tmp_path, 500)
        assert proc.returncode == 0
        assert proc.stdout == ""

    def test_an_unreachable_daemon_fails_open_without_leaking_curl_text(self, tmp_path):
        proc, _requests = _run(tmp_path, None, unreachable=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == ""
        assert "Traceback" not in proc.stderr
        assert "curl:" not in proc.stdout and "Connection refused" not in proc.stdout
