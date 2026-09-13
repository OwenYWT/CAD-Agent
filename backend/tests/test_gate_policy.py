import pytest

from app.validation.gate_policy import gate_blocks


@pytest.mark.parametrize("stage", ["workflow", "seal", "review"])
@pytest.mark.parametrize("gate", ["geometry", "visual", "dfm", "bom"])
def test_required_gate_needs_positive_evidence_at_every_boundary(stage, gate):
    assert not gate_blocks(gate, "required", "passed", stage=stage)
    for outcome in ("failed", "indeterminate", None):
        assert gate_blocks(gate, "required", outcome, stage=stage)


@pytest.mark.parametrize("stage", ["workflow", "seal", "review"])
def test_advisory_uncertainty_and_measured_visual_mismatch_are_distinct(stage):
    assert gate_blocks("visual", "advisory", "failed", stage=stage)
    assert not gate_blocks("visual", "advisory", "indeterminate", stage=stage)
    assert not gate_blocks("dfm", "advisory", "failed", stage=stage)
    assert gate_blocks("dfm", "advisory", None, stage=stage)


def test_old_workflow_replay_exception_never_changes_final_seal_or_review():
    assert not gate_blocks("visual", "advisory", "failed", stage="workflow", legacy_visual_workflow=True)
    for stage in ("seal", "review"):
        assert gate_blocks("visual", "advisory", "failed", stage=stage, legacy_visual_workflow=True)


def test_disabled_gate_cannot_claim_evidence_and_unknown_is_not_passed():
    assert not gate_blocks("visual", "disabled", None)
    assert gate_blocks("visual", "disabled", "passed")
    with pytest.raises(ValueError):
        gate_blocks("geometry", "required", "unknown")
