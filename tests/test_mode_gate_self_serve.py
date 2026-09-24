"""The no-mode denial hands the agent the exact command, with the session id it refused."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from writ.session import gates

REPO = Path(__file__).resolve().parent.parent
SHIM = REPO / "bin" / "writ"


@pytest.fixture()
def iso(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "cache"))
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    monkeypatch.chdir(proj)
    return proj


def _deny(sid: str, proj: Path) -> dict:
    return gates._can_write_check(sid, {"tool_input": {"file_path": str(proj / "src" / "x.py")}})


@pytest.mark.parametrize("sid", ["gate-self-serve-1", "agent-abc123"])
def test_the_denial_names_the_command_with_the_real_session_id(iso, sid):
    res = _deny(sid, iso)
    assert res["can_write"] is False
    assert res["reason"].startswith(
        "[ENF-GATE-MODE] No mode declared. Set a mode before writing code.")
    assert f"writ mode set work {sid}" in res["reason"]
    for mode in ("conversation", "debug", "investigate", "review"):
        assert mode in res["reason"]


def test_running_the_printed_command_lifts_the_mode_denial(iso, tmp_path):
    sid = "gate-self-serve-2"
    assert "[ENF-GATE-MODE]" in _deny(sid, iso)["reason"]
    r = subprocess.run([str(SHIM), "mode", "set", "work", sid], cwd=str(iso),
                       env={**os.environ, "WRIT_CACHE_DIR": str(tmp_path / "cache")},
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "[ENF-GATE-MODE]" not in (_deny(sid, iso)["reason"] or "")


# A sub-agent takes its mode from the main session at SubagentStart. When it has none, the
# fix is the main session's, so the sub-agent is told to report back rather than handed a
# command that would give it a mode (and blanket write access) its dispatcher never had.
SUBAGENT_ENVELOPES = {
    "server_body": lambda proj: {"tool_input": {"file_path": str(proj / "src" / "x.py")},
                                 "parent_session_id": "main-sess-1"},
    "raw_hook_payload": lambda proj: {"session_id": "main-sess-1", "agent_id": "agent-7",
                                      "tool_input": {"file_path": str(proj / "src" / "x.py")}},
}


@pytest.mark.parametrize("shape", sorted(SUBAGENT_ENVELOPES))
def test_a_subagent_is_sent_back_to_the_main_session(iso, shape):
    res = gates._can_write_check("agent-7", SUBAGENT_ENVELOPES[shape](iso))
    assert res["can_write"] is False
    assert res["reason"].startswith("[ENF-GATE-MODE] No mode declared.")
    assert "writ mode set work agent-7" not in res["reason"]
    assert "report" in res["reason"].lower()
    assert "writ mode set <mode> main-sess-1" in res["reason"]


def test_an_empty_parent_id_is_treated_as_the_main_session(iso):
    env = {"tool_input": {"file_path": str(iso / "src" / "x.py")}, "parent_session_id": ""}
    assert "writ mode set work main-x" in gates._can_write_check("main-x", env)["reason"]
