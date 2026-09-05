from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent.assembly_planner import AssemblyPlanner
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
