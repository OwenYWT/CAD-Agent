from uuid import uuid4

from app.services.change_sets import build_agent_change_set_evidence


def _row(gate, mode, outcome, *, evidence=None):
    return {
        "id": uuid4(),
        "gate": gate,
        "mode": mode,
        "outcome": outcome,
        "evidence_hash": "a" * 64,
        "evidence": evidence or {},
    }


def test_advisory_failure_becomes_review_risk_not_validation_failure():
    validation, risks = build_agent_change_set_evidence(
        [
            _row("geometry", "required", "passed"),
            _row(
                "dfm",
                "advisory",
                "failed",
                evidence={"issues": ["overhang"], "violations": [{"id": "r1"}]},
            ),
        ]
    )
    assert validation["status"] == "passed"
    assert validation["issue_count"] == 0
    assert risks["status"] == "attention_required"
    assert risks["items"][0]["gate"] == "dfm"
    assert risks["items"][0]["issues"] == ["overhang"]


def test_required_indeterminate_is_validation_failure():
    validation, risks = build_agent_change_set_evidence(
        [_row("visual", "required", "indeterminate")]
    )
    assert validation["status"] == "failed"
    assert validation["issue_count"] == 1
    assert risks["status"] == "clear"
