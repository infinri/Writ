"""Cross-domain evaluation through one core evaluator (M0-02c, M0-02d; INV-24, INV-25)."""
from __future__ import annotations

import ast
import inspect

import pytest

from writ_m0 import canonical, contracts, evaluation, reference_packs

TENANT = "fixture-tenant"


def load(task: str):
    t = contracts.load_fixture(f"{task}.task.json")
    sources = contracts.load_fixture(f"domains/{task}/sources.json")
    return (t, contracts.load_fixture(f"domains/{task}/artifact.json"),
            contracts.load_fixture(f"domains/{task}/evidence.json"),
            evaluation.FixtureCatalog(sources, TENANT, "fixture-project"))


def checks_for(task: str, catalog):
    if task == "code-review":
        return reference_packs.SOFTWARE_ENGINEERING
    if task == "research":
        return reference_packs.RESEARCH
    return reference_packs.review_audit(catalog.resolve("fixture://rubric/1", TENANT, "fixture-project"))


def run(task, evidence=None, signoffs=(), tenant_catalog=None):
    t, artifact, items, catalog = load(task)
    catalog = tenant_catalog or catalog
    return evaluation.evaluate(
        task=t, artifact=artifact, evidence=items if evidence is None else evidence,
        catalog=catalog, kind_checks=checks_for(task, catalog), rubric_id=f"{task}-rubric",
        rubric_version="1", evaluation_id=f"eval-{task}", signoffs=signoffs)


class TestPositiveDomains:
    def test_code_review_passes_mechanical_evidence(self):
        record = run("code-review")
        assert record["status"] == "pass"

    def test_research_with_contradictory_sources_passes_and_records_counterevidence(self):
        record = run("research")
        assert record["status"] == "pass"
        assert {"criterion_id": "requirement:counterevidence", "status": "pass", "evidence_refs": []} in record["criteria"]

    def test_document_review_needs_human_signoff(self):
        assert run("document-review")["status"] == "needs_human_review"

    def test_document_review_passes_with_authenticated_signoff_bound_to_artifact(self):
        artifact = contracts.load_fixture("domains/document-review/artifact.json")
        grant = evaluation.SignoffGrant("g-1", TENANT, "human:reviewer", canonical.digest(artifact))
        assert run("document-review", signoffs=[grant])["status"] == "pass"

    def test_signoff_for_a_different_artifact_does_not_count(self):
        grant = evaluation.SignoffGrant("g-1", TENANT, "human:reviewer", canonical.digest({"other": 1}))
        assert run("document-review", signoffs=[grant])["status"] == "needs_human_review"

    @pytest.mark.parametrize("task", ["code-review", "research", "document-review"])
    def test_every_domain_produces_a_valid_evaluation_record(self, task):
        contracts.validate("evaluation-record", run(task))


class TestNegativeDomains:
    def test_fabricated_citations_fail_despite_claimed_verified_status(self):
        fabricated = contracts.load_fixture("domains/negative/fabricated-citation.evidence.json")
        assert all(e["verification_status"] == "verified" for e in fabricated)
        record = run("research", evidence=fabricated)
        assert record["status"] == "fail"
        failed = {c["criterion_id"] for c in record["criteria"] if c["status"] == "fail"}
        assert failed == {"evidence:ev-neg-1", "evidence:ev-neg-2"}

    def test_missing_counterevidence_is_incomplete_not_pass(self):
        record = run("research", evidence=contracts.load_fixture("domains/negative/insufficient-evidence.evidence.json"))
        assert record["status"] == "incomplete"

    def test_wrong_document_range_fails(self):
        items = contracts.load_fixture("domains/document-review/evidence.json")
        items[0] = items[0] | {"citation_locator": "chars:0-10"}
        assert run("document-review", evidence=items)["status"] == "fail"

    def test_counterevidence_kind_without_contradicting_stance_fails(self):
        items = contracts.load_fixture("domains/research/evidence.json")
        items[1] = items[1] | {"stance": "supports"}
        assert run("research", evidence=items)["status"] == "fail"

    def test_cross_tenant_evidence_rejected(self):
        items = [e | {"tenant_id": "other-tenant"} for e in contracts.load_fixture("domains/research/evidence.json")]
        assert run("research", evidence=items)["status"] == "fail"

    def test_catalog_scoped_to_other_tenant_resolves_nothing(self):
        sources = contracts.load_fixture("domains/research/sources.json")
        record = run("research", tenant_catalog=evaluation.FixtureCatalog(sources, "other-tenant", "fixture-project"))
        assert record["status"] == "fail"

    def test_unregistered_evidence_kind_is_not_silently_accepted(self):
        items = contracts.load_fixture("domains/research/evidence.json")
        items[0] = items[0] | {"evidence_kinds": ["dated_source", "peer_reviewed"]}
        assert run("research", evidence=items)["status"] == "fail"


