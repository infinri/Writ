"""Program item 1a: the per-prompt injection is four UserPromptSubmit hooks, each capped.

The host caps each hook command's injected text at 10,000 characters and swaps anything
longer for a file path plus a short preview. The single `writ-rag-inject.sh` printed 10.4 to
17.2 KB per turn. The approved design is one hook per section:

    writ-rag-inject.sh        ranked rules + every control line (routing, status, reminders,
                              nudge, escalation); buffers its output under the ceiling
    writ-inject-always-on.sh  the ALWAYS-ACTIVE RULES block, nothing else
    writ-inject-methodology.sh  the methodology companion block, nothing else
    writ-inject-recall.sh     the once-per-session recall briefing, nothing else

This module drives the REAL hook scripts as subprocesses (`bash <hook>` with the JSON
envelope on stdin, the way the host does) against a loopback STUB daemon, and sources the
real `bin/lib/common.sh` helpers for the pure-bash pieces. The stub records every request, so
the request shape (one POST per hook, one section each, `reserve_chars`) is asserted from what
the hook actually sent, not from reading its source.

WHAT A STUB CAN AND CANNOT PROVE. It proves each hook's branch structure, what it sends, what
it prints and how it caps. It does not prove the server's rendering (tests/
test_prompt_injection_ceiling.py does, against the real formatter), and it cannot prove that
the host really concatenates hook outputs in any order (the design does not rely on one).

THE STUB DELIBERATELY RETURNS MORE THAN A REAL SERVER WOULD in two places, both on purpose:
20,000 characters in every text field (so the bash backstop `writ_emit_capped` is what keeps
each hook under 9,500), and a response that carries ALL FOUR text fields to every hook (the
stale-daemon shape, where `sections` is ignored), so "no section text appears in more than one
hook's output" is proven against the worst case instead of the cooperative one.

Script-existence is asserted FIRST in `_run_hook`, so before the hooks exist the tests fail with
a message naming the missing file rather than a bash exit-127 three assertions later.

Run with `WRIT_TEST_NO_ISOLATION=1 python3 -m pytest tests/test_prompt_hooks_split.py` when no
isolated graph is configured; nothing here touches the graph or a real daemon.
"""
from __future__ import annotations

import contextlib
import http.server
import itertools
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

# autouse: pins cwd to a sandbox so a `mode` write cannot touch THIS repo's gate artifacts.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401
from tests.fixtures.net import free_port

REPO = Path(__file__).resolve().parent.parent
COMMON_SH = REPO / "bin" / "lib" / "common.sh"
SECTION_LIB = REPO / "bin" / "lib" / "writ-prompt-section.sh"
FRICTION_JQ = REPO / "bin" / "lib" / "friction-rows.jq"
FRICTION_PY = REPO / "bin" / "lib" / "writ_friction_rows.py"
HOOKS_JSON = REPO / "hooks" / "hooks.json"
SCRIPTS = REPO / "hooks" / "scripts"

CEILING = 9500
TRUNCATION_LINE = "[Writ: output truncated at 9500 characters]"

RANKED = "writ-rag-inject.sh"
ALWAYS_ON = "writ-inject-always-on.sh"
METHODOLOGY = "writ-inject-methodology.sh"
RECALL = "writ-inject-recall.sh"
ALL_HOOKS = [RANKED, ALWAYS_ON, METHODOLOGY, RECALL]
SECTION_OF = {RANKED: "ranked", ALWAYS_ON: "always_on", METHODOLOGY: "methodology", RECALL: "recall"}
SECTION_HOOKS = [ALWAYS_ON, METHODOLOGY, RECALL]

LONG_PROMPT = "refactor the authentication middleware module for the orders service"
ROUTING_PROMPT = "please implement the new widget handler class for the orders module"

needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")


