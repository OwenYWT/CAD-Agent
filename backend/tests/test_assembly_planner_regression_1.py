from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent.assembly_planner import AssemblyPlanner
from app.config import settings
from app.models.schemas import CADPlan


class _CompletionsStub:
    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


def _response(content: str | None, *, finish_reason: str = "stop") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content),
            )
        ]
    )


def _plan() -> CADPlan:
    return CADPlan(
        description="底座与上盖装配",
        part_type="assembly",
        dimensions={"length": 30, "width": 20, "height": 8},
        features=["底座 30x20x5", "上盖 30x20x3"],
    )


@pytest.mark.asyncio
async def test_assembly_planner_retries_malformed_json_and_requires_json_mode():
    completions = _CompletionsStub(
        [
            _response("我来拆解这个装配体"),
            _response(
                '{"assembly_description":"底座与上盖",'
                '"parts":['
                '{"name":"base","description":"底座",'
                '"dimensions":{"length":30,"width":20,"height":5},'
                '"position":[0,0,0],"color":"steelblue"},'
                '{"name":"cover","description":"上盖",'
                '"dimensions":{"length":30,"width":20,"height":3},'
                '"position":[0,0,5],"color":"silver"}'
                "]}"
            ),
        ]
    )
    planner = AssemblyPlanner()
    planner._client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )

    result = await planner.plan_assembly(_plan(), allow_fallback=False)

    assert [part.name for part in result.parts] == ["base", "cover"]
    assert len(completions.calls) == 2
    assert all(
        call["response_format"] == {"type": "json_object"}
        for call in completions.calls
    )


@pytest.mark.asyncio
async def test_durable_assembly_planning_fails_closed_after_invalid_responses():
    completions = _CompletionsStub(
        [_response(""), _response(None, finish_reason="length")]
    )
    planner = AssemblyPlanner()
    planner._client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )

    with pytest.raises(
        ValueError,
        match="assembly planner model did not return a valid assembly plan",
    ):
        await planner.plan_assembly(_plan(), allow_fallback=False)

    assert len(completions.calls) == 2


@pytest.mark.asyncio
async def test_truncated_assembly_retries_with_configured_budget_and_feedback(monkeypatch):
    monkeypatch.setattr(settings, "planner_max_tokens", 12288)
    completions = _CompletionsStub([
        _response(None, finish_reason="length"),
        _response('{"assembly_description":"盒体", "parts":['
                  '{"name":"box","description":"两腔电子盒",'
                  '"dimensions":{"length":60,"width":40,"height":25,"wall":2}}]}'),
    ])
    planner = AssemblyPlanner()
    planner._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    result = await planner.plan_assembly(_plan(), allow_fallback=False)

    assert result.parts[0].dimensions["wall"] == 2
    assert [call["max_tokens"] for call in completions.calls] == [12288, 12288]
    first, retry = [call["messages"] for call in completions.calls]
    assert first[1] == retry[1]  # Preserve the original requirements on retry.
    assert len(retry) > len(first)
    assert "truncated at 12288" in retry[-1]["content"]


@pytest.mark.asyncio
async def test_exhausted_assembly_reports_actual_truncation_without_fallback(monkeypatch):
    monkeypatch.setattr(settings, "planner_max_tokens", 8192)
    completions = _CompletionsStub([
        _response(None, finish_reason="length"),
        _response(None, finish_reason="length"),
    ])
    planner = AssemblyPlanner()
    planner._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    with pytest.raises(ValueError, match="truncated at 8192 completion tokens") as error:
        await planner.plan_assembly(_plan(), allow_fallback=False)

    assert "valid assembly plan" in str(error.value)
    assert error.value.__cause__ is not None
    assert len(completions.calls) == 2