class TestAggregation:
    def rec(self, status, reviewer):
        return {"status": status, "reviewer_type": reviewer}

    def test_model_assisted_pass_alone_is_not_verification(self):
        assert evaluation.aggregate_status([self.rec("pass", "model_assisted")]) == "needs_human_review"

    def test_unanimous_models_do_not_verify(self):
        records = [self.rec("pass", "model_assisted")] * 3
        assert evaluation.aggregate_status(records) == "needs_human_review"

    def test_any_failure_fails(self):
        assert evaluation.aggregate_status([self.rec("pass", "mechanical"), self.rec("fail", "model_assisted")]) == "fail"

    def test_verifying_pass(self):
        assert evaluation.aggregate_status([self.rec("pass", "evidence_engine"), self.rec("pass", "model_assisted")]) == "pass"


def test_core_evaluator_has_no_domain_imports():
    tree = ast.parse(inspect.getsource(evaluation))
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert "reference_packs" not in imported
    source = inspect.getsource(evaluation)
    for word in ("file_line", "diff", "citation_locator", "rubric_criterion"):
        assert word not in source


class TestProjectIsolation:
    """SR-05: evidence and sources are scoped to tenant AND project, not tenant alone."""

    def test_evidence_from_a_sibling_project_is_rejected(self):
        items = [e | {"project_id": "other-project"} for e in contracts.load_fixture("domains/research/evidence.json")]
        assert run("research", evidence=items)["status"] == "fail"

    def test_catalog_scoped_to_a_sibling_project_resolves_nothing(self):
        sources = contracts.load_fixture("domains/research/sources.json")
        record = run("research", tenant_catalog=evaluation.FixtureCatalog(sources, TENANT, "other-project"))
        assert record["status"] == "fail"

    def test_colliding_source_ids_resolve_to_their_own_project(self):
        """Two projects in one tenant use the same source_ref with different content."""
        ref = "fixture://sources/vendor-report-2026-03"
        own = contracts.load_fixture("domains/research/sources.json")
        foreign = {ref: {"published_at": own[ref]["published_at"],
                         "content": "Sibling project copy: Product X reduced median checkout latency by 90%."}}
        sibling = evaluation.FixtureCatalog(foreign, TENANT, "other-project")
        mine = evaluation.FixtureCatalog(own, TENANT, "fixture-project")
        assert sibling.resolve(ref, TENANT, "fixture-project") is None
        assert mine.resolve(ref, TENANT, "fixture-project").content == own[ref]["content"]
        items = contracts.load_fixture("domains/research/evidence.json")
        items[0] = items[0] | {"quoted_excerpt": "reduced median checkout latency by 90%", "content_digest": None}
        assert run("research", evidence=items, tenant_catalog=mine)["status"] == "fail"

    def test_evidence_contract_requires_project_scope(self):
        item = contracts.load_fixture("domains/research/evidence.json")[0]
        item.pop("project_id", None)
        assert contracts.violations("evidence", item)
