import json
import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError
from types import SimpleNamespace

from app.validation.durable_visual import (
    DurableVisualReport,
    VisualJudgment,
    VisualRenderEvidence,
    DurableVisualValidator,
    indeterminate_visual_report,
)
from app.llm import _completion_provenance


def _render(view: str) -> VisualRenderEvidence:
    return VisualRenderEvidence(
        view=view,
        filename=f"{view}.png",
        object_key=f"staging/renders/{view}.png",
        sha256=hashlib.sha256(view.encode()).hexdigest(),
        size_bytes=1000,
        width=512,
        height=512,
    )


def test_visual_pass_requires_four_renders_and_provider_provenance():
    report = DurableVisualReport(
        schema_version="durable-visual-report.v1",
        outcome="passed",
        renders=tuple(
            _render(view) for view in ("front", "right", "top", "isometric")
        ),
        judgment=VisualJudgment(is_match=True, confidence=0.92),
        runtime_provenance={"image_digest": "sha256:" + "a" * 64},
        provider_provenance={
            "provider": "controlled",
            "model": "vision",
            "request_hash": "a" * 64,
            "response_hash": "b" * 64,
        },
    )
    assert report.outcome == "passed"


def test_visual_low_confidence_cannot_be_passed():
    with pytest.raises(ValidationError, match="confidence"):
        VisualJudgment(is_match=True, confidence=0.69)


def test_visual_indeterminate_never_becomes_pass():
    report = indeterminate_visual_report(issue="vision_provider_unavailable")
    assert report.outcome == "indeterminate"
    assert report.judgment is None


def test_visual_failed_requires_explicit_mismatch():
    with pytest.raises(ValidationError, match="disagrees"):
        DurableVisualReport(
            schema_version="durable-visual-report.v1",
            outcome="failed",
            renders=tuple(
                _render(view)
                for view in ("front", "right", "top", "isometric")
            ),
            judgment=VisualJudgment(is_match=True, confidence=0.9),
            provider_provenance={
                "provider": "controlled",
                "model": "vision",
                "request_hash": "a" * 64,
                "response_hash": "b" * 64,
            },
        )


class _UnavailableVisualValidator(DurableVisualValidator):
    async def judge(self, **_kwargs):
        raise RuntimeError("provider unavailable")


class _MalformedVisualValidator(DurableVisualValidator):
    async def judge(self, **_kwargs):
        raise ValueError("malformed provider JSON")


class _TimedOutVisualValidator(DurableVisualValidator):
    async def judge(self, **_kwargs):
        raise TimeoutError("provider timed out")


class _PassingVisualValidator(DurableVisualValidator):
    async def judge(self, **_kwargs):
        return (
            VisualJudgment(is_match=True, confidence=0.95),
            {
                "provider": "controlled",
                "model": "vision",
                "request_hash": "a" * 64,
                "response_hash": "b" * 64,
            },
        )


class _FailedVisualValidator(DurableVisualValidator):
    async def judge(self, **_kwargs):
        return (
            VisualJudgment(
                is_match=False,
                confidence=0.91,
                issues=("missing hole",),
                suggestions=("add the requested hole",),
            ),
            {
                "provider": "controlled",
                "model": "vision",
                "request_hash": "a" * 64,
                "response_hash": "b" * 64,
            },
        )


@pytest.mark.parametrize(
    ("validator", "issue"),
    [
        (_UnavailableVisualValidator(), "vision_provider_unavailable:RuntimeError"),
        (_MalformedVisualValidator(), "vision_provider_unavailable:ValueError"),
        (_TimedOutVisualValidator(), "vision_provider_unavailable:TimeoutError"),
    ],
)
@pytest.mark.asyncio
async def test_visual_provider_failures_are_indeterminate(validator, issue):
    renders = tuple(
        _render(view) for view in ("front", "right", "top", "isometric")
    )
    report = await validator.report(
        objective="box",
        design_brief={},
        render_paths=tuple(Path(item.filename) for item in renders),
        renders=renders,
        runtime_provenance={"image_digest": "sha256:" + "a" * 64},
    )
    assert report.outcome == "indeterminate"
    assert report.issues == (issue,)
    assert report.provider_provenance is None


@pytest.mark.parametrize(
    ("validator", "expected"),
    [
        (_PassingVisualValidator(), "passed"),
        (_FailedVisualValidator(), "failed"),
    ],
)
@pytest.mark.asyncio
async def test_visual_provider_pass_and_failure_remain_distinct(validator, expected):
    renders = tuple(
        _render(view) for view in ("front", "right", "top", "isometric")
    )
    report = await validator.report(
        objective="box",
        design_brief={},
        render_paths=tuple(Path(item.filename) for item in renders),
        renders=renders,
        runtime_provenance={"image_digest": "sha256:" + "a" * 64},
    )
    assert report.outcome == expected
    assert report.provider_provenance.model_dump(mode="json") == {
        "provider": "controlled",
        "model": "vision",
        "provider_response_id": None,
        "request_hash": "a" * 64,
        "response_hash": "b" * 64,
        "finish_reason": None,
        "usage": {},
    }


@pytest.mark.asyncio
async def test_visual_judgment_uses_configured_vision_model(tmp_path, monkeypatch):
    seen = {}

    class Completions:
        async def create(self, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(
                id="vision-1",
                model=kwargs["model"],
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=json.dumps(
                                {
                                    "checks": [
                                        {"key": key, "verdict": "satisfied"}
                                        for key in (
                                            "shape.overall_form",
                                            "shape.proportions",
                                            "features.present",
                                            "features.attached",
                                            "geometry.sane",
                                        )
                                    ],
                                    "issues": [],
                                    "suggestions": [],
                                    "confidence": 0.95,
                                }
                            )
                        ),
                        finish_reason="stop",
                    )
                ],
                usage=SimpleNamespace(
                    prompt_tokens=10,
                    completion_tokens=5,
                    total_tokens=15,
                ),
            )

    paths = []
    for view in ("front", "right", "top", "isometric"):
        path = tmp_path / f"{view}.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + view.encode())
        paths.append(path)
    monkeypatch.setattr(
        "app.validation.durable_visual.settings.vision_model",
        "moonshot-vision-test",
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))

    def provenance_reader():
        response = SimpleNamespace(
            id="vision-1",
            model="moonshot-vision-test",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="{}"),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )
        return _completion_provenance(
            params={"model": "moonshot-vision-test", "messages": []},
            response=response,
            provider="moonshot",
        )

    validator = DurableVisualValidator(
        client=client,
        provenance_reader=provenance_reader,
    )
    judgment, provenance = await validator.judge(
        objective="box",
        design_brief={},
        render_paths=tuple(paths),
    )
    assert judgment.is_match is True
    assert seen["model"] == "moonshot-vision-test"
    assert seen["response_format"] == {"type": "json_object"}
    # A requirement briefing, the four renders, then the per-key closing ask.
    content = seen["messages"][1]["content"]
    assert [part["type"] for part in content] == [
        "text",
        "image_url",
        "image_url",
        "image_url",
        "image_url",
        "text",
    ]
    assert provenance["model"] == "moonshot-vision-test"
