"""Configuration identity and dimensional correctness of engineering checks."""
import pytest
from copy import deepcopy
from uuid import uuid4

from app.dfm.models import DFMRule, StepAnalysisResult, StepDerivedMetrics
from app.dfm.rule_engine import DFMRuleEngine
from app.validation.design_analyzer import DFMGeometryResult
from app.dfm.configuration import freeze_rules, configuration_rules


@pytest.mark.parametrize("category,unit", [
    ("hole", "ratio"), ("wall_thickness", "ratio"),
    ("hole", "degree"), ("size", "ratio"), ("overhang", "mm"),
])
def test_rules_never_compare_measurements_in_incompatible_units(category, unit):
    rule = DFMRule(id="custom-rule", process="CNC", category=category,
                   check_type="geometric", threshold_max=10, unit=unit)
    step = StepAnalysisResult(derived_metrics=StepDerivedMetrics(
        min_hole_diameter=12, min_wall_thickness=9))
    geometry = DFMGeometryResult(min_wall_thickness=9, overhang_ratio=0.25,
        bounding_box={"x_min": 0, "x_max": 60})
    assert DFMRuleEngine()._get_actual_value(rule, geometry, step) is None


def test_hole_diameter_retains_its_actual_length_unit():
    rule = DFMRule(id="custom-diameter", process="CNC", category="hole",
                   check_type="geometric", threshold_min=1, unit="mm")
    step = StepAnalysisResult(derived_metrics=StepDerivedMetrics(min_hole_diameter=12))
    assert DFMRuleEngine()._get_actual_value(rule, DFMGeometryResult(), step) == 12


def test_configuration_hash_covers_disabled_state_thresholds_and_ownership():
    rule = DFMRule(id='size', process='CNC', category='size', check_type='geometric', threshold_max=1)
    owner = dict(tenant_id=uuid4(), principal_id=uuid4(), process='CNC')
    first = freeze_rules([rule], **owner)
    rule.enabled = False
    second = freeze_rules([rule], **owner)
    assert first['sha256'] != second['sha256']
    assert configuration_rules(first, **owner)[0].enabled
    rule.threshold_max = 1000
    assert freeze_rules([rule], **owner)['sha256'] != second['sha256']
    corrupted = deepcopy(first); corrupted['rules'][0]['threshold_max'] = 1000
    with pytest.raises(ValueError, match='integrity'):
        configuration_rules(corrupted, **owner)
    with pytest.raises(ValueError, match='tenant'):
        configuration_rules(first, **{**owner, 'tenant_id': uuid4()})
    with pytest.raises(ValueError, match='process'):
        configuration_rules(first, **{**owner, 'process': 'FDM'})


@pytest.mark.asyncio
async def test_frozen_custom_threshold_and_disabled_rule_are_reported_without_ambient_stores():
    rule = DFMRule(id='cnc_max_size', process='CNC', category='size', check_type='geometric', threshold_max=1)
    owner = dict(tenant_id=uuid4(), principal_id=uuid4(), process='CNC')
    old = freeze_rules([rule], **owner)
    rule.enabled = False
    new = freeze_rules([rule], **owner)
    engine = DFMRuleEngine()
    geometry = DFMGeometryResult(bounding_box={'x_min': 0, 'x_max': 60})
    violations, checks = await engine.evaluate_with_evidence(geometry, process='CNC', material='Aluminum', rule_configuration=old)
    assert len(violations) == 1 and checks[0]['status'] == 'violated'
    assert checks[0]['actual_value'] == 60 and checks[0]['threshold_max'] == 1
    violations, checks = await engine.evaluate_with_evidence(geometry, process='CNC', rule_configuration=new)
    assert not violations and checks[0]['status'] == 'disabled'
    assert checks[0]['actual_value'] is None


def test_native_generation_uses_the_same_dimensional_boundary_and_preserves_old_policy_identity():
    from sandbox.dfm_validation import _actual, _thresholds
    from app.validation.dfm_policy_snapshot import DFMPolicySnapshot
    assert _actual('wall_thickness', {'min_wall_thickness_mm': 9}, 'ratio') is None
    assert _actual('wall_thickness', {'min_wall_thickness_mm': 9}, 'mm') == 9
    rule = {'category': 'size', 'threshold_max': 1}
    assert _thresholds(rule, {'max_size': 1000}, configured_rules=True) == (None, 1)
    assert _thresholds(rule, {'max_size': 1000}) == (None, 1000)
    old = {'schema_version': 'dfm-policy-snapshot.v1', 'tenant_id': str(uuid4()),
           'process': 'CNC', 'material': 'steel', 'rule_set_versions': {}, 'rules': [],
           'knowledge_constraints': {}, 'source': 'builtin-default'}
    assert DFMPolicySnapshot.model_validate(old).model_dump(mode='json') == old


def test_queued_legacy_check_dispatch_keeps_its_original_serialized_identity():
    from app.models.workflow_requests import McadCheckRequest
    old = {key: str(uuid4()) for key in ('workflow_run_id', 'source_workflow_run_id',
        'source_revision_id', 'tenant_id', 'project_id', 'principal_id')}
    old.update(code='', description='', process=None, material=None, timeout_seconds=120)
    assert McadCheckRequest.model_validate(old).temporal_payload() == old
