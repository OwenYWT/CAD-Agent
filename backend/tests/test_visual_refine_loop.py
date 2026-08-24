"""The refinement loop's safety properties, with fake ports (no LLM, no Docker).

The loop exists to make visual repair *monotonic*. The behaviour worth pinning
is not "it iterates" but "it can never hand back something worse than it was
given, and it never reports a pass it cannot support".
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.visual_refine.contracts import RequirementCheck, VisualCritique
from app.visual_refine.facts import GeometryFacts
from app.visual_refine.loop import VisualRefinementLoop
from app.visual_refine.ports import BuildResult
from app.visual_refine.spec import build_spec

SPEC = build_spec(objective="a 40mm cube bracket", plan={"dimensions": {}})


def _facts() -> GeometryFacts:
    return GeometryFacts(
        bounding_box=(40.0, 40.0, 40.0),
        volume=64000.0,
        surface_area=9600.0,
        is_watertight=True,
        body_count=1,
        largest_body_volume_fraction=1.0,
        fill_ratio=1.0,
        triangle_count=12,
    )


def _critique(*, satisfied: bool, confidence: float = 0.95) -> VisualCritique:
    verdict = "satisfied" if satisfied else "violated"
    return VisualCritique(
        checks=tuple(
            RequirementCheck(
                key=item.key,
                requirement=item.text,
                verdict=verdict,
                observed="fake",
                severity=item.severity,
            )
            for item in SPEC.requirements
        ),
        suggestions=("attach the rib",),
        confidence=confidence,
    )


class _Builder:
    """Builds unless the source is marked broken."""

    def __init__(self, tmp_path: Path) -> None:
        self._tmp = tmp_path
        self.calls: list[str] = []

    async def build(self, source_code: str) -> BuildResult:
        self.calls.append(source_code)
        if "BROKEN" in source_code:
            return BuildResult(success=False, error="SyntaxError: broken")
        path = self._tmp / "model.stl"
        path.write_bytes(b"solid\nendsolid\n")
        return BuildResult(success=True, model_path=path)


class _Renderer:
    async def render(self, model_path, output_dir, *, views=None, footer=()):
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        rendered = []
        for name in ("front", "top"):
            path = output_dir / f"{name}.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 200)
            rendered.append(_View(name, path))
        return rendered, _facts()


class _View:
    def __init__(self, name: str, path: Path) -> None:
        self.name = name
        self.path = path


class _Critic:
    """Returns a scripted critique per iteration."""

    def __init__(self, script: list[VisualCritique]) -> None:
        self._script = script
        self.calls = 0

    async def critique(self, **_kwargs) -> VisualCritique:
        index = min(self.calls, len(self._script) - 1)
        self.calls += 1
        return self._script[index]


class _Patcher:
    def __init__(self, replacements: list[str]) -> None:
        self._replacements = replacements
        self.calls: list[str] = []

    async def patch(self, *, source_code: str, **_kwargs) -> str:
        self.calls.append(source_code)
        index = min(len(self.calls) - 1, len(self._replacements) - 1)
        return self._replacements[index]


def _loop(tmp_path, critic, patcher, *, max_iterations=3, builder=None):
    return VisualRefinementLoop(
        builder=builder or _Builder(tmp_path),
        renderer=_Renderer(),
        critic=critic,
        patcher=patcher,
        max_iterations=max_iterations,
    )


@pytest.mark.asyncio
async def test_a_passing_first_build_is_returned_untouched(tmp_path):
    patcher = _Patcher(["should-never-run"])
    loop = _loop(tmp_path, _Critic([_critique(satisfied=True)]), patcher)

    outcome = await loop.run(
        source_code="original", spec=SPEC, work_dir=tmp_path / "work"
    )

    assert outcome.status == "passed"
    assert outcome.source_code == "original"
    assert outcome.changed is False
    assert patcher.calls == []  # no repair spent on a part that already passes


@pytest.mark.asyncio
async def test_a_repair_that_fixes_the_defect_is_adopted(tmp_path):
    critic = _Critic([_critique(satisfied=False), _critique(satisfied=True)])
    loop = _loop(tmp_path, critic, _Patcher(["repaired"]))

    outcome = await loop.run(
        source_code="original", spec=SPEC, work_dir=tmp_path / "work"
    )

    assert outcome.status == "passed"
    assert outcome.source_code == "repaired"
    assert outcome.changed is True
    assert outcome.iterations == 2


@pytest.mark.asyncio
async def test_a_repair_that_scores_worse_is_discarded(tmp_path):
    """The core monotonicity guarantee: refinement cannot make a part worse."""
    good = VisualCritique(
        checks=(
            RequirementCheck(
                key="shape.overall_form",
                requirement="form",
                verdict="satisfied",
                severity="blocking",
            ),
            RequirementCheck(
                key="features.present",
                requirement="features",
                verdict="violated",
                severity="minor",
            ),
        ),
        confidence=0.9,
    )
    worse = VisualCritique(
        checks=(
            RequirementCheck(
                key="shape.overall_form",
                requirement="form",
                verdict="violated",
                severity="blocking",
            ),
            RequirementCheck(
                key="features.present",
                requirement="features",
                verdict="violated",
                severity="minor",
            ),
        ),
        confidence=0.9,
    )
    loop = _loop(
        tmp_path,
        _Critic([good, worse, worse]),
        _Patcher(["regressed", "regressed-again"]),
    )

    outcome = await loop.run(
        source_code="original", spec=SPEC, work_dir=tmp_path / "work"
    )

    assert outcome.source_code == "original"
    assert outcome.changed is False
    assert outcome.critique is good


@pytest.mark.asyncio
async def test_a_repair_that_fails_to_build_never_replaces_a_working_part(tmp_path):
    loop = _loop(
        tmp_path,
        _Critic([_critique(satisfied=False)]),
        _Patcher(["BROKEN"]),
    )

    outcome = await loop.run(
        source_code="original", spec=SPEC, work_dir=tmp_path / "work"
    )

    assert outcome.source_code == "original"
    assert outcome.changed is False
    assert any(item.verdict == "build_failed" for item in outcome.attempts)


@pytest.mark.asyncio
async def test_indeterminate_critique_never_reads_as_a_pass(tmp_path):
    unavailable = VisualCritique(
        indeterminate=True, indeterminate_reason="vision_provider_unavailable:Timeout"
    )
    patcher = _Patcher(["should-never-run"])
    loop = _loop(tmp_path, _Critic([unavailable]), patcher)

    outcome = await loop.run(
        source_code="original", spec=SPEC, work_dir=tmp_path / "work"
    )

    assert outcome.status == "indeterminate"
    assert outcome.source_code == "original"
    # Nothing actionable was reported, so no blind rewrite was attempted.
    assert patcher.calls == []


@pytest.mark.asyncio
async def test_first_build_failure_is_reported_not_silently_passed(tmp_path):
    loop = _loop(tmp_path, _Critic([_critique(satisfied=True)]), _Patcher(["x"]))

    outcome = await loop.run(
        source_code="BROKEN", spec=SPEC, work_dir=tmp_path / "work"
    )

    assert outcome.status == "failed"
    assert outcome.source_code == "BROKEN"
    assert outcome.changed is False


@pytest.mark.asyncio
async def test_iteration_budget_is_respected(tmp_path):
    critic = _Critic([_critique(satisfied=False)])
    builder = _Builder(tmp_path)
    loop = _loop(
        tmp_path,
        critic,
        _Patcher(["v2", "v3", "v4"]),
        max_iterations=2,
        builder=builder,
    )

    outcome = await loop.run(
        source_code="original", spec=SPEC, work_dir=tmp_path / "work"
    )

    assert outcome.iterations == 2
    assert critic.calls == 2


@pytest.mark.asyncio
async def test_a_prebuilt_model_skips_the_first_execution(tmp_path):
    builder = _Builder(tmp_path)
    model = tmp_path / "already.stl"
    model.write_bytes(b"solid\nendsolid\n")
    loop = _loop(
        tmp_path,
        _Critic([_critique(satisfied=True)]),
        _Patcher(["x"]),
        builder=builder,
    )

    outcome = await loop.run(
        source_code="original",
        spec=SPEC,
        work_dir=tmp_path / "work",
        model_path=model,
    )

    assert outcome.status == "passed"
    assert builder.calls == []  # the caller's existing build was reused
