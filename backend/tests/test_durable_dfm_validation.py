import hashlib
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.validation.dfm_policy_snapshot import DFMPolicySnapshot, FrozenDFMRule
from app.validation.durable_dfm import (
    DFMViolationEvidence,
    DurableDFMReport,
    indeterminate_dfm_report,
)
from sandbox.dfm_validation import evaluate_dfm


def _policy(tenant_id) -> DFMPolicySnapshot:
    return DFMPolicySnapshot(
        schema_version="dfm-policy-snapshot.v1",
        tenant_id=tenant_id,
        process="FDM",
        material="PLA",
        rule_set_versions={"default_fdm": "1"},
        rules=(
            FrozenDFMRule(
                id="fdm_max_size",
                process="FDM",
                category="size",
                check_type="geometric",
                threshold_max=15,
                unit="mm",
                severity="critical",
            ),
        ),
        knowledge_constraints={},
        source="builtin-default",
    )


def test_dfm_policy_hash_matches_canonical_bytes():
    tenant_id = uuid4()
    policy = _policy(tenant_id)
    assert policy.policy_hash == hashlib.sha256(policy.canonical_bytes()).hexdigest()
    assert json.loads(policy.canonical_bytes())["tenant_id"] == str(tenant_id)


def test_dfm_policy_snapshot_rejects_unknown_process_mapping():
    from app.validation.dfm_policy_snapshot import _PROCESS_MAP

    assert _PROCESS_MAP["injection_mold"] == "injection_mold"
    assert _PROCESS_MAP["die_casting"] == "die_casting"


def test_dfm_failed_requires_confirmed_finding():
    violation = DFMViolationEvidence(
        rule_id="fdm_max_size",
        category="size",
        severity="critical",
        actual_value=20,
        message="too large",
        source="geometric",
    )
    report = DurableDFMReport(
        schema_version="durable-dfm-report.v1",
        outcome="failed",
        process="FDM",
        material="PLA",
        policy_hash="a" * 64,
        metrics={"max_size_mm": 20},
        evaluated_rule_ids=("fdm_max_size",),
        violations=(violation,),
    )
    assert report.outcome == "failed"


def test_dfm_indeterminate_never_becomes_pass():
    report = indeterminate_dfm_report(
        process="FDM",
        material="PLA",
        policy_hash="b" * 64,
        issue="runtime_unavailable",
    )
    assert report.outcome == "indeterminate"


def test_dfm_timeout_remains_indeterminate():
    report = indeterminate_dfm_report(
        process="FDM",
        material="PLA",
        policy_hash="d" * 64,
        issue="execution_timeout",
    )
    assert report.outcome == "indeterminate"
    assert report.issues == ("execution_timeout",)


def test_dfm_pass_rejects_blocking_violation():
    with pytest.raises(ValidationError, match="passed DFM"):
        DurableDFMReport(
            schema_version="durable-dfm-report.v1",
            outcome="passed",
            process="FDM",
            material="PLA",
            policy_hash="c" * 64,
            metrics={},
            violations=(
                DFMViolationEvidence(
                    rule_id="r",
                    category="size",
                    severity="warning",
                    message="issue",
                    source="geometric",
                ),
            ),
        )


def test_dfm_sandbox_rejects_changed_policy(tmp_path):
    policy = _policy(uuid4())
    policy_path = tmp_path / "policy.json"
    changed = json.loads(policy.canonical_bytes())
    changed["material"] = "ABS"
    policy_path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        evaluate_dfm(
            tmp_path / "missing.stl",
            policy_path,
            expected_policy_hash=policy.policy_hash,
        )


def test_dfm_unevaluated_blocking_rule_is_indeterminate(tmp_path, monkeypatch):
    policy = DFMPolicySnapshot(
        schema_version="dfm-policy-snapshot.v1",
        tenant_id=uuid4(),
        process="FDM",
        material="PLA",
        rule_set_versions={"tenant": "3"},
        rules=(
            FrozenDFMRule(
                id="fdm_feature",
                process="FDM",
                category="feature",
                check_type="heuristic",
                severity="warning",
            ),
        ),
        knowledge_constraints={},
        source="tenant-postgres",
    )
    policy_path = tmp_path / "policy.json"
    policy_path.write_bytes(policy.canonical_bytes())
    monkeypatch.setattr(
        "sandbox.dfm_validation._metrics",
        lambda _path: (
            {
                "is_watertight": True,
                "face_count": 12,
                "surface_area_mm2": 100,
                "volume_mm3": 100,
                "max_size_mm": 10,
                "overhang_ratio": 0,
                "material_ratio": 1,
                "min_wall_thickness_mm": 4,
            },
            None,
        ),
    )
    report = DurableDFMReport.model_validate(
        evaluate_dfm(
            tmp_path / "model.stl",
            policy_path,
            expected_policy_hash=policy.policy_hash,
        )
    )
    assert report.outcome == "indeterminate"
    assert report.issues == ("unevaluated_rules:fdm_feature",)


@pytest.mark.parametrize(
    ("maximum", "expected"),
    [(15, "passed"), (5, "failed")],
)
def test_dfm_deterministic_pass_and_failure(tmp_path, monkeypatch, maximum, expected):
    policy = _policy(uuid4()).model_copy(
        update={
            "rules": (
                FrozenDFMRule(
                    id="fdm_max_size",
                    process="FDM",
                    category="size",
                    check_type="geometric",
                    threshold_max=maximum,
                    unit="mm",
                    severity="critical",
                ),
            )
        }
    )
    policy_path = tmp_path / "policy.json"
    policy_path.write_bytes(policy.canonical_bytes())
    monkeypatch.setattr(
        "sandbox.dfm_validation._metrics",
        lambda _path: (
            {
                "is_watertight": True,
                "face_count": 12,
                "surface_area_mm2": 100,
                "volume_mm3": 100,
                "max_size_mm": 10,
                "overhang_ratio": 0,
                "material_ratio": 1,
                "min_wall_thickness_mm": 4,
            },
            None,
        ),
    )
    report = DurableDFMReport.model_validate(
        evaluate_dfm(
            tmp_path / "model.stl",
            policy_path,
            expected_policy_hash=policy.policy_hash,
        )
    )
    assert report.outcome == expected
