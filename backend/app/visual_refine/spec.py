"""Turn a request into requirements, and check the measurable ones by measuring.

The accuracy argument for this module
-------------------------------------
The old visual gate asked one vision model one question -- "does this picture
match this sentence?" -- and took its word for everything, including whether a
part was 100 mm or 60 mm long. Vision models are good at shape and bad at
absolute size, so every dimension error had to be caught by luck.

So requirements are split by what can actually settle them:

* **measured**  -- overall size, hole count, hole diameter, through vs blind,
  body count, watertightness. Settled from the mesh, exactly, every time. A
  vision model is never asked and can never overrule the result.
* **observed**  -- shape family, feature presence and placement, proportions,
  "does this read as a bracket". Settled by the vision model looking at renders
  that now carry dimensions and a scale.

Everything here is pure: dicts and dataclasses in, checks out. No LLM, no IO.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from app.visual_refine.contracts import RequirementCheck, Severity
from app.visual_refine.facts import GeometryFacts

# Only these complete groups describe the three bounding-box axes. Feature
# dimensions (hole diameter, wall thickness, fillet radius) must never be read
# as an overall extent -- the same rule the geometry validator applies.
_BBOX_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("width", "depth", "height"),
    ("length", "width", "height"),
    ("length", "depth", "height"),
    ("length", "width", "thickness"),
    ("width", "height", "thickness"),
    ("x", "y", "z"),
)

_DIAMETER_KEYS = ("diameter", "outer_diameter", "od", "d")
_HOLE_DIAMETER_KEYS = (
    "hole_diameter",
    "bore_diameter",
    "inner_diameter",
    "id",
    "hole_d",
)

# Default relative tolerance on an overall dimension. Generous enough to survive
# a fillet eating a corner, tight enough that a wrong number is still wrong.
_DIMENSION_TOLERANCE = 0.10
_DIAMETER_TOLERANCE = 0.12

# Reading a hole *count* out of prose is where a naive regex does real damage:
# in "直径 6mm 的通孔" the 6 is a diameter, and in "M4 沉孔" the 4 is a thread. So
# a count is accepted only when a quantity word is attached to a counter, the
# number is not part of a dimension, and the reading is unambiguous. Anything
# else abstains and lets the critic compare the measured bores against the
# sentence itself, which is a language job a model does better than a pattern.
_CHINESE_DIGITS = {
    "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十二": 12,
}
_ENGLISH_DIGITS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12,
}
_COUNTER = "个|處|处|只|条|組|组|處"
_HOLE_NOUN = r"孔|hole"
# A digit glued to a unit is a measurement, never a quantity.
_UNIT_SUFFIX = re.compile(r"^\s*(mm|毫米|cm|厘米|米|inch|in\b|°|度)", re.IGNORECASE)
# A digit glued to one of these prefixes is a thread size, a radius or a span.
_DIMENSION_PREFIX = re.compile(r"(?:[MmØøΦφRr#]|直径|半径|深|厚|宽|長|长|高|边长)\s*$")

_COUNT_PATTERNS = (
    re.compile(rf"(\d+)\s*(?:{_COUNTER})"),
    re.compile(rf"([一两二三四五六七八九十])\s*(?:{_COUNTER})"),
    re.compile(r"(\d+)\s*[xX×]\s"),
    # "4 corner through holes", "two mounting holes": a quantity and its noun are
    # rarely adjacent, so a few descriptive words are allowed between them. The
    # dimension guards below still reject "6mm diameter holes" and "M6 holes".
    re.compile(
        r"\b(one|two|three|four|five|six|seven|eight|nine|ten|twelve|\d+)\s+"
        r"(?:[A-Za-z][A-Za-z-]*\s+){0,3}holes?\b",
        re.IGNORECASE,
    ),
)
_HOLE_WINDOW = 18

# A distributive quantifier makes a stated number per-unit rather than total:
# "四角各一个 M6 螺栓孔" is four holes, not one, and "每板两个 M5 孔" is four, not
# two. The number is real but the total is not recoverable from it.
_DISTRIBUTIVE = re.compile(r"各|每|\beach\b|\bper\b", re.IGNORECASE)
# Likewise when a description names more than one group of holes and quantifies
# only some of them -- "中心孔直径 30mm，四角各一个 M6 螺栓孔" -- any single number
# in it is a group count, never the total.
_HOLE_MENTION = re.compile(r"孔|holes?", re.IGNORECASE)


@dataclass(frozen=True)
class Requirement:
    """One checkable statement about the requested part."""

    key: str
    text: str
    severity: Severity = "major"
    # Only requirements the mesh can settle carry a measurement plan.
    measurable: bool = False


@dataclass
class DesignSpec:
    """The requirement set for one generation request.

    ``objective`` stays verbatim: it is the ground truth the vision model is
    ultimately comparing against, and paraphrasing it into structured fields is
    how detail gets lost.
    """

    objective: str
    requirements: tuple[Requirement, ...] = ()
    expected_dimensions: dict[str, float] = field(default_factory=dict)
    expected_hole_count: int | None = None
    expected_hole_diameters: tuple[float, ...] = ()
    features: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def briefing(self) -> str:
        """The requirement block shown to the vision model."""
        lines = [f"REQUEST (verbatim): {self.objective}"]
        if self.expected_dimensions:
            rendered = ", ".join(
                f"{name}={value:g}mm"
                for name, value in sorted(self.expected_dimensions.items())
            )
            lines.append(f"Stated dimensions: {rendered}")
        if self.features:
            lines.append("Requested features: " + "; ".join(self.features))
        if self.constraints:
            lines.append("Constraints: " + "; ".join(self.constraints))
        if self.acceptance_criteria:
            lines.append("Acceptance criteria: " + "; ".join(self.acceptance_criteria))
        visual = [item for item in self.requirements if not item.measurable]
        if visual:
            lines.append("")
            lines.append("CHECK EACH OF THESE AND REPORT ONE VERDICT PER KEY:")
            for item in visual:
                lines.append(f"- [{item.key}] ({item.severity}) {item.text}")
        return "\n".join(lines)

    def measured_checks(self, facts: GeometryFacts) -> tuple[RequirementCheck, ...]:
        """Settle every measurable requirement directly from the mesh."""
        checks: list[RequirementCheck] = []
        checks.extend(_check_topology(facts))
        checks.extend(_check_dimensions(self.expected_dimensions, facts))
        checks.extend(
            _check_holes(self.expected_hole_count, self.expected_hole_diameters, facts)
        )
        return tuple(checks)

    def visual_requirement_keys(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.requirements if not item.measurable)


def _check_topology(facts: GeometryFacts) -> list[RequirementCheck]:
    """Body count and watertightness: the defects that ruin a part silently."""
    checks = [
        RequirementCheck(
            key="topology.single_body",
            requirement=(
                "every feature is fused into one solid body, with nothing "
                "floating or detached"
            ),
            verdict="satisfied" if facts.body_count == 1 else "violated",
            observed=f"mesh splits into {facts.body_count} body/bodies",
            severity="blocking",
            origin="measured",
        )
    ]
    # A CadQuery assembly legitimately exports several bodies, so watertightness
    # is reported at major severity rather than blocking the whole run.
    checks.append(
        RequirementCheck(
            key="topology.watertight",
            requirement="the solid is watertight and printable as a closed volume",
            verdict="satisfied" if facts.is_watertight else "violated",
            observed=(
                "watertight" if facts.is_watertight else "open shell / non-manifold mesh"
            ),
            severity="major",
            origin="measured",
        )
    )
    if facts.volume <= 0:
        checks.append(
            RequirementCheck(
                key="topology.has_volume",
                requirement="the result is a solid with positive volume",
                verdict="violated",
                observed="the mesh encloses no measurable volume",
                severity="blocking",
                origin="measured",
            )
        )
    return checks


def _bbox_triple(expected: dict[str, float]) -> tuple[float, float, float] | None:
    """Extract three overall extents, only from a complete naming group."""
    lowered = {str(key).strip().lower(): float(value) for key, value in expected.items()}
    for group in _BBOX_GROUPS:
        if all(name in lowered for name in group):
            return (lowered[group[0]], lowered[group[1]], lowered[group[2]])
    return None


def _check_dimensions(
    expected: dict[str, float], facts: GeometryFacts
) -> list[RequirementCheck]:
    """Compare stated overall size against the measured bounding box.

    Comparison is on *sorted* extents, because a request rarely pins which model
    axis carries which name and a correct part laid out on a different axis is
    still a correct part.
    """
    if not expected:
        return []
    checks: list[RequirementCheck] = []
    lowered = {str(key).strip().lower(): float(value) for key, value in expected.items()}

    triple = _bbox_triple(expected)
    if triple:
        wanted = sorted((abs(value) for value in triple), reverse=True)
        measured = list(facts.sorted_extents)
        worst = 0.0
        for want, got in zip(wanted, measured):
            if want <= 0:
                continue
            worst = max(worst, abs(got - want) / want)
        satisfied = worst <= _DIMENSION_TOLERANCE
        checks.append(
            RequirementCheck(
                key="dimensions.bounding_box",
                requirement=(
                    "overall size matches the request: "
                    + " x ".join(f"{value:g}" for value in wanted)
                    + " mm"
                ),
                verdict="satisfied" if satisfied else "violated",
                observed=(
                    "measured "
                    + " x ".join(f"{value:.2f}" for value in measured)
                    + f" mm (worst axis off by {worst:.1%})"
                ),
                severity="blocking",
                origin="measured",
            )
        )
        return checks

    # No complete triple: fall back to the single strongest scalar the request
    # pins, which for round parts is almost always an outside diameter.
    for key in _DIAMETER_KEYS:
        if key in lowered and lowered[key] > 0:
            wanted = lowered[key]
            extents = sorted(facts.bounding_box, reverse=True)
            # A cylinder's two largest-or-equal cross-section extents are the
            # diameter; compare against the pair closest to it.
            candidate = min(extents, key=lambda value: abs(value - wanted))
            deviation = abs(candidate - wanted) / wanted
            checks.append(
                RequirementCheck(
                    key="dimensions.outer_diameter",
                    requirement=f"outer diameter is {wanted:g} mm",
                    verdict=(
                        "satisfied" if deviation <= _DIAMETER_TOLERANCE else "violated"
                    ),
                    observed=(
                        f"closest measured extent {candidate:.2f} mm "
                        f"(off by {deviation:.1%})"
                    ),
                    severity="major",
                    origin="measured",
                )
            )
            break
    return checks


def _check_holes(
    expected_count: int | None,
    expected_diameters: Sequence[float],
    facts: GeometryFacts,
) -> list[RequirementCheck]:
    """Count and size the bores, from the mesh rather than from the picture."""
    checks: list[RequirementCheck] = []
    measured = [hole for hole in facts.holes if hole.kind == "hole"]

    if expected_count is not None:
        satisfied = len(measured) == expected_count
        checks.append(
            RequirementCheck(
                key="features.hole_count",
                requirement=f"the part has {expected_count} hole(s)",
                verdict="satisfied" if satisfied else "violated",
                observed=(
                    f"{len(measured)} cylindrical bore(s) detected"
                    + (
                        ": " + ", ".join(f"D{hole.diameter:.2f}" for hole in measured)
                        if measured
                        else ""
                    )
                ),
                severity="major",
                origin="measured",
            )
        )

    for index, wanted in enumerate(expected_diameters):
        if wanted <= 0:
            continue
        if not measured:
            checks.append(
                RequirementCheck(
                    key=f"features.hole_diameter.{index}",
                    requirement=f"a hole of diameter {wanted:g} mm exists",
                    verdict="violated",
                    observed="no cylindrical bore was detected in the mesh",
                    severity="major",
                    origin="measured",
                )
            )
            continue
        closest = min(measured, key=lambda hole: abs(hole.diameter - wanted))
        deviation = abs(closest.diameter - wanted) / wanted
        checks.append(
            RequirementCheck(
                key=f"features.hole_diameter.{index}",
                requirement=f"a hole of diameter {wanted:g} mm exists",
                verdict="satisfied" if deviation <= _DIAMETER_TOLERANCE else "violated",
                observed=(
                    f"closest measured bore D{closest.diameter:.2f} mm "
                    f"(off by {deviation:.1%})"
                ),
                severity="major",
                origin="measured",
            )
        )
    return checks


def _coerce_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _hole_count_from_text(text: str) -> int | None:
    """Read an explicit hole count out of a request, or abstain.

    Abstains -- returns ``None`` -- in three situations, all of which appear in
    this repository's own eval set and RAG examples:

    * the sentence supports two readings ("四个角各一个直径 6mm 的通孔" is both 4
      and 1);
    * a distributive quantifier makes the number per-unit ("每板两个 M5 孔" is
      four holes across two plates);
    * more than one group of holes is named and only some are counted
      ("中心孔直径 30mm，四角各一个 M6 螺栓孔" is five, not one).

    Abstaining costs one deterministic check and leaves the judgement to the
    vision model, which is given the measured bore list anyway. Guessing wrong
    fails a *correct* part, which is far more expensive.
    """
    body = text or ""
    if _DISTRIBUTIVE.search(body):
        return None
    if len(_HOLE_MENTION.findall(body)) > 1:
        return None
    found: set[int] = set()

    for pattern in _COUNT_PATTERNS:
        for match in pattern.finditer(body):
            token = match.group(1)
            start, end = match.span(1)
            if token.isdigit():
                if _UNIT_SUFFIX.match(body[end:]):
                    continue  # "6mm" is a size
                if _DIMENSION_PREFIX.search(body[max(0, start - 4) : start]):
                    continue  # "M4", "直径 6", "R10"
                value = int(token)
            else:
                lowered = token.lower()
                value = _CHINESE_DIGITS.get(token) or _ENGLISH_DIGITS.get(lowered, 0)
            if not 0 < value <= 64:
                continue
            window = body[end : end + _HOLE_WINDOW]
            if re.search(_HOLE_NOUN, window, re.IGNORECASE) or re.search(
                _HOLE_NOUN, match.group(0), re.IGNORECASE
            ):
                found.add(value)

    return found.pop() if len(found) == 1 else None


def build_spec(
    *,
    objective: str,
    plan: Any | None = None,
    design_brief: Any | None = None,
    extra_requirements: Iterable[Requirement] = (),
    expected_hole_count: int | None = None,
) -> DesignSpec:
    """Assemble a spec from the objective plus whatever planning data exists.

    ``plan`` and ``design_brief`` are read structurally (``getattr`` / mapping
    access) rather than imported, so this module stays independent of the agent
    schemas and can be exercised with plain dicts in tests.
    """
    dimensions: dict[str, float] = {}
    features: list[str] = []
    constraints: list[str] = []
    acceptance: list[str] = []
    notes: list[str] = []

    def _read(source: Any, name: str, default: Any = None) -> Any:
        if source is None:
            return default
        if isinstance(source, dict):
            return source.get(name, default)
        return getattr(source, name, default)

    raw_dimensions = _read(plan, "dimensions") or {}
    if isinstance(raw_dimensions, dict):
        for key, value in raw_dimensions.items():
            number = _coerce_float(value)
            if number is not None and number > 0:
                dimensions[str(key).strip().lower()] = number

    for item in _read(plan, "features") or ():
        text = str(item).strip()
        if text:
            features.append(text)
    for item in _read(plan, "constraints") or ():
        text = str(item).strip()
        if text:
            constraints.append(text)

    brief = design_brief if design_brief is not None else _read(plan, "design_brief")
    critical = _read(brief, "critical_dimensions") or ()
    for item in critical:
        name = str(_read(item, "name", "") or "").strip().lower()
        number = _coerce_float(_read(item, "value"))
        if name and number is not None and number > 0:
            dimensions.setdefault(name, number)
    for item in _read(brief, "functional_requirements") or ():
        text = str(item).strip()
        if text and text not in features:
            features.append(text)
    for item in _read(brief, "acceptance_criteria") or ():
        text = str(item).strip()
        if text:
            acceptance.append(text)
    for item in _read(brief, "assumptions") or ():
        text = str(item).strip()
        if text:
            notes.append(f"assumption: {text}")

    hole_diameters = tuple(
        value
        for key, value in dimensions.items()
        if key in _HOLE_DIAMETER_KEYS and value > 0
    )
    # A count supplied by the caller is trusted. Otherwise the request gets one
    # strict reading attempt, and the planner's own feature list gets a second:
    # "4 corner through holes D6" states a quantity plainly where the original
    # sentence often does not. The number of distinct *diameters* is never
    # mistaken for a number of holes.
    hole_count = expected_hole_count
    if hole_count is None:
        hole_count = _hole_count_from_text(objective)
    if hole_count is None and features:
        hole_count = _hole_count_from_text("; ".join(features))

    requirements: list[Requirement] = [
        Requirement(
            key="shape.overall_form",
            text=(
                "the overall form is the object the request describes, not a "
                "generic block standing in for it"
            ),
            severity="blocking",
        ),
        Requirement(
            key="shape.proportions",
            text=(
                "proportions in the renders agree with the stated dimensions "
                "(read the burned-in dimension labels and the millimetre grid)"
            ),
            severity="major",
        ),
        Requirement(
            key="features.present",
            text=(
                "every requested feature is present and positioned sensibly: "
                + ("; ".join(features) if features else "as described in the request")
            ),
            severity="major",
        ),
        Requirement(
            key="features.attached",
            text=(
                "added features grow out of the body instead of hovering beside "
                "it, and cut features actually remove material"
            ),
            severity="blocking",
        ),
        Requirement(
            key="geometry.sane",
            text=(
                "no degenerate geometry: nothing collapsed to a sheet, no visible "
                "self-intersection, no stray protrusion the request never asked for"
            ),
            severity="major",
        ),
    ]
    if constraints:
        requirements.append(
            Requirement(
                key="constraints.respected",
                text="stated constraints are respected: " + "; ".join(constraints),
                severity="major",
            )
        )
    requirements.extend(extra_requirements)

    return DesignSpec(
        objective=objective,
        requirements=tuple(requirements),
        expected_dimensions=dimensions,
        expected_hole_count=hole_count,
        expected_hole_diameters=hole_diameters,
        features=tuple(features),
        constraints=tuple(constraints),
        acceptance_criteria=tuple(acceptance),
        notes=tuple(notes),
    )
