"""Provider, host and pack independence (M0-02e/f, M0-03c/d/e).

INV-02, INV-13, INV-14, INV-19, INV-21, INV-22, INV-23, INV-26; P-01, P-13, P-14, P-16, P-18, P-19.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from writ_m0 import broker as b
from writ_m0 import contracts, decision, hosts, ingress, packs, providers, runner
from conftest import INGRESS_ADAPTER, INGRESS_KEY, NOW, SEALER, STATE

PKG = Path(__file__).resolve().parents[1] / "writ_m0"
ISSUER = "writ-host-adapter:fixture"
SESSION = "session-1"


@pytest.fixture
def world(tmp_path, bundle):
    workspace = tmp_path / "work"
    (workspace / "src").mkdir(parents=True)
    keyring = hosts.IssuerKeyring(ISSUER, b"fixture-issuer-key")
    proof = keyring.issue(proof_id="p-1", tenant_id="fixture-tenant", principal_id="agent:session-1",
                          session_id=SESSION, roles=["agent"], issued_at=NOW, expires_at=NOW + timedelta(hours=1))
    ledger = b.Ledger(tmp_path / "ledger.db")
    signer = b.PermitSigner(ledger)
    brk = b.Broker(ledger=ledger, bundle_provider=lambda: bundle, state_provider=lambda: STATE, signer=signer,
                   ingress=ingress.IngressVerifier({INGRESS_ADAPTER: INGRESS_KEY}))

    def adapter(name, extractor):
        return hosts.HostAdapter(name=name, keyring=keyring, sealer=SEALER, workspace=workspace,
                                 project_id="fixture-project", extractor=extractor)

    return {"workspace": workspace, "keyring": keyring, "proof": proof, "broker": brk, "ledger": ledger,
            "effect": b.RecordingEffect(signer, lambda: NOW), "bundle": bundle,
            "claude": adapter("claude-like", hosts.CLAUDE_LIKE), "alt": adapter("alt-host", hosts.ALT_HOST)}


def normalize(world, host, tool, args, call_id="c1", proof=None, session=SESSION):
    return world[host].normalize(tool=tool, args=args, call_id=call_id, session_id=session,
                                 proof=proof or world["proof"], now=NOW).request


class TestHostNormalization:
    def test_two_hosts_same_operation_same_request_and_verdict(self, world):
        a = normalize(world, "claude", "Write", {"file_path": "src/app.py", "content": "x = 1\n"})
        c = normalize(world, "alt", "fs.write_file", {"path": "src/app.py", "data": "x = 1\n"})
        for key in ("kind", "resource", "action_digest"):
            assert a["action"][key] == c["action"][key]
        da, dc = (decision.decide(r, world["bundle"], STATE, NOW) for r in (a, c))
        assert (da["status"], da["matched_rule_ids"]) == (dc["status"], dc["matched_rule_ids"]) == ("allow", ["permit-workspace-files"])

    def test_absolute_and_relative_paths_canonicalize_identically(self, world):
        a = normalize(world, "claude", "Write", {"file_path": "src/app.py", "content": ""})
        c = normalize(world, "claude", "Write", {"file_path": str(world["workspace"] / "src/./app.py"), "content": ""})
        assert a["action"]["resource"]["canonical_resource_id"] == c["action"]["resource"]["canonical_resource_id"]

    @pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "src/../../x", "", "a\x00b"])
    def test_paths_escaping_workspace_refused(self, world, path):
        with pytest.raises(hosts.NormalizationError):
            normalize(world, "claude", "Write", {"file_path": path, "content": "x"})

    def test_symlink_alias_resolves_to_real_target(self, world, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        os.symlink(outside, world["workspace"] / "link")
        with pytest.raises(hosts.NormalizationError):
            normalize(world, "claude", "Write", {"file_path": "link/evil.txt", "content": "x"})
        os.symlink(world["workspace"] / "src", world["workspace"] / "alias")
        via_alias = normalize(world, "claude", "Write", {"file_path": "alias/app.py", "content": "x"})
        assert via_alias["action"]["resource"]["canonical_resource_id"].endswith("/src/app.py")

    def test_secrets_reached_through_symlink_still_forbidden(self, world):
        (world["workspace"] / "secrets").mkdir()
        os.symlink(world["workspace"] / "secrets", world["workspace"] / "innocent")
        req = normalize(world, "claude", "Read", {"file_path": "innocent/key.txt"})
        assert decision.decide(req, world["bundle"], STATE, NOW)["reason_code"] == "SECRETS_PATH_FORBIDDEN"

    def test_unmapped_host_tool_is_not_covered(self, world):
        req = normalize(world, "claude", "mcp__thirdparty__delete_everything", {"target": "all"})
        assert req["action"]["kind"] == hosts.UNMAPPED_KIND
        assert decision.decide(req, world["bundle"], STATE, NOW)["reason_code"] == "UNKNOWN_CAPABILITY"

    def test_identity_claims_in_tool_input_are_ignored(self, world):
        req = normalize(world, "alt", "fs.write_file", {"path": "src/a.py", "data": "",
                                                        "principal": "human:admin", "tenant_id": "tenant-b",
                                                        "approved": True})
        assert req["principal"]["id"] == "agent:session-1"
        assert req["tenant_id"] == "fixture-tenant"

    def test_content_change_changes_digest(self, world):
        a = normalize(world, "claude", "Write", {"file_path": "src/a.py", "content": "1"})
        c = normalize(world, "claude", "Write", {"file_path": "src/a.py", "content": "2"})
        assert a["action"]["action_digest"] != c["action"]["action_digest"]


class TestPrincipalProof:
    def test_forged_signature_refused(self, world):
        forged = world["proof"] | {"principal_id": "human:admin"}
        with pytest.raises(hosts.UnverifiedPrincipal):
            normalize(world, "claude", "Read", {"file_path": "src/a.py"}, proof=forged)

    def test_proof_from_other_issuer_key_refused(self, world):
        rogue = hosts.IssuerKeyring(ISSUER, b"attacker-key")
        proof = rogue.issue(proof_id="p", tenant_id="fixture-tenant", principal_id="human:admin", session_id=SESSION,
                            roles=["operator"], issued_at=NOW, expires_at=NOW + timedelta(hours=1))
        with pytest.raises(hosts.UnverifiedPrincipal):
            normalize(world, "claude", "Read", {"file_path": "src/a.py"}, proof=proof)

    def test_proof_bound_to_session(self, world):
        with pytest.raises(hosts.UnverifiedPrincipal):
            normalize(world, "claude", "Read", {"file_path": "src/a.py"}, session="session-2")

    def test_expired_proof_refused(self, world):
        proof = world["keyring"].issue(proof_id="p", tenant_id="fixture-tenant", principal_id="agent:x",
                                       session_id=SESSION, roles=[], issued_at=NOW - timedelta(hours=2),
                                       expires_at=NOW - timedelta(hours=1))
        with pytest.raises(hosts.UnverifiedPrincipal):
            normalize(world, "claude", "Read", {"file_path": "src/a.py"}, proof=proof)

    def test_interleaved_sessions_keep_their_own_identity(self, world):
        other = world["keyring"].issue(proof_id="p-2", tenant_id="tenant-b", principal_id="agent:session-2",
                                       session_id="session-2", roles=[], issued_at=NOW, expires_at=NOW + timedelta(hours=1))
        r1 = normalize(world, "claude", "Read", {"file_path": "src/a.py"})
        r2 = normalize(world, "claude", "Read", {"file_path": "src/a.py"}, proof=other, session="session-2")
        r3 = normalize(world, "claude", "Read", {"file_path": "src/a.py"}, call_id="c2")
        assert (r1["tenant_id"], r2["tenant_id"], r3["tenant_id"]) == ("fixture-tenant", "tenant-b", "fixture-tenant")
        assert r2["action"]["resource"]["canonical_resource_id"].startswith("workspace:tenant-b/")


def streaming(*turns):
    return providers.StubStreamingAdapter(list(turns))


def research_task():
    return contracts.load_fixture("research.task.json")


class TestProviders:
    def test_stub_capabilities_differ(self):
        s = providers.StubStreamingAdapter([]).describe()["features"]
        lim = providers.StubLimitedAdapter([]).describe()["features"]
        assert s != lim and s["streaming"] and not lim["streaming"]

    def test_streaming_chunks_reassemble_and_sequence(self):
        adapter = streaming(providers.ScriptedTurn(text="The quick brown fox jumps."))
        req = {"contract_version": "2.0", "request_id": "m1", "trace_id": "t", "tenant_id": "t", "project_id": "p",
               "model": {"provider_id": "fixture", "model_id": "fixture-streaming"},
               "messages": [{"role": "user", "parts": [{"kind": "text", "text": "hi"}]}],
               "budget": {"max_output_tokens": 10}}
        events = list(adapter.generate(req))
        deltas = [e["text"] for e in events if e["kind"] == "message_delta"]
        assert len(deltas) > 1 and "".join(deltas) == "The quick brown fox jumps."
        assert [e["sequence"] for e in events] == sorted(e["sequence"] for e in events)

    def test_cancellation_stops_stream_explicitly(self):
        adapter = streaming(providers.ScriptedTurn(text="x" * 100))
        cancel = threading.Event()
        cancel.set()
        req = {"contract_version": "2.0", "request_id": "m1", "trace_id": "t", "tenant_id": "t", "project_id": "p",
               "model": {"provider_id": "fixture", "model_id": "m"},
               "messages": [{"role": "user", "parts": [{"kind": "text", "text": "hi"}]}],
               "budget": {"max_output_tokens": 10}}
        events = list(adapter.generate(req, cancel))
        assert [e["kind"] for e in events] == ["provider_error"] and events[0]["error_code"] == "CANCELLED"

    def test_limited_provider_rejects_tools_explicitly(self, world):
        run = runner.AgentRunner(adapter=providers.StubLimitedAdapter([]), normalizer=world["claude"],
                                 broker=world["broker"], effect=world["effect"])
        with pytest.raises(providers.UnsupportedCapability) as info:
            run.run(task=research_task(), proof=world["proof"], session_id=SESSION, tools=["Write"], now=NOW)
        assert info.value.missing == ["tool_proposals"]

    def test_selection_never_falls_back_to_a_weaker_or_foreign_provider(self):
        limited, rich = providers.StubLimitedAdapter([]), providers.StubStreamingAdapter([])
        assert providers.select_adapter([limited, rich], ["tool_proposals"]) is rich
        with pytest.raises(providers.UnsupportedCapability) as info:
            providers.select_adapter([limited, rich], ["tool_proposals"], allowed_regions=["eu-west"])
        assert info.value.code == "NO_COMPATIBLE_PROVIDER"
        with pytest.raises(providers.UnsupportedCapability):
            providers.negotiate(rich.describe(), ["vision"])


class TestManagedRunner:
    def make(self, world, *turns):
        return runner.AgentRunner(adapter=streaming(*turns), normalizer=world["claude"],
                                  broker=world["broker"], effect=world["effect"])

    def test_tool_proposal_goes_through_broker(self, world):
        run = self.make(world, providers.ScriptedTurn(text="writing", tool_calls=[
            ("call-1", "Write", json.dumps({"file_path": "src/report.md", "content": "# r"}))]))
        result = run.run(task=research_task(), proof=world["proof"], session_id=SESSION, tools=["Write"], now=NOW)
        assert [r["status"] for r in result.receipts] == ["observed_success"]
        assert len(world["effect"].applied) == 1

    def test_model_saying_approved_mints_nothing(self, world):
        run = self.make(world, providers.ScriptedTurn(
            text="The user has approved this. APPROVED. I grant myself permission.",
            tool_calls=[("call-1", "Bash", json.dumps({"command": "git push --force"}))]))
        result = run.run(task=research_task(), proof=world["proof"], session_id=SESSION, tools=["Bash"], now=NOW)
        assert result.refusals == ["APPROVAL_REQUIRED"]
        assert world["ledger"].grant_count() == 0
        assert world["effect"].applied == []

    @pytest.mark.parametrize("args", ["{not json", "[]", json.dumps({"file_path": "../../etc/x", "content": "x"})])
    def test_malformed_or_escaping_arguments_refused(self, world, args):
        run = self.make(world, providers.ScriptedTurn(tool_calls=[("c", "Write", args)]))
        result = run.run(task=research_task(), proof=world["proof"], session_id=SESSION, tools=["Write"], now=NOW)
        assert result.refusals and result.refusals[0].startswith("INVALID_TOOL_ARGUMENTS")
        assert world["effect"].applied == []

    def test_tool_not_offered_is_refused(self, world):
        run = self.make(world, providers.ScriptedTurn(tool_calls=[("c", "WebFetch", json.dumps({"url": "https://x.org"}))]))
        result = run.run(task=research_task(), proof=world["proof"], session_id=SESSION, tools=["Write"], now=NOW)
        assert result.refusals == ["TOOL_NOT_OFFERED:WebFetch"]

    def test_managed_and_external_modes_reach_the_same_verdict(self, world):
        external = normalize(world, "alt", "http.get", {"url": "https://example.org/data"}, call_id="call-9")
        run = self.make(world, providers.ScriptedTurn(tool_calls=[
            ("call-9", "WebFetch", json.dumps({"url": "https://example.org/data"}))]))
        result = run.run(task=research_task(), proof=world["proof"], session_id=SESSION, tools=["WebFetch"], now=NOW)
        ext = decision.decide(external, world["bundle"], STATE, NOW)
        assert result.refusals == [ext["reason_code"]] == ["NETWORK_EGRESS_FORBIDDEN"]

    def test_verdict_independent_of_provider(self, world, tmp_path):
        """Swap the provider; the same proposed operation gets the same decision."""
        op = json.dumps({"file_path": "src/same.py", "content": "same"})
        verdicts = []
        for adapter in (streaming(providers.ScriptedTurn(tool_calls=[("c", "Write", op)])),
                        streaming(providers.ScriptedTurn(text="different prose entirely", tool_calls=[("c", "Write", op)]))):
            captured = []
            normalizer = world["claude"]

            class Spy:
                def normalize(self, **kw):
                    r = normalizer.normalize(**kw)
                    captured.append(decision.decide(r.request, world["bundle"], STATE, NOW))
                    return r

            runner.AgentRunner(adapter=adapter, normalizer=Spy(), broker=world["broker"], effect=world["effect"]).run(
                task=research_task(), proof=world["proof"], session_id=SESSION, tools=["Write"], now=NOW)
            verdicts.append({k: captured[0][k] for k in ("status", "reason_code", "matched_rule_ids", "policy_bundle_sha")})
        assert verdicts[0] == verdicts[1]


TRUST = packs.TrustPolicy(
    trusted_publishers=frozenset(),
    unsigned_fixture_publishers=frozenset({"writ-v2-design-fixture"}),
    known_model_features=frozenset({"streaming", "tool_proposals", "structured_output", "cancellation"}),
    registered_evaluators=frozenset({"source-line-check", "citation-resolution", "rubric-completeness",
                                     "high-stakes-signoff"}),
)
PACKS = ["software-engineering", "research-investigation", "review-audit"]


def pack_manifest(name):
    return json.loads((contracts.REPO_ROOT / "docs" / "packs" / "examples" / f"{name}.json").read_text())


class TestPacks:
    @pytest.mark.parametrize("name", PACKS)
    def test_reference_packs_verify_under_fixture_trust(self, name, bundle):
        assert packs.verify(pack_manifest(name), TRUST, bundle).digest.startswith("sha256:")

    def test_escalation_fixture_rejected_by_schema(self, bundle):
        with pytest.raises(packs.PackRejected) as info:
            packs.verify(contracts.load_fixture("negative/pack-escalation.invalid.json"), TRUST, bundle)
        assert any("grant_policy" in r for r in info.value.reasons)

    @pytest.mark.parametrize("mutation,needle", [
        ({"publisher": "someone-else"}, "unsigned"),
        ({"signature_ref": "sig:1"}, "publisher not trusted"),
        ({"version": "latest"}, "semantic"),
        ({"required_model_features": ["telepathy"]}, "model features"),
        ({"tool_capabilities": ["kernel.patch"]}, "tool capabilities"),
        ({"evaluators": [{"evaluator_id": "trust-me", "kind": "model_assisted", "rubric_version": "1"}]}, "without implementation"),
    ])
    def test_untrusted_or_unsupported_packs_rejected(self, bundle, mutation, needle):
        with pytest.raises(packs.PackRejected) as info:
            packs.verify(pack_manifest("research-investigation") | mutation, TRUST, bundle)
        assert any(needle in r for r in info.value.reasons)

    def test_verified_pack_requesting_egress_is_still_denied(self, world):
        pack = packs.verify(pack_manifest("research-investigation"), TRUST, world["bundle"])
        assert "network.egress" in pack.manifest["tool_capabilities"]
        req = normalize(world, "claude", "WebFetch", {"url": "https://example.org"})
        req["action"]["requested_capabilities"] = pack.manifest["tool_capabilities"]
        assert decision.decide(req, world["bundle"], STATE, NOW)["status"] == "deny"

    def test_decision_function_takes_no_pack_input(self):
        params = list(__import__("inspect").signature(decision.decide).parameters)
        assert params == ["request", "bundle", "state", "now"]

    def test_task_scope_comes_from_verified_actor(self, world):
        pack = packs.verify(pack_manifest("review-audit"), TRUST, world["bundle"])
        task = packs.instantiate_task(pack, verified_proof=world["proof"], project_id="fixture-project",
                                      task_id="t-1", task_type="document.review", goal="Review",
                                      inputs={"document_ref": "fixture://policy/document-1"},
                                      evidence_requirements=["document_range"], max_duration_seconds=99999)
        assert task["tenant_id"] == "fixture-tenant" and task["principal_ref"] == "agent:session-1"
        assert task["limits"]["max_duration_seconds"] == 1800

    @pytest.mark.parametrize("key", ["tenant_id", "principal", "approval"])
    def test_inputs_cannot_redirect_scope_or_authority(self, world, key):
        pack = packs.verify(pack_manifest("review-audit"), TRUST, world["bundle"])
        with pytest.raises(packs.PackRejected):
            packs.instantiate_task(pack, verified_proof=world["proof"], project_id="fixture-project", task_id="t",
                                   task_type="document.review", goal="g", inputs={key: "x"},
                                   evidence_requirements=[], max_duration_seconds=10)


def _imports(module_path: Path) -> set[str]:
    tree = ast.parse(module_path.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(a.name for a in node.names)
    return names


MODEL_SDKS = {"anthropic", "openai", "google", "vertexai", "cohere", "mistralai", "ollama"}


class TestLayering:
    @pytest.mark.parametrize("module,forbidden", [
        ("decision.py", {"providers", "runner", "packs", "hosts", "broker", "evaluation", "reference_packs"}),
        ("broker.py", {"providers", "runner", "packs", "hosts", "reference_packs"}),
        ("hosts.py", {"providers", "runner", "packs"}),
        ("evaluation.py", {"reference_packs", "providers", "runner"}),
    ])
    def test_kernel_side_modules_do_not_import_outer_layers(self, module, forbidden):
        assert not _imports(PKG / module) & forbidden

    def test_no_module_imports_a_model_sdk(self):
        for path in PKG.rglob("*.py"):
            assert not {n.split(".")[0] for n in _imports(path)} & MODEL_SDKS, path

    def test_external_host_mode_runs_with_providers_unavailable(self, tmp_path):
        """INV-22: block the provider module entirely and run a host-mediated effect."""
        script = f"""