# --------------------------------------------------------------------------- #
# Loopback stub daemon (routes may be a callable taking the parsed request body)
# --------------------------------------------------------------------------- #
class _Handler(http.server.BaseHTTPRequestHandler):
    routes: dict = {}
    requests: list = []

    def _handle(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        text = raw.decode("utf-8", "replace")
        try:
            body = json.loads(text) if text else None
        except ValueError:
            body = None
        self.requests.append({"method": self.command, "path": self.path.split("?", 1)[0],
                              "body": body, "raw": text})
        route = self.routes.get(self.path.split("?", 1)[0])
        if route is None:
            status, payload = 404, {"error": "not found"}
        else:
            status, payload = route
            if callable(payload):
                payload = payload(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def do_GET(self):  # noqa: N802
        self._handle()

    def do_POST(self):  # noqa: N802
        self._handle()

    def log_message(self, *_a):
        pass


@contextlib.contextmanager
def _stub(routes: dict):
    handler = type("H", (_Handler,), {"routes": dict(routes), "requests": []})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1], handler.requests
    finally:
        server.shutdown()
        server.server_close()


def _bundles(requests: list) -> list[dict]:
    return [r for r in requests if r["path"] == "/prompt-bundle" and r["method"] == "POST"]


def _line(width: int, tag: str, i: int) -> str:
    return (f"{tag}-{i:04d} " + "x" * width)[:width]


def _big_text(tag: str, total: int = 20000, width: int = 99) -> str:
    return "\n".join(_line(width, tag, i) for i in range(total // (width + 1) + 1))[:total]


def _meta_for(section: str, mode: str = "conversation") -> dict:
    return {
        "broad_meta": {"cost": 10, "rule_ids": ["R-1"]} if section == "ranked" else None,
        "ao_meta": {"tokens": 12, "count": 1, "rule_ids": ["AO-1"]} if section == "always_on" else None,
        "method_meta": ({"cost": 5, "rule_ids": ["M-1"], "query_source": "methodology"}
                        if section == "methodology" else None),
    }


def _section_response(section: str, text: str, **extra) -> dict:
    """What a cooperative server returns: ONLY the requested section's field is filled."""
    field = {"ranked": "rules_text", "always_on": "always_on_block",
             "methodology": "methodology_block", "recall": "recall_block"}[section]
    body = {"error": False, "skipped": False, "always_on_block": "", "rules_text": "",
            "methodology_block": "", "recall_block": "", "nudge": "", "nudge_text": "",
            **_meta_for(section)}
    body[field] = text
    body.update(extra)
    return body


def _superset_response(**fields) -> dict:
    """The stale-daemon shape: every text field filled, whatever was asked for."""
    body = {"error": False, "skipped": False, "nudge": "", "nudge_text": "",
            "always_on_block": "", "rules_text": "", "methodology_block": "", "recall_block": "",
            "broad_meta": None, "ao_meta": None, "method_meta": None}
    body.update(fields)
    return body


def _routes(sid: str, bundle, *, mode: str = "conversation", phase: str | None = None) -> dict:
    return {
        f"/session/{sid}": (200, {
            "mode": mode, "current_phase": phase, "loaded_rule_ids": [], "remaining_budget": 8000,
            "is_orchestrator": False, "recall_briefed": False, "gates_approved": [],
        }),
        f"/session/{sid}/should-skip": (200, {"known": True, "should_skip": False}),
        f"/session/{sid}/check-escalation": (200, {"needed": False}),
        "/prompt-bundle": (200, bundle),
    }


def _by_section(responses: dict[str, dict]):
    """A /prompt-bundle route that answers per the section the request names."""
    def respond(body):
        section = ((body or {}).get("sections") or ["ranked"])[0]
        return responses[section]
    return respond


def _env(tmp_path: Path, port: int, extra: dict | None = None) -> dict:
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    env = {**os.environ, "WRIT_HOST": "127.0.0.1", "WRIT_PORT": str(port), "WRIT_NO_AUTOSTART": "1",
           "WRIT_CACHE_DIR": str(cache), "HOME": str(tmp_path)}
    env.pop("WRIT_SOCKET", None)
    env.pop("WRIT_NO_JQ", None)
    env.update(extra or {})
    return env


def _run_hook(script: str, tmp_path: Path, port: int, *, sid: str = "split-sid",
              prompt: str = LONG_PROMPT, envelope: dict | None = None,
              env_extra: dict | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    path = SCRIPTS / script
    assert path.is_file(), f"hook script {path} does not exist"
    stdin = {"session_id": sid, "prompt": prompt, "cwd": str(tmp_path), "hook_event_name": "UserPromptSubmit"}
    stdin.update(envelope or {})
    return subprocess.run(["bash", str(path)], input=json.dumps(stdin), capture_output=True, text=True,
                          env=_env(tmp_path, port, env_extra), cwd=str(tmp_path), timeout=timeout)


def _seed_cache(tmp_path: Path, sid: str, **fields) -> Path:
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    path = cache / f"writ-session-{sid}.json"
    path.write_text(json.dumps(fields))
    return path


def _bash(snippet: str, tmp_path: Path, *, env_extra: dict | None = None,
          stdin: str | bytes | None = None, source_section_lib: bool = False) -> subprocess.CompletedProcess:
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    env = {**os.environ, "WRIT_CACHE_DIR": str(cache), "WRIT_DIR": str(REPO), "HOME": str(tmp_path)}
    env.pop("WRIT_NO_JQ", None)
    env.update(env_extra or {})
    prelude = f'set -euo pipefail; source "{COMMON_SH}"; WRIT_HOOK_LOG_SINK=/dev/null; '
    if source_section_lib:
        prelude += f'source "{SECTION_LIB}"; '
    return subprocess.run(["bash", "-c", prelude + snippet], input=stdin, capture_output=True,
                          env=env, cwd=str(tmp_path), timeout=60)


# --------------------------------------------------------------------------- #
# Task 6. Registration
# --------------------------------------------------------------------------- #
class TestRegistration:
    @staticmethod
    def _blocks() -> list[dict]:
        return json.loads(HOOKS_JSON.read_text())["hooks"]["UserPromptSubmit"]

    @staticmethod
    def _commands(block: dict) -> list[str]:
        return [h["command"] for h in block["hooks"]]

    def test_all_four_injection_hooks_are_registered_on_user_prompt_submit(self):
        commands = [c for b in self._blocks() for c in self._commands(b)]
        for script in ALL_HOOKS:
            assert any(c.endswith(f'/hooks/scripts/{script}"') for c in commands), f"{script} is not registered"

    def test_each_injection_hook_is_its_own_command_block(self):
        for script in ALL_HOOKS:
            owners = [b for b in self._blocks() if any(script in c for c in self._commands(b))]
            assert len(owners) == 1
            assert len(owners[0]["hooks"]) == 1, f"{script} shares a block with another command"

    def test_new_hooks_use_the_plugin_root_command_form_and_an_empty_matcher(self):
        for script in (ALWAYS_ON, METHODOLOGY, RECALL):
            block = next(b for b in self._blocks() if any(script in c for c in self._commands(b)))
            assert block["matcher"] == ""
            assert block["hooks"][0]["type"] == "command"
            assert block["hooks"][0]["command"] == f'bash "${{CLAUDE_PLUGIN_ROOT}}/hooks/scripts/{script}"'

    def test_the_three_new_hooks_follow_the_ranked_hook_in_order(self):
        commands = [c for b in self._blocks() for c in self._commands(b)]
        positions = [next(i for i, c in enumerate(commands) if s in c) for s in (RANKED, ALWAYS_ON, METHODOLOGY, RECALL)]
        assert positions == sorted(positions)

    def test_the_ranked_hook_keeps_its_file_name(self):
        assert (SCRIPTS / RANKED).is_file()

    def test_new_hook_scripts_exist_and_are_executable(self):
        for script in (ALWAYS_ON, METHODOLOGY, RECALL):
            path = SCRIPTS / script
            assert path.is_file(), f"{path} does not exist"
            assert os.access(path, os.X_OK), f"{path} is not executable"
            # Executable for everyone and not world-writable; group-write varies with the
            # checkout's umask (git stores 100755 either way).
            mode = path.stat().st_mode
            assert mode & 0o555 == 0o555 and not mode & 0o002, oct(mode)

    def test_each_new_hook_calls_the_shared_section_body_for_its_own_section(self):
        expected = {ALWAYS_ON: "writ_prompt_section_main always_on writ-inject-always-on",
                    METHODOLOGY: "writ_prompt_section_main methodology writ-inject-methodology",
                    RECALL: "writ_prompt_section_main recall writ-inject-recall"}
        for script, call in expected.items():
            text = (SCRIPTS / script).read_text()
            assert call in text
            assert text.startswith("#!/usr/bin/env bash")
            assert "exit 0" in text

    def test_total_registered_command_count_is_fifty_one(self):
        def count(node) -> int:
            if isinstance(node, dict):
                return (1 if "command" in node else 0) + sum(count(v) for v in node.values())
            if isinstance(node, list):
                return sum(count(v) for v in node)
            return 0
        assert count(json.loads(HOOKS_JSON.read_text())) == 51  # 48 after item 1a, plus the three tool-failure budget registrations (item 7c)


# --------------------------------------------------------------------------- #
# Task 5. Shared bash
# --------------------------------------------------------------------------- #
def _emit_capped(text: str, limit: int, tmp_path: Path, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    return _bash(f"writ_emit_capped {limit}", tmp_path, env_extra=env_extra, stdin=text.encode("utf-8"))


class TestEmitCapped:
    def test_input_under_the_limit_is_copied_unchanged(self, tmp_path):
        text = "line one\nline two\nline three\n"
        out = _emit_capped(text, 1000, tmp_path)
        assert out.returncode == 0
        assert out.stdout.decode() == text

    def test_empty_input_prints_nothing(self, tmp_path):
        out = _emit_capped("", 1000, tmp_path)
        assert out.returncode == 0
        assert out.stdout == b""

    def test_input_exactly_at_the_limit_is_unchanged(self, tmp_path):
        text = ("a" * 49 + "\n") * 2  # 100 characters
        out = _emit_capped(text, 100, tmp_path)
        assert out.stdout.decode() == text

    def test_one_character_over_the_limit_is_truncated(self, tmp_path):
        text = ("a" * 49 + "\n") + ("b" * 50 + "\n")  # 101 characters
        out = _emit_capped(text, 100, tmp_path).stdout.decode()
        assert len(out) <= 100
        assert out.rstrip("\n").split("\n")[-1] == "[Writ: output truncated at 100 characters]"
        assert "b" * 50 not in out

    def test_over_limit_input_prints_whole_lines_then_the_marker(self, tmp_path):
        lines = [_line(99, "L", i) for i in range(400)]
        out = _emit_capped("\n".join(lines) + "\n", CEILING, tmp_path).stdout.decode()
        printed = out.rstrip("\n").split("\n")
        assert printed[-1] == TRUNCATION_LINE
        assert printed[:-1] == lines[: len(printed) - 1], "only whole input lines, in order"
        assert len(out) <= CEILING

    def test_twenty_thousand_characters_are_capped_at_the_ceiling(self, tmp_path):
        out = _emit_capped(_big_text("BIG") + "\n", CEILING, tmp_path).stdout.decode()
        assert 0 < len(out) <= CEILING

    def test_multibyte_text_is_counted_in_characters_in_a_utf8_locale(self, tmp_path):
        locales = subprocess.run(["locale", "-a"], capture_output=True, text=True).stdout.lower()
        if "c.utf8" not in locales and "c.utf-8" not in locales:
            pytest.skip("no C.UTF-8 locale available")
        text = "\n".join("é" * 98 for _ in range(300)) + "\n"
        out = _emit_capped(text, 2000, tmp_path, env_extra={"LC_ALL": "C.UTF-8"}).stdout.decode("utf-8")
        assert 0 < len(out) <= 2000
        assert out.rstrip("\n").split("\n")[-1] == "[Writ: output truncated at 2000 characters]"

    def test_the_bound_also_holds_in_the_c_locale_where_bash_counts_bytes(self, tmp_path):
        text = "\n".join("é" * 98 for _ in range(300)) + "\n"
        out = _emit_capped(text, 2000, tmp_path, env_extra={"LC_ALL": "C"}).stdout
        assert out, "the helper must print something for oversize input"
        assert len(out) <= 2000, "bytes are never fewer than characters, so a byte bound is a character bound"
        assert out.decode("utf-8").rstrip("\n").split("\n")[-1] == "[Writ: output truncated at 2000 characters]"

    def test_the_ceiling_constant_is_defined_in_common_sh(self, tmp_path):
        out = _bash('printf "%s" "$WRIT_PROMPT_CHAR_CEILING"', tmp_path)
        assert out.stdout.decode() == "9500"

    def test_the_minimum_query_length_constant_is_ten(self, tmp_path):
        out = _bash('printf "%s" "$WRIT_MIN_QUERY_LENGTH"', tmp_path)
        assert out.stdout.decode() == "10"


def _route_oracle(prior: str, source: str, hint: str) -> str:
    """The documented rule, written independently of the bash under test."""
    if not hint:
        return "none"
    if not prior:
        return "init"
    if prior == hint:
        return "none"
    if source == "auto":
        return "switch"
    if (prior, hint) in {("work", "investigate"), ("investigate", "work")}:
        return "switch"
    return "none"


class TestRouteDecisionTable:
    PRIORS = ["", "work", "investigate", "conversation", "debug", "review"]
    SOURCES = ["", "auto", "manual", "user"]
    HINTS = ["", "work", "investigate"]

    def _decide(self, tmp_path, prior, source, hint) -> str:
        out = _bash(f'writ_route_decision "{prior}" "{source}" "{hint}"', tmp_path)
        assert out.returncode == 0, out.stderr.decode()
        return out.stdout.decode()

    def test_the_full_matrix_of_prior_source_and_hint(self, tmp_path):
        combos = list(itertools.product(self.PRIORS, self.SOURCES, self.HINTS))
        script = "; ".join(f'printf "%s\\n" "$(writ_route_decision "{p}" "{s}" "{h}")"' for p, s, h in combos)
        out = _bash(script, tmp_path)
        assert out.returncode == 0, out.stderr.decode()
        got = out.stdout.decode().split("\n")[:-1]
        assert len(got) == len(combos)
        wrong = [(c, g, _route_oracle(*c)) for c, g in zip(combos, got) if g != _route_oracle(*c)]
        assert not wrong, f"(prior, source, hint) -> got, expected: {wrong[:8]}"

    def test_no_hint_never_routes(self, tmp_path):
        assert self._decide(tmp_path, "work", "auto", "") == "none"
        assert self._decide(tmp_path, "", "", "") == "none"

    def test_no_prior_mode_initialises(self, tmp_path):
        assert self._decide(tmp_path, "", "", "work") == "init"
        assert self._decide(tmp_path, "", "", "investigate") == "init"

    def test_an_equal_mode_never_re_routes(self, tmp_path):
        assert self._decide(tmp_path, "work", "auto", "work") == "none"

    def test_auto_provenance_re_routes_from_any_mode(self, tmp_path):
        assert self._decide(tmp_path, "conversation", "auto", "work") == "switch"
        assert self._decide(tmp_path, "review", "auto", "investigate") == "switch"

    def test_work_and_investigate_re_route_each_other_whatever_the_provenance(self, tmp_path):
        assert self._decide(tmp_path, "work", "manual", "investigate") == "switch"
        assert self._decide(tmp_path, "investigate", "user", "work") == "switch"

    def test_a_manual_conversation_mode_is_not_overridden_by_a_hint(self, tmp_path):
        assert self._decide(tmp_path, "conversation", "manual", "work") == "none"
        assert self._decide(tmp_path, "debug", "", "investigate") == "none"


class TestTurnMode:
    def _turn_mode(self, tmp_path, sid, agent, hint, env_extra=None) -> str:
        out = _bash(f'writ_turn_mode "{sid}" "{agent}" "{hint}"', tmp_path, env_extra=env_extra)
        assert out.returncode == 0, out.stderr.decode()
        return out.stdout.decode().strip()

    def test_a_session_with_no_cache_and_a_work_hint_predicts_work(self, tmp_path):
        assert self._turn_mode(tmp_path, "tm-none", "", "work") == "work"

    def test_no_cache_and_no_hint_predicts_no_mode(self, tmp_path):
        assert self._turn_mode(tmp_path, "tm-nothing", "", "") == ""

    def test_a_manual_conversation_session_keeps_its_mode_despite_a_work_hint(self, tmp_path):
        _seed_cache(tmp_path, "tm-conv", mode="conversation", mode_source="explicit")
        assert self._turn_mode(tmp_path, "tm-conv", "", "work") == "conversation"

    def test_an_auto_routed_session_predicts_the_hinted_mode(self, tmp_path):
        _seed_cache(tmp_path, "tm-auto", mode="conversation", mode_source="auto")
        assert self._turn_mode(tmp_path, "tm-auto", "", "investigate") == "investigate"

    def test_a_manual_work_session_predicts_investigate_on_an_investigate_hint(self, tmp_path):
        _seed_cache(tmp_path, "tm-wi", mode="work", mode_source="explicit")
        assert self._turn_mode(tmp_path, "tm-wi", "", "investigate") == "investigate"

    def test_an_equal_hint_predicts_the_prior_mode(self, tmp_path):
        _seed_cache(tmp_path, "tm-same", mode="work", mode_source="explicit")
        assert self._turn_mode(tmp_path, "tm-same", "", "work") == "work"

    def test_the_prediction_is_idempotent_after_the_ranked_hook_wrote_the_mode(self, tmp_path):
        _seed_cache(tmp_path, "tm-idem", mode="work", mode_source="auto")
        assert self._turn_mode(tmp_path, "tm-idem", "", "work") == "work"

    def test_a_sub_agent_reads_its_own_cache_and_ignores_the_hint(self, tmp_path):
        _seed_cache(tmp_path, "tm-agent", mode="debug", mode_source="explicit")
        assert self._turn_mode(tmp_path, "tm-agent", "agent-9", "work") == "debug"

    def test_a_corrupt_cache_file_falls_back_to_the_hint(self, tmp_path):
        (tmp_path / "cache").mkdir()
        (tmp_path / "cache" / "writ-session-tm-bad.json").write_text("{not json")
        assert self._turn_mode(tmp_path, "tm-bad", "", "work") == "work"

    @pytest.mark.parametrize("no_jq", [False, True])
    def test_mode_pair_is_one_snapshot_on_both_arms(self, tmp_path, no_jq):
        if not no_jq and shutil.which("jq") is None:
            pytest.skip("jq not installed")
        _seed_cache(tmp_path, "tm-pair", mode="work", mode_source="auto")
        out = _bash('writ_session_mode_pair "tm-pair"', tmp_path, env_extra={"WRIT_NO_JQ": "1"} if no_jq else None)
        assert out.stdout.decode() == "work|auto"

    def test_mode_pair_prints_nothing_for_a_missing_or_corrupt_cache(self, tmp_path):
        missing = _bash('writ_session_mode_pair "tm-missing"', tmp_path)
        assert missing.returncode == 0, missing.stderr.decode()
        assert missing.stdout == b""
        (tmp_path / "cache").mkdir(exist_ok=True)
        (tmp_path / "cache" / "writ-session-tm-corrupt.json").write_text("][")
        corrupt = _bash('writ_session_mode_pair "tm-corrupt"', tmp_path)
        assert corrupt.returncode == 0, corrupt.stderr.decode()
        assert corrupt.stdout == b""


class TestSectionRequestBody:
    NASTY = 'say "hello"\nthen\tpress\\n and \'quote\' $HOME `date` é中'

    def _body(self, tmp_path, section, mode, prompt, root, aof, no_jq: bool) -> dict:
        script = (f'writ_section_request "sid-1" "{section}" "{mode}" "$WRIT_TEST_PROMPT" "{root}" "{aof}"')
        env = {"WRIT_TEST_PROMPT": prompt}
        if no_jq:
            env["WRIT_NO_JQ"] = "1"
        out = _bash(script, tmp_path, env_extra=env, source_section_lib=True)
        assert out.returncode == 0, out.stderr.decode()
        return json.loads(out.stdout.decode())

    @pytest.mark.parametrize("no_jq", [False, True])
    def test_body_names_exactly_one_section_and_the_request_fields(self, tmp_path, no_jq):
        if not no_jq and shutil.which("jq") is None:
            pytest.skip("jq not installed")
        body = self._body(tmp_path, "always_on", "work", "plain prompt", "/proj", "true", no_jq)
        assert body == {"session_id": "sid-1", "sections": ["always_on"], "mode": "work",
                        "prompt": "plain prompt", "project_root": "/proj", "always_on_filter": True}

    @pytest.mark.parametrize("no_jq", [False, True])
    def test_always_on_filter_is_a_json_boolean_in_both_states(self, tmp_path, no_jq):
        if not no_jq and shutil.which("jq") is None:
            pytest.skip("jq not installed")
        assert self._body(tmp_path, "methodology", "", "p", "", "true", no_jq)["always_on_filter"] is True
        assert self._body(tmp_path, "methodology", "", "p", "", "false", no_jq)["always_on_filter"] is False

    @needs_jq
    def test_the_jq_and_python_arms_build_the_same_body(self, tmp_path):
        for section in ("always_on", "methodology", "recall"):
            with_jq = self._body(tmp_path, section, "work", self.NASTY, "/p", "true", False)
            without = self._body(tmp_path, section, "work", self.NASTY, "/p", "true", True)
            assert with_jq == without

    @pytest.mark.parametrize("no_jq", [False, True])
    def test_a_prompt_with_quotes_newlines_and_shell_metacharacters_round_trips(self, tmp_path, no_jq):
        if not no_jq and shutil.which("jq") is None:
            pytest.skip("jq not installed")
        assert self._body(tmp_path, "recall", "", self.NASTY, "", "true", no_jq)["prompt"] == self.NASTY

    @pytest.mark.parametrize("no_jq", [False, True])
    def test_an_empty_mode_stays_an_empty_string(self, tmp_path, no_jq):
        if not no_jq and shutil.which("jq") is None:
            pytest.skip("jq not installed")
        assert self._body(tmp_path, "recall", "", "p", "", "true", no_jq)["mode"] == ""


class TestSectionFrictionRows:
    """Each section response yields only its own friction row; the filter and the python
    fallback produce identical rows (the python program is bin/lib/writ_friction_rows.py)."""

    SID = "fr-sid"

    @staticmethod
    def _ranked():
        return _section_response("ranked", "rules")

    def _py_rows(self, body: dict, mode: str = "work") -> list[dict]:
        assert FRICTION_PY.is_file(), f"{FRICTION_PY} does not exist"
        out = subprocess.run(["python3", str(FRICTION_PY)], input=json.dumps(body), capture_output=True,
                             text=True, env={**os.environ, "WRIT_SID": self.SID, "WRIT_MODE": mode}, timeout=30)
        assert out.returncode == 0, out.stderr
        return [json.loads(ln) for ln in out.stdout.splitlines() if ln.strip()]

    def _jq_rows(self, body: dict, mode: str = "work") -> list[dict]:
        out = subprocess.run(["jq", "-R", "-s", "-r", "--arg", "sid", self.SID, "--arg", "mode", mode,
                              "-f", str(FRICTION_JQ)], input=json.dumps(body), capture_output=True,
                             text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return [json.loads(ln) for ln in out.stdout.splitlines() if ln.strip()]

    def test_the_python_fallback_lives_in_its_own_file(self):
        assert FRICTION_PY.is_file()
        assert "friction-rows.jq" in FRICTION_PY.read_text(), "its docstring names the filter it mirrors"

    def test_the_ranked_response_yields_one_broad_rag_query_row(self):
        rows = self._py_rows(_section_response("ranked", "rules"))
        assert [(r["event"], r["query_source"]) for r in rows] == [("rag_query", "broad")]

    def test_a_suppressed_ranked_response_yields_one_suppression_row(self):
        body = _section_response("ranked", "")
        body["broad_meta"] = {"suppressed": True}
        assert [r["event"] for r in self._py_rows(body)] == ["rag_channel_suppressed"]

    def test_the_always_on_response_yields_one_always_on_inject_row(self):
        rows = self._py_rows(_section_response("always_on", "block"))
        assert [r["event"] for r in rows] == ["always_on_inject"]
        assert rows[0]["tokens"] == 12

    def test_the_methodology_response_yields_one_methodology_rag_query_row(self):
        rows = self._py_rows(_section_response("methodology", "block"))
        assert [(r["event"], r["query_source"]) for r in rows] == [("rag_query", "methodology")]

    def test_the_recall_response_yields_no_friction_row(self):
        assert self._py_rows(_section_response("recall", "briefing")) == []

    @needs_jq
    @pytest.mark.parametrize("section", ["ranked", "always_on", "methodology", "recall"])
    def test_the_filter_and_the_python_arm_produce_identical_rows(self, section):
        body = _section_response(section, "text")
        assert sorted(map(json.dumps, self._jq_rows(body)), key=str) == sorted(map(json.dumps, self._py_rows(body)), key=str)

    @needs_jq
    def test_the_filter_and_the_python_arm_agree_on_the_suppressed_shape(self):
        body = _section_response("ranked", "")
        body["broad_meta"] = {"suppressed": True}
        assert self._jq_rows(body) == self._py_rows(body)

    def test_malformed_input_yields_no_rows_and_does_not_fail(self):
        assert FRICTION_PY.is_file(), f"{FRICTION_PY} does not exist"
        out = subprocess.run(["python3", str(FRICTION_PY)], input="not json", capture_output=True, text=True,
                             env={**os.environ, "WRIT_SID": self.SID, "WRIT_MODE": ""}, timeout=30)
        assert out.returncode == 0 and out.stdout.strip() == ""


# --------------------------------------------------------------------------- #
# Task 6. Hooks
# --------------------------------------------------------------------------- #
class TestPerHookCeiling:
    def _oversize(self) -> dict:
        return _superset_response(
            always_on_block=_big_text("AO"), rules_text=_big_text("RK"),
            methodology_block=_big_text("MT"), recall_block=_big_text("RC"),
            nudge="NO_RULES", nudge_text=_big_text("NG", 3000),
            broad_meta={"cost": 1, "rule_ids": ["R-1"]}, ao_meta={"tokens": 1, "count": 1, "rule_ids": ["AO-1"]},
            method_meta={"cost": 1, "rule_ids": ["M-1"], "query_source": "methodology"})

    @pytest.mark.parametrize("script", ALL_HOOKS)
    def test_every_hook_prints_at_most_9500_characters_for_20000_character_fields(self, tmp_path, script):
        sid = "ceil-sid"
        with _stub(_routes(sid, self._oversize())) as (port, requests):
            run = _run_hook(script, tmp_path, port, sid=sid)
        assert run.returncode == 0, run.stderr
        assert len(_bundles(requests)) == 1, f"{script} should have reached /prompt-bundle once"
        assert 0 < len(run.stdout) <= CEILING, f"{script} printed {len(run.stdout)} characters"

    def test_the_ranked_hook_with_a_pending_gate_reminder_ends_with_the_truncation_marker(self, tmp_path):
        sid = "ceil-work"
        subprocess.run(["python3", str(REPO / "bin" / "lib" / "writ-session.py"), "mode", "set", "work", sid],
                       env=_env(tmp_path, 1), capture_output=True, text=True, timeout=30, cwd=str(tmp_path))
        with _stub(_routes(sid, self._oversize(), mode="work", phase="planning")) as (port, requests):
            run = _run_hook(RANKED, tmp_path, port, sid=sid)
        assert run.returncode == 0, run.stderr
        assert len(run.stdout) <= CEILING
        assert run.stdout.rstrip("\n").split("\n")[-1] == TRUNCATION_LINE

    @pytest.mark.parametrize("script", ALL_HOOKS)
    def test_an_in_budget_block_is_printed_whole_without_the_marker(self, tmp_path, script):
        sid = "ceil-small"
        text = "\n".join(f"SMALL-{i} " + "y" * 60 for i in range(20))
        section = SECTION_OF[script]
        with _stub(_routes(sid, _section_response(section, text))) as (port, _requests):
            run = _run_hook(script, tmp_path, port, sid=sid)
        assert run.returncode == 0, run.stderr
        assert text in run.stdout
        assert TRUNCATION_LINE not in run.stdout


class TestNoDuplication:
    MARKERS = {"always_on_block": "AO-BLOCK-MARKER-7f3a", "rules_text": "RANKED-RULES-MARKER-91bc",
               "methodology_block": "METHODOLOGY-MARKER-c40d", "recall_block": "RECALL-MARKER-5e12"}
    NUDGE_MARKER = "NUDGE-TEXT-MARKER-0a9f"
    MODE_REMINDER = "Conversation mode. Rules injected as context"
    OWNER = {"always_on_block": ALWAYS_ON, "rules_text": RANKED,
             "methodology_block": METHODOLOGY, "recall_block": RECALL}

    def _run_all(self, tmp_path) -> dict[str, str]:
        sid = "dup-sid"
        bundle = _superset_response(**self.MARKERS, nudge="NO_RULES", nudge_text=f"[Writ: {self.NUDGE_MARKER}]",
                                    broad_meta={"cost": 1, "rule_ids": ["R-1"]})
        _seed_cache(tmp_path, sid, mode="conversation", mode_source="explicit")
        outputs = {}
        with _stub(_routes(sid, bundle, mode="conversation")) as (port, _requests):
            for script in ALL_HOOKS:
                run = _run_hook(script, tmp_path, port, sid=sid)
                assert run.returncode == 0, f"{script}: {run.stderr}"
                outputs[script] = run.stdout
        return outputs

    def test_each_section_text_appears_in_exactly_its_own_hook(self, tmp_path):
        outputs = self._run_all(tmp_path)
        for field, marker in self.MARKERS.items():
            holders = sorted(s for s, out in outputs.items() if marker in out)
            assert holders == [self.OWNER[field]], f"{field} marker found in {holders}"

    def test_the_nudge_appears_only_in_the_ranked_hook(self, tmp_path):
        outputs = self._run_all(tmp_path)
        assert sorted(s for s, out in outputs.items() if self.NUDGE_MARKER in out) == [RANKED]

    def test_the_mode_reminder_appears_only_in_the_ranked_hook(self, tmp_path):
        outputs = self._run_all(tmp_path)
        assert sorted(s for s, out in outputs.items() if self.MODE_REMINDER in out) == [RANKED]

    def test_no_section_hook_prints_a_control_line(self, tmp_path):
        outputs = self._run_all(tmp_path)
        for script in SECTION_HOOKS:
            assert "[Writ:" not in outputs[script].replace("[Writ: methodology companion]", ""), \
                f"{script} printed control text"

    def test_the_orchestrator_status_line_appears_only_in_the_ranked_hook(self, tmp_path):
        sid = "dup-orch"
        routes = _routes(sid, _superset_response(**self.MARKERS))
        routes[f"/session/{sid}"] = (200, {"mode": "work", "current_phase": "planning", "loaded_rule_ids": [],
                                           "remaining_budget": 8000, "is_orchestrator": True,
                                           "recall_briefed": False, "gates_approved": []})
        with _stub(routes) as (port, _requests):
            outputs = {s: _run_hook(s, tmp_path, port, sid=sid).stdout for s in ALL_HOOKS}
        holders = [s for s, out in outputs.items() if "[Writ: mode=work, phase=planning" in out]
        assert holders == [RANKED]


class TestRequestShape:
    @pytest.mark.parametrize("script", ALL_HOOKS)
    def test_each_hook_sends_exactly_one_request_naming_only_its_own_section(self, tmp_path, script):
        sid = "shape-sid"
        with _stub(_routes(sid, _section_response(SECTION_OF[script], "text body here"))) as (port, requests):
            run = _run_hook(script, tmp_path, port, sid=sid)
        assert run.returncode == 0, run.stderr
        bundles = _bundles(requests)
        assert len(bundles) == 1
        body = bundles[0]["body"]
        assert body["sections"] == [SECTION_OF[script]]
        assert body["session_id"] == sid

    @pytest.mark.parametrize("script", ALL_HOOKS)
    def test_the_request_carries_the_prompt_the_project_root_and_the_filter_flag(self, tmp_path, script):
        sid = "shape-fields"
        with _stub(_routes(sid, _section_response(SECTION_OF[script], "text body here"))) as (port, requests):
            _run_hook(script, tmp_path, port, sid=sid)
        body = _bundles(requests)[0]["body"]
        assert body["prompt"] == LONG_PROMPT
        assert isinstance(body["always_on_filter"], bool)
        assert "project_root" in body and "mode" in body

    def test_only_the_ranked_hook_sends_reserve_chars(self, tmp_path):
        sid = "shape-reserve-only"
        bodies = {}
        for script in ALL_HOOKS:
            with _stub(_routes(sid, _section_response(SECTION_OF[script], "text body here"))) as (port, requests):
                _run_hook(script, tmp_path, port, sid=sid)
            bodies[script] = _bundles(requests)[0]["body"]
        assert isinstance(bodies[RANKED]["reserve_chars"], int)
        for script in SECTION_HOOKS:
            assert bodies[script].get("reserve_chars", 0) == 0

    def test_the_ranked_reserve_equals_the_control_text_printed_before_the_rules(self, tmp_path):
        sid = "shape-reserve"
        marker = "RULES-BLOCK-MARKER-2d7e"
        with _stub(_routes(sid, _section_response("ranked", marker), mode="conversation")) as (port, requests):
            run = _run_hook(RANKED, tmp_path, port, sid=sid)
        assert run.returncode == 0, run.stderr
        assert marker in run.stdout
        control = run.stdout[: run.stdout.index(marker)]
        reserve = _bundles(requests)[0]["body"]["reserve_chars"]
        assert reserve > 0, "the conversation-mode reminder is control text printed before the rules"
        assert reserve == len(control)

    def test_a_ranked_hook_with_no_control_text_reserves_nothing(self, tmp_path):
        sid = "shape-noreserve"
        with _stub(_routes(sid, _section_response("ranked", "ONLY-RULES"), mode="")) as (port, requests):
            run = _run_hook(RANKED, tmp_path, port, sid=sid)
        body = _bundles(requests)[0]["body"]
        assert body["reserve_chars"] == len(run.stdout[: run.stdout.index("ONLY-RULES")])


class TestIndependence:
    @pytest.mark.parametrize("script", SECTION_HOOKS)
    def test_each_section_hook_produces_its_section_when_run_alone(self, tmp_path, script):
        sid = "indep-sid"
        text = f"ONLY-{SECTION_OF[script].upper()}-TEXT"
        with _stub(_routes(sid, _section_response(SECTION_OF[script], text))) as (port, _requests):
            run = _run_hook(script, tmp_path, port, sid=sid)
        assert run.returncode == 0, run.stderr
        assert text in run.stdout

    def test_running_the_three_section_hooks_leaves_the_session_cache_file_unchanged(self, tmp_path):
        sid = "indep-cache"
        path = _seed_cache(tmp_path, sid, mode="conversation", mode_source="explicit", queries=3)
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        with _stub(_routes(sid, _by_section({s: _section_response(s, f"{s}-text") for s in SECTION_OF.values()}))) as (port, _r):
            for script in SECTION_HOOKS:
                assert _run_hook(script, tmp_path, port, sid=sid).returncode == 0
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before
        assert sorted(p.name for p in (tmp_path / "cache").glob("writ-session-*")) == [path.name]

    def test_a_section_hook_does_not_need_the_ranked_hook_to_have_run(self, tmp_path):
        sid = "indep-solo"
        with _stub(_routes(sid, _section_response("always_on", "SOLO-ALWAYS-ON-BLOCK"))) as (port, requests):
            run = _run_hook(ALWAYS_ON, tmp_path, port, sid=sid)
        assert "SOLO-ALWAYS-ON-BLOCK" in run.stdout
        assert len(_bundles(requests)) == 1

    def test_a_section_hook_never_starts_the_daemon(self, tmp_path):
        text = "\n".join((SCRIPTS / s).read_text() for s in (ALWAYS_ON, METHODOLOGY, RECALL))
        assert "writ_ensure_server" not in text and "nohup" not in text
        shared = SECTION_LIB.read_text()
        assert "writ_ensure_server" not in shared and "nohup" not in shared


class TestAutoRouteMode:
    @pytest.mark.parametrize("script", [ALWAYS_ON, METHODOLOGY])
    def test_on_an_auto_routed_turn_the_section_requests_carry_the_hinted_mode(self, tmp_path, script):
        sid = "route-sid"
        with _stub(_routes(sid, _section_response(SECTION_OF[script], "block text here"))) as (port, requests):
            run = _run_hook(script, tmp_path, port, sid=sid, prompt=ROUTING_PROMPT)
        assert run.returncode == 0, run.stderr
        assert _bundles(requests)[0]["body"]["mode"] == "work"

    def test_a_manual_conversation_session_is_not_re_moded_by_the_hint(self, tmp_path):
        sid = "route-manual"
        _seed_cache(tmp_path, sid, mode="conversation", mode_source="explicit")
        with _stub(_routes(sid, _section_response("always_on", "block text here"))) as (port, requests):
            _run_hook(ALWAYS_ON, tmp_path, port, sid=sid, prompt=ROUTING_PROMPT)
        assert _bundles(requests)[0]["body"]["mode"] == "conversation"

    def test_the_ranked_hook_and_the_section_hooks_agree_on_the_mode(self, tmp_path):
        sid = "route-agree"
        modes = {}
        for script in (RANKED, ALWAYS_ON, METHODOLOGY):
            # A fresh session id per hook: the ranked hook writes the mode it routes to, and the
            # section hooks must predict that same mode from a cache that has none yet.
            hook_sid = f"{sid}-{script}"
            with _stub(_routes(hook_sid, _section_response(SECTION_OF[script], "block text here"))) as (port, requests):
                _run_hook(script, tmp_path, port, sid=hook_sid, prompt=ROUTING_PROMPT)
            assert len(_bundles(requests)) == 1, f"{script} did not reach /prompt-bundle"
            modes[script] = _bundles(requests)[0]["body"]["mode"]
        assert modes[ALWAYS_ON] == modes[METHODOLOGY] == modes[RANKED] == "work"


class TestRecallHook:
    def test_it_prints_the_recall_block(self, tmp_path):
        sid = "recall-sid"
        with _stub(_routes(sid, _section_response("recall", "RECALL-BRIEFING-TEXT"))) as (port, _r):
            run = _run_hook(RECALL, tmp_path, port, sid=sid)
        assert run.returncode == 0, run.stderr
        assert "RECALL-BRIEFING-TEXT" in run.stdout

    def test_a_sub_agent_sends_nothing(self, tmp_path):
        sid = "recall-agent"
        with _stub(_routes(sid, _section_response("recall", "RECALL-BRIEFING-TEXT"))) as (port, requests):
            run = _run_hook(RECALL, tmp_path, port, sid=sid, envelope={"agent_id": "agent-7"})
        assert run.returncode == 0, run.stderr
        assert _bundles(requests) == []
        assert run.stdout == ""

    def test_it_is_not_gated_by_prompt_length(self, tmp_path):
        sid = "recall-short"
        with _stub(_routes(sid, _section_response("recall", "RECALL-BRIEFING-TEXT"))) as (port, requests):
            run = _run_hook(RECALL, tmp_path, port, sid=sid, prompt="ok")
        assert len(_bundles(requests)) == 1
        assert "RECALL-BRIEFING-TEXT" in run.stdout

    def test_the_second_turn_prints_nothing_when_the_server_has_already_briefed(self, tmp_path):
        sid = "recall-twice"
        answers = iter([_section_response("recall", "RECALL-BRIEFING-TEXT"), _section_response("recall", "")])
        with _stub(_routes(sid, lambda body: next(answers))) as (port, requests):
            first = _run_hook(RECALL, tmp_path, port, sid=sid)
            second = _run_hook(RECALL, tmp_path, port, sid=sid)
        assert "RECALL-BRIEFING-TEXT" in first.stdout
        assert second.stdout.strip() == ""
        assert len(_bundles(requests)) == 2, "the once-per-session decision is the server's, not the hook's"

    def test_the_hook_does_not_write_the_briefed_flag_itself(self, tmp_path):
        sid = "recall-flag"
        path = _seed_cache(tmp_path, sid, mode="conversation", recall_briefed=False)
        with _stub(_routes(sid, _section_response("recall", "RECALL-BRIEFING-TEXT"))) as (port, requests):
            _run_hook(RECALL, tmp_path, port, sid=sid)
        assert json.loads(path.read_text())["recall_briefed"] is False
        assert not [r for r in requests if r["path"] == "/recall"]


class TestShortPrompt:
    @pytest.mark.parametrize("script", [ALWAYS_ON, METHODOLOGY])
    def test_a_prompt_under_ten_characters_sends_nothing_and_prints_nothing(self, tmp_path, script):
        sid = "short-sid"
        with _stub(_routes(sid, _section_response(SECTION_OF[script], "SHOULD-NOT-PRINT"))) as (port, requests):
            run = _run_hook(script, tmp_path, port, sid=sid, prompt="fix it")
        assert run.returncode == 0, run.stderr
        assert _bundles(requests) == []
        assert run.stdout == ""

    @pytest.mark.parametrize("script", [ALWAYS_ON, METHODOLOGY])
    def test_a_long_enough_prompt_does_send(self, tmp_path, script):
        sid = "short-long"
        with _stub(_routes(sid, _section_response(SECTION_OF[script], "SHOULD-PRINT"))) as (port, requests):
            run = _run_hook(script, tmp_path, port, sid=sid, prompt="0123456789")
        assert len(_bundles(requests)) == 1
        assert "SHOULD-PRINT" in run.stdout

    @pytest.mark.parametrize("script", SECTION_HOOKS)
    def test_an_envelope_with_no_session_id_prints_nothing_and_sends_nothing(self, tmp_path, script):
        with _stub(_routes("x", _section_response(SECTION_OF[script], "SHOULD-NOT-PRINT"))) as (port, requests):
            run = _run_hook(script, tmp_path, port, sid="")
        assert run.returncode == 0
        assert run.stdout == ""
        assert _bundles(requests) == []


class TestDaemonDown:
    @pytest.mark.parametrize("script", SECTION_HOOKS)
    def test_a_section_hook_exits_zero_with_empty_stdout_when_the_daemon_is_down(self, tmp_path, script):
        run = _run_hook(script, tmp_path, free_port())
        assert run.returncode == 0, run.stderr
        assert run.stdout == ""

    @pytest.mark.parametrize("script", SECTION_HOOKS)
    @pytest.mark.parametrize("error", [True, "neo4j unreachable"])
    def test_a_section_hook_prints_nothing_for_an_error_response(self, tmp_path, script, error):
        sid = "down-error"
        bundle = _section_response(SECTION_OF[script], "MUST-NOT-APPEAR", error=error)
        with _stub(_routes(sid, bundle)) as (port, _requests):
            run = _run_hook(script, tmp_path, port, sid=sid)
        assert run.returncode == 0, run.stderr
        assert run.stdout == ""

    def test_the_ranked_hook_prints_its_degrade_line_when_the_daemon_is_down(self, tmp_path):
        run = _run_hook(RANKED, tmp_path, free_port())
        assert run.returncode == 0, run.stderr
        assert "[Writ: server unavailable, proceeding without rules]" in run.stdout

    def test_the_ranked_hook_prints_its_degrade_line_for_an_error_response(self, tmp_path):
        sid = "down-ranked-error"
        bundle = _section_response("ranked", "MUST-NOT-APPEAR", error=True)
        with _stub(_routes(sid, bundle)) as (port, _requests):
            run = _run_hook(RANKED, tmp_path, port, sid=sid)
        assert run.returncode == 0, run.stderr
        assert "[Writ: query failed, proceeding without rules]" in run.stdout
        assert "MUST-NOT-APPEAR" not in run.stdout


class TestSkipped:
    @pytest.mark.parametrize("script", SECTION_HOOKS)
    def test_a_skipped_response_prints_nothing(self, tmp_path, script):
        sid = "skip-sid"
        bundle = _section_response(SECTION_OF[script], "", skipped=True)
        with _stub(_routes(sid, bundle)) as (port, _requests):
            run = _run_hook(script, tmp_path, port, sid=sid)
        assert run.returncode == 0, run.stderr
        assert run.stdout.strip() == ""
