"""Control-flow regression tests; real provider/kernel evidence is tested separately."""
from uuid import uuid4
import pytest
from temporalio.exceptions import ApplicationError

from app.services.change_sets import build_agent_change_set_evidence, _require_evidence, ValidationRequired
from app.workflows.agent_v2 import McadAgentWorkflowV2


def row(gate, mode, outcome):
    return {"id": uuid4(), "gate": gate, "mode": mode, "outcome": outcome,
            "evidence_hash": "a" * 64, "evidence": {"issues": ["four holes required; only one measured"] if outcome != "passed" else []}}


def test_known_visual_design_failure_cannot_be_summarized_as_zero_issue_pass():
    validation, _ = build_agent_change_set_evidence([
        row("geometry", "required", "passed"), row("visual", "advisory", "failed")])
    assert validation["status"] == "failed"
    assert validation["issue_count"] == 1


def test_dfm_advisory_issue_is_a_warning_in_the_overall_summary():
    validation, _ = build_agent_change_set_evidence([
        row("geometry", "required", "passed"), row("dfm", "advisory", "failed")])
    assert validation["status"] == "warning"
    assert validation["issue_count"] == 1


@pytest.mark.asyncio
async def test_legacy_zero_issue_summary_does_not_bypass_failed_design_evidence():
    with pytest.raises(ValidationRequired):
        await _require_evidence(None, {"validation_summary": {"status": "passed", "issue_count": 0,
            "gates": [{"gate": "visual", "mode": "advisory", "outcome": "failed"}]}})


class GateRun(McadAgentWorkflowV2):
    def __init__(self, *, visual=("passed",), dfm=("passed",)):
        super().__init__()
        self._validation_repair_v2 = True
        self.calls = []
        self.visual = iter(visual)
        self.dfm = iter(dfm)

    async def _activity(self, name, payload, **kwargs):
        self.calls.append((name, payload, kwargs))
        if name == "agent_v2.render_visual":
            return {"renders": [], "attempt_id": "render", "step_id": "render-step", "runtime_provenance": {}}
        if name in {"agent_v2.judge_visual", "agent_v2.validate_dfm"}:
            outcome = next(self.visual if name.endswith("judge_visual") else self.dfm)
            return {"outcome": outcome, "evidence_id": f"e-{len(self.calls)}", "attempt_id": "attempt",
                    "report": {"issues": ["design mismatch"], "judgment": {"issues": ["missing holes"], "suggestions": ["restore all four holes"]},
                               "violations": [{"rule_id": "wall", "message": "thin wall", "suggestion": "increase unconstrained wall"}]}}
        if name in {"agent_v2.repair_operations", "agent_v2.repair_source"}:
            return {"source_id": "repaired", "source_hash": "new", "source_code": "{}",
                    "repair_step_key": payload.get("repair_step_key", "repair"), "signature": "repair-signature"}
        if name == "agent_v2.execute_freecad":
            return {"staging_manifest_id": "new-manifest"}
        if name == "agent_v2.validate_geometry":
            # This unit harness must supply the strict activity wire contract
            # consumed by the merged Geometry IR / feature-evidence layer.
            from tests.test_cad_intelligence_layers import _sample_report
            return {"outcome": "passed", "evidence_id": "geometry",
                    "report": _sample_report().model_dump(mode="json")}
        raise AssertionError(name)


def arguments(gate, mode="required", budget=1):
    return {"request": {"modeling_backend": "freecad"},
            "plan": {"modeling_backend": "freecad", "objective": "plate with four holes",
                     "design_brief": {}, "validation_policy": {
                         "geometry": {"mode": "required", "repair_budget": 2},
                         "visual": {"mode": "required", "repair_budget": 1},
                         "dfm": {"mode": "required", "repair_budget": 1},
                         gate: {"mode": mode, "repair_budget": budget}}},
            "candidate_build_id": "candidate", "plan_step_index": 0,
            "modeled": {"step": {"step_key": "model"}, "mode": "3d", "base_state": None,
                        "generated": {"source_id": "old", "source_hash": "old", "source_code": "{}"},
                        "executed": {"staging_manifest_id": "old-manifest"}, "repair_count": 0}}


@pytest.mark.asyncio
async def test_inconclusive_advisory_image_is_review_risk_without_redesign():
    run = GateRun(visual=("indeterminate",))
    result = await run._visual_gate(**arguments("visual", mode="advisory"), expected_dimensions={})
    assert result["visual"]["outcome"] == "indeterminate"
    assert not any("repair" in name for name, _, _ in run.calls)
    summary, _ = build_agent_change_set_evidence([
        row("geometry", "required", "passed"), row("visual", "advisory", "indeterminate")])
    assert summary["status"] == "warning" and summary["issue_count"] > 0


@pytest.mark.asyncio
async def test_inconclusive_required_image_remains_blocked():
    run = GateRun(visual=("indeterminate",))
    with pytest.raises(ApplicationError, match="indeterminate"):
        await run._visual_gate(**arguments("visual", mode="required"), expected_dimensions={})


@pytest.mark.asyncio
async def test_freecad_visual_budget_is_used_then_geometry_and_visual_are_rechecked():
    run = GateRun(visual=("failed", "passed"))
    result = await run._visual_gate(**arguments("visual", mode="advisory"), expected_dimensions={})
    assert result["visual"]["outcome"] == "passed"
    assert result["executed"]["staging_manifest_id"] == "new-manifest"
    names = [name for name, _, _ in run.calls]
    assert names.count("agent_v2.repair_operations") == 1
    assert names.count("agent_v2.validate_geometry") == 1
    assert names.count("agent_v2.judge_visual") == 2


@pytest.mark.asyncio
async def test_dfm_repair_revalidates_the_new_artifact_in_all_gates():
    run = GateRun(dfm=("failed", "passed"))
    result = await run._dfm_gate(**arguments("dfm"))
    assert result["dfm"]["outcome"] == "passed"
    assert result["executed"]["staging_manifest_id"] == "new-manifest"
    names = [name for name, _, _ in run.calls]
    assert names.count("agent_v2.repair_operations") == 1
    assert names.count("agent_v2.validate_geometry") == 1
    assert names.count("agent_v2.judge_visual") == 1
    assert names.count("agent_v2.validate_dfm") == 2
    manifests = [p["staging_manifest_id"] for n, p, _ in run.calls if n == "agent_v2.validate_dfm"]
    assert manifests == ["old-manifest", "new-manifest"]


@pytest.mark.asyncio
async def test_required_dfm_failure_after_budget_stops_with_failed_evidence():
    run = GateRun(dfm=("failed", "failed"))
    with pytest.raises(ApplicationError):
        await run._dfm_gate(**arguments("dfm"))
    assert [n for n, _, _ in run.calls].count("agent_v2.repair_operations") == 1


@pytest.mark.asyncio
async def test_visual_mismatch_cannot_succeed_after_advisory_budget_exhaustion():
    run = GateRun(visual=("failed", "failed"))
    with pytest.raises(ApplicationError):
        await run._visual_gate(**arguments("visual", mode="advisory"), expected_dimensions={})
    assert [n for n, _, _ in run.calls].count("agent_v2.repair_operations") == 1