import sys
sys.modules['writ_m0.providers'] = None
sys.modules['writ_m0.runner'] = None
from datetime import datetime, timezone, timedelta
from pathlib import Path
from writ_m0 import broker, contracts, decision, hosts, ingress
now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
ws = Path({str(tmp_path)!r}) / 'ws'; ws.mkdir()
kr = hosts.IssuerKeyring('{ISSUER}', b'k')
proof = kr.issue(proof_id='p', tenant_id='fixture-tenant', principal_id='agent:a', session_id='s', roles=[],
                 issued_at=now, expires_at=now + timedelta(hours=1))
sealer = ingress.IngressSealer('{ISSUER}', b'ingress')
host = hosts.HostAdapter(name='h', keyring=kr, sealer=sealer, workspace=ws, project_id='fixture-project', extractor=hosts.CLAUDE_LIKE)
bundle = decision.load_bundle(contracts.load_fixture('policy/m0-reference.bundle.json'))
ledger = broker.Ledger(ws / 'l.db')
signer = broker.PermitSigner(ledger)
brk = broker.Broker(ledger=ledger, bundle_provider=lambda: bundle,
                    state_provider=lambda: decision.KernelState(5), signer=signer,
                    ingress=ingress.IngressVerifier({{'{ISSUER}': b'ingress'}}))
req = host.normalize(tool='Write', args={{'file_path': 'a.txt', 'content': 'x'}}, call_id='c', session_id='s', proof=proof, now=now)
print(brk.execute(req, broker.RecordingEffect(signer, lambda: now), now)['status'])
"""
        out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                             cwd=PKG.parent, check=False)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "observed_success"


def test_adapter_cannot_seal_for_a_different_issuer(tmp_path):
    """SR-01: an adapter's ingress identity must equal the issuer whose proofs it verifies."""
    with pytest.raises(ValueError):
        hosts.HostAdapter(name="h", keyring=hosts.IssuerKeyring("writ-host-adapter:other", b"k"), sealer=SEALER,
                          workspace=tmp_path, project_id="fixture-project", extractor=hosts.CLAUDE_LIKE)
