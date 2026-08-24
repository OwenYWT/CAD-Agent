"""Verdict derivation and response parsing at the vision boundary.

The rule under test throughout: the overall verdict is *computed* from the
individual checks. A vision model that lists a blocking defect and then declares
a match must not be able to pass a part, and a model that omits a requirement
must not be able to score as though it had confirmed it.
"""
from __future__ import annotations

import json

import pytest

from app.visual_refine.contracts import RequirementCheck, VisualCritique
from app.visual_refine.critic import indeterminate, parse_critique_payload
from app.visual_refine.spec import build_spec

SPEC = build_spec(objective="an L bracket 60x40x30", plan={"dimensions": {}})
VISUAL_KEYS = SPEC.visual_requirement_keys()


def _check(key: str, verdict: str, severity: str = "major") -> RequirementCheck:
    return RequirementCheck(
        key=key, requirement=key, verdict=verdict, severity=severity
    )


# --- derived verdicts ---------------------------------------------------------


def test_a_blocking_violation_cannot_be_talked_into_a_pass():
    critique = VisualCritique(
        checks=(_check("shape.overall_form", "violated", "blocking"),),
        summary="looks great to me",
        confidence=0.99,
    )
    assert critique.is_match is False


def test_a_minor_violation_alone_does_not_fail_the_gate():
    critique = VisualCritique(
        checks=(
            _check("shape.overall_form", "satisfied", "blocking"),
            _check("geometry.sane", "violated", "minor"),
        ),
        confidence=0.9,
    )
    assert critique.is_match is True


def test_low_confidence_is_not_a_pass():
    critique = VisualCritique(
        checks=(_check("shape.overall_form", "satisfied", "blocking"),),
        confidence=0.4,
    )
    assert critique.is_match is False


def test_no_checks_is_indeterminate_not_a_pass():
    assert VisualCritique(confidence=0.99).is_match is None


def test_indeterminate_requires_a_reason():
    with pytest.raises(ValueError, match="reason"):
        VisualCritique(indeterminate=True)


def test_indeterminate_is_never_a_pass_or_a_fail():
    critique = indeterminate("vision_provider_unavailable:Timeout")
    assert critique.is_match is None
    assert critique.indeterminate_reason.startswith("vision_provider_unavailable")


def test_blocking_failures_outrank_minor_ones_in_the_repair_brief():
    critique = VisualCritique(
        checks=(
            _check("geometry.sane", "violated", "minor"),
            _check("features.attached", "violated", "blocking"),
        ),
        confidence=0.9,
    )
    brief = critique.repair_brief()
    assert brief.index("BLOCKING") < brief.index("MINOR")


def test_score_ranks_a_partly_good_part_above_a_wholly_bad_one():
    good = VisualCritique(
        checks=(
            _check("shape.overall_form", "satisfied", "blocking"),
            _check("features.present", "violated", "major"),
        ),
        confidence=0.9,
    )
    bad = VisualCritique(
        checks=(
            _check("shape.overall_form", "violated", "blocking"),
            _check("features.present", "violated", "major"),
        ),
        confidence=0.9,
    )
    assert good.score() > bad.score()


def test_not_visible_scores_between_satisfied_and_violated():
    def score(verdict: str) -> float:
        return VisualCritique(
            checks=(_check("shape.overall_form", verdict, "major"),), confidence=0.9
        ).score()

    assert score("violated") < score("not_visible") < score("satisfied")


# --- response parsing ---------------------------------------------------------


def _payload(checks, **extra):
    body = {"checks": checks, "issues": [], "suggestions": [], "confidence": 0.9}
    body.update(extra)
    return json.dumps(body)


def test_missing_keys_become_not_visible_rather_than_silently_passing():
    critique = parse_critique_payload(
        _payload([{"key": "shape.overall_form", "verdict": "satisfied"}]),
        spec=SPEC,
    )
    reported = {check.key: check.verdict for check in critique.checks}
    assert reported["shape.overall_form"] == "satisfied"
    for key in VISUAL_KEYS:
        assert key in reported
    assert reported["features.present"] == "not_visible"


def test_invented_keys_are_dropped():
    critique = parse_critique_payload(
        _payload(
            [
                {"key": "shape.overall_form", "verdict": "satisfied"},
                {"key": "colour.is_nice", "verdict": "violated"},
            ]
        ),
        spec=SPEC,
    )
    assert "colour.is_nice" not in {check.key for check in critique.checks}


def test_unknown_verdict_words_degrade_to_not_visible():
    critique = parse_critique_payload(
        _payload([{"key": "shape.overall_form", "verdict": "probably fine"}]),
        spec=SPEC,
    )
    verdict = next(
        c.verdict for c in critique.checks if c.key == "shape.overall_form"
    )
    assert verdict == "not_visible"


def test_markdown_fenced_json_is_accepted():
    fenced = "```json\n" + _payload(
        [{"key": "shape.overall_form", "verdict": "satisfied"}]
    ) + "\n```"
    critique = parse_critique_payload(fenced, spec=SPEC)
    assert critique.confidence == pytest.approx(0.9)


def test_measured_checks_are_kept_and_lead_the_check_list():
    measured = RequirementCheck(
        key="dimensions.bounding_box",
        requirement="overall size",
        verdict="violated",
        severity="blocking",
        origin="measured",
    )
    critique = parse_critique_payload(
        _payload([{"key": key, "verdict": "satisfied"} for key in VISUAL_KEYS]),
        spec=SPEC,
        measured_checks=(measured,),
    )
    assert critique.checks[0].key == "dimensions.bounding_box"
    # The model said everything looked fine; the measurement still fails the gate.
    assert critique.is_match is False


def test_a_model_cannot_overwrite_a_measured_verdict():
    measured = RequirementCheck(
        key="topology.single_body",
        requirement="one body",
        verdict="violated",
        severity="blocking",
        origin="measured",
    )
    critique = parse_critique_payload(
        _payload([{"key": "topology.single_body", "verdict": "satisfied"}]),
        spec=SPEC,
        measured_checks=(measured,),
    )
    single_body = [c for c in critique.checks if c.key == "topology.single_body"]
    assert len(single_body) == 1
    assert single_body[0].origin == "measured"
    assert single_body[0].verdict == "violated"


def test_non_json_response_raises_so_the_caller_reports_indeterminate():
    with pytest.raises(Exception):
        parse_critique_payload("I had a look and it seems fine", spec=SPEC)
