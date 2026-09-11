"""Keep remote geometric evidence valid for actual durable/native plans."""
import pytest

from app.agent.durable_plan import AgentPlan
from app.geometry_ir.planner import build_geometry_plan
from app.validation.verification.evaluator import build_verification_targets
from tests.test_durable_agent_plan import _plan


def _with_step(description, step_key="model-main") -> AgentPlan:
    data = _plan().model_dump(mode="json")
    data["modeling_backend"] = "freecad"
    data["steps"][0].update(description=description, step_key=step_key)
    data["design_brief"]["critical_dimensions"] = [
        {"name": "length", "value": 80, "unit": "mm", "reason": "overall"},
    ]
    return AgentPlan.model_validate(data)


@pytest.mark.parametrize("description", ["x" * 4000, "生成参数化主支架"], ids=["long-description", "chinese-description"])
def test_valid_durable_step_uses_its_stable_key_for_all_evidence(description):
    plan = _with_step(description)
    geometry = build_geometry_plan(plan)
    targets = build_verification_targets(plan)
    assert geometry.features[0].feature_id == "model-main"
    assert all(t.feature_id == "model-main" for t in targets)
    assert {r.target_id for r in geometry.references} == {t.target_id for t in targets}
    assert geometry.features[0].parameters["description"] == description


def test_maximum_step_key_has_bounded_derived_sketch_identifiers():
    plan = _with_step("pad " * 30, "model_" + "x" * 114)
    geometry = build_geometry_plan(plan)
    assert geometry.features[0].feature_id == plan.steps[0].step_key
    assert len(geometry.sketches[0].sketch_id) <= 120
    assert len(geometry.sketches[0].entities[0].entity_id) <= 120
    assert geometry == build_geometry_plan(plan)


def test_legacy_long_duplicate_and_chinese_features_keep_closed_references():
    plan = {
        "objective": "legacy planning",
        "features": ["base " * 80, "base " * 80, "孔", "孔"],
        "dimensions": {"length": 80},
    }
    geometry = build_geometry_plan(plan)
    ids = {feature.feature_id for feature in geometry.features}
    targets = build_verification_targets(plan)
    assert len(ids) == 4
    assert all(len(identifier) <= 120 for identifier in ids)
    assert all(target.feature_id in ids for target in targets)
    assert geometry == build_geometry_plan(plan)


def test_long_advisory_target_ids_are_bounded_and_unique():
    plan = _with_step("生成主模型").model_dump(mode="json")
    plan["design_brief"]["acceptance_criteria"] = ["watertight " * 50] * 2
    geometry = build_geometry_plan(plan)
    targets = build_verification_targets(plan)
    ids = [target.target_id for target in targets]
    assert len(ids) == len(set(ids))
    assert all(len(identifier) <= 120 for identifier in ids)
    assert {r.target_id for r in geometry.references} == set(ids)
