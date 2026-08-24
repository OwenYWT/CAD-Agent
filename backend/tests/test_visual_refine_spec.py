"""Requirement extraction and the measured half of the visual gate.

These are hermetic: no LLM, no Docker, no rendering. They pin the rules that
decide when the gate is allowed to fail a part deterministically, because a
false deterministic failure is worse than no check at all.
"""
from __future__ import annotations

import pytest

from app.visual_refine.facts import GeometryFacts, HoleFact
from app.visual_refine.spec import _hole_count_from_text, build_spec


def _facts(
    *,
    bbox=(100.0, 60.0, 40.0),
    volume=215689.0,
    bodies=1,
    watertight=True,
    holes=(),
) -> GeometryFacts:
    return GeometryFacts(
        bounding_box=bbox,
        volume=volume,
        surface_area=29568.0,
        is_watertight=watertight,
        body_count=bodies,
        largest_body_volume_fraction=1.0,
        fill_ratio=volume / (bbox[0] * bbox[1] * bbox[2]),
        holes=holes,
        triangle_count=120,
    )


def _bore(diameter: float, center=(0.0, 0.0, 0.0)) -> HoleFact:
    return HoleFact(
        axis="Z",
        diameter=diameter,
        depth=40.0,
        through=True,
        kind="hole",
        center=center,
    )


def _verdict(checks, key: str) -> str:
    return next(check.verdict for check in checks if check.key == key)


# --- hole counting: abstaining beats guessing ---------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("四个角各一个直径 6mm 的通孔", None),  # supports both 4 and 1 -> abstain
        ("顶部中心一个 M4 沉孔", 1),
        ("底板上 4 个 M3 螺纹孔", 4),
        ("a bracket with four holes", 4),
        ("plate with 6 holes on the flange", 6),
        ("直径 6mm 的通孔", None),  # 6 is a diameter, not a quantity
        ("M4 沉孔", None),  # 4 is a thread size
        ("两端各一个 R10 的半圆缺口", None),  # not a hole at all
        ("外径 48mm 内径 26mm 厚 5mm 的环形件", None),
        ("55x18x10mm 的长方块，沿长边开一条 2mm 宽 8mm 深的插槽", None),
        # Quantity and noun are rarely adjacent in planner feature text.
        ("4 corner through holes D6", 4),
        ("two mounting holes", 2),
        ("4x M3 螺纹孔", 4),
        ("6mm diameter holes", None),  # still a diameter
        ("M6 holes", None),  # still a thread size
        ("a single hole", None),  # no quantity word at all
        # Distributive quantifiers make a number per-unit, not total. Both of
        # these are real eval cases (M03, M01) where reading the number as a
        # total would have failed a correct part.
        ("每板两个 M5 孔", None),  # two per plate, two plates -> 4
        ("四角各一个 M6 螺栓孔", None),  # one per corner -> 4
        ("two holes per flange", None),
        # More than one group of holes named, only one counted (M01, X01).
        ("中心孔直径 30mm，四角各一个 M6 螺栓孔边距 10mm", None),
        ("外径 60mm，中心孔 25mm，六个直径 5mm 的螺栓孔", None),
    ],
)
def test_hole_count_reads_quantities_and_abstains_on_ambiguity(text, expected):
    assert _hole_count_from_text(text) == expected


def test_derived_hole_counts_agree_with_the_eval_set():
    """Sweep the parser over the repository's own eval descriptions.

    A wrong hole count fails a *correct* part, so the bar is not "usually
    right": every count this derives from a case that declares one must match
    it exactly. Abstaining is always allowed; being confidently wrong is not.

    This caught three real false positives (M01, M03, X01) on first run.
    """
    from benchmark.eval_cases import EVAL_CASES

    disagreements = []
    for case in EVAL_CASES:
        declared = (case.get("expected_features") or {}).get("holes")
        if declared is None:
            continue
        derived = _hole_count_from_text(case["description"])
        if derived is not None and derived != declared:
            disagreements.append(
                f"{case['id']}: derived {derived}, case declares {declared} "
                f"-- {case['description']}"
            )
    assert not disagreements, "\n".join(disagreements)


def test_planner_features_supply_a_count_the_prose_leaves_ambiguous():
    spec = build_spec(
        objective="四个角各一个直径 6mm 的通孔",
        plan={"dimensions": {}, "features": ["4 corner through holes D6"]},
    )
    assert spec.expected_hole_count == 4


def test_structured_hole_count_overrides_prose():
    spec = build_spec(
        objective="四个角各一个直径 6mm 的通孔",
        plan={"dimensions": {}, "features": []},
        expected_hole_count=4,
    )
    assert spec.expected_hole_count == 4


def test_hole_diameter_count_is_never_read_as_a_hole_count():
    """One stated diameter does not mean one hole -- the old fallback's bug."""
    spec = build_spec(
        objective="四个角各一个直径 6mm 的通孔",
        plan={"dimensions": {"hole_diameter": 6}, "features": []},
    )
    assert spec.expected_hole_diameters == (6.0,)
    assert spec.expected_hole_count is None
    keys = {check.key for check in spec.measured_checks(_facts(holes=tuple(_bore(6.0) for _ in range(4))))}
    assert "features.hole_count" not in keys


# --- measured checks ----------------------------------------------------------


def test_bounding_box_is_compared_on_sorted_extents():
    """A correct part laid out on a different axis is still a correct part."""
    spec = build_spec(
        objective="100x60x40 plate",
        plan={"dimensions": {"width": 60, "depth": 40, "height": 100}},
    )
    checks = spec.measured_checks(_facts(bbox=(100.0, 60.0, 40.0)))
    assert _verdict(checks, "dimensions.bounding_box") == "satisfied"


def test_wrong_overall_size_is_a_blocking_measured_failure():
    spec = build_spec(
        objective="100x60x40 plate",
        plan={"dimensions": {"width": 100, "depth": 60, "height": 40}},
    )
    checks = spec.measured_checks(_facts(bbox=(125.0, 60.0, 40.0)))
    failure = next(c for c in checks if c.key == "dimensions.bounding_box")
    assert failure.verdict == "violated"
    assert failure.severity == "blocking"
    assert failure.origin == "measured"


def test_partial_dimension_group_does_not_become_a_bounding_box_check():
    """hole_diameter alone must never be read as an overall extent."""
    spec = build_spec(
        objective="a plate with a 6mm hole",
        plan={"dimensions": {"hole_diameter": 6}},
    )
    keys = {check.key for check in spec.measured_checks(_facts())}
    assert "dimensions.bounding_box" not in keys


def test_detached_bodies_are_a_blocking_measured_failure():
    spec = build_spec(objective="one solid bracket", plan={"dimensions": {}})
    checks = spec.measured_checks(_facts(bodies=2))
    failure = next(c for c in checks if c.key == "topology.single_body")
    assert failure.verdict == "violated"
    assert failure.severity == "blocking"


def test_hole_count_mismatch_is_measured_not_guessed():
    spec = build_spec(
        objective="底板上 4 个 M3 螺纹孔",
        plan={"dimensions": {}},
    )
    checks = spec.measured_checks(_facts(holes=(_bore(3.0), _bore(3.0))))
    assert _verdict(checks, "features.hole_count") == "violated"


def test_diameter_within_tolerance_passes():
    spec = build_spec(
        objective="plate with a 6mm hole",
        plan={"dimensions": {"hole_diameter": 6}},
    )
    checks = spec.measured_checks(_facts(holes=(_bore(6.05),)))
    assert _verdict(checks, "features.hole_diameter.0") == "satisfied"


def test_spec_briefing_carries_the_request_verbatim():
    objective = "牙刷架底座，70x45x6mm 的圆角板"
    spec = build_spec(objective=objective, plan={"dimensions": {}})
    assert objective in spec.briefing()
    # Visual keys are listed so the critic knows exactly what to answer.
    assert "shape.overall_form" in spec.briefing()
