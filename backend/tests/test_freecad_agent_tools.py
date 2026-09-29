"""Protocol tests use scripted provider turns; these are not model-success evidence."""
import copy
import json
from types import SimpleNamespace

import jsonschema
import pytest
from openai.types.chat import ChatCompletion

from app.freecad.agent_tools import read_tool, tool_schemas
from app.contracts.cad_tools import execution_receipt
from app.freecad.operation_generator import FreeCADOperationGenerator
from tests.test_freecad_operation_generator import _plan, _provenance, _valid_plan


def call(name, arguments, identity="call_1"):
    return {"id": identity, "type": "function", "function": {"name": name,
            "arguments": json.dumps(arguments)}}


class ProviderTurns:
    def __init__(self, *turns):
        self.turns = iter(turns)
        self.requests = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        calls = next(self.turns)
        return ChatCompletion(id=f"response-{len(self.requests)}", model="protocol-fixture", created=1,
            object="chat.completion", choices=[{"index": 0, "finish_reason": "tool_calls",
            "message": {"role": "assistant", "tool_calls": calls,
                        "reasoning_content": "synthetic protocol context"}}])


async def generate(provider, state=None):
    return await FreeCADOperationGenerator(client=provider, provenance_reader=_provenance)._complete(
        user_payload={"task": "modify" if state else "generate"}, generator_kind="freecad_operations",
        output_formats=("step", "stl"), base_state=state)


@pytest.mark.asyncio
async def test_discover_describe_and_dispatch_actual_api_operation_contract():
    plan = {"operations": [
        {"op_id": "api", "action": "api.execute", "args": {"source": "document.addObject('Part::Box', 'Box')"}},
        {"op_id": "export", "action": "document.export", "args": {"objects": ["Box"], "formats": ["fcstd", "step", "stl"]}}]}
    provider = ProviderTurns(
        [call("freecad_discover", {"module": "Part", "symbol": "makeHelix"}, "discover")],
        [call("freecad_describe_operation", {"action": "api.execute"}, "describe")],
        [call("freecad_execute", plan, "build")])
    result = await generate(provider)
    assert result.provenance["execution_tool_call"]["id"] == "build"
    assert "tool_result" not in result.provenance  # Dispatch is not execution.
    assert result.operation_plan.operations[0].action == "api.execute"
    messages = provider.requests[-1]["messages"]
    replies = [m for m in messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in replies] == ["discover", "describe"]
    assert "Part.makeHelix" in replies[0]["content"]
    assert json.loads(replies[1]["content"])["parameters"]["properties"]["source"]
    assert provider.requests[0]["tool_choice"] == "auto"
    assert all(m['reasoning_content']=='synthetic protocol context' for m in messages if m['role']=='assistant')
    assert "response_format" not in provider.requests[0]
    assert 'Return exactly one JSON object' not in provider.requests[0]['messages'][0]['content']
    assert 'call freecad_execute' in json.loads(provider.requests[0]['messages'][1]['content'])['next_response']
    assert not any(k.startswith("max_") for k in provider.requests[0])


@pytest.mark.asyncio
async def test_inspection_can_page_beyond_three_calls_without_losing_measured_values():
    provider = ProviderTurns(*[
        [call("freecad_inspect", {"objects": ["Pad"], "fields": ["properties"], "offset": i, "limit": 1}, f"read_{i}")]
        for i in range(4)], [call("freecad_execute", _valid_plan(), "build")])
    state = {"objects": [{"name": "Pad", "properties": {f"p{i}": i for i in range(4)}}]}
    result = await generate(provider, state)
    assert len(result.provenance["inspection_calls"]) == 4
    assert [x["result"]["objects"][0]["properties"]["items"][0]["value"]
            for x in result.provenance["inspection_calls"]] == [0, 1, 2, 3]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    [call("commit_revision", {"confirmed": True})],
    [call("freecad_execute", {**_valid_plan(), "confirmed": True})],
    [call("freecad_execute", _valid_plan(), "a"), call("freecad_execute", _valid_plan(), "b")],
])
async def test_invalid_calls_receive_matched_errors_without_execution(bad):
    provider = ProviderTurns(bad, [call("freecad_execute", _valid_plan(), "corrected")])
    result = await generate(provider)
    replies = [m for m in provider.requests[-1]["messages"] if m["role"] == "tool"]
    assert {m["tool_call_id"] for m in replies} == {c["id"] for c in bad}
    assert all(json.loads(m["content"])["executed"] is False for m in replies)
    assert result.provenance["execution_tool_call"]["id"] == "corrected"


@pytest.mark.asyncio
async def test_duplicate_inspection_is_rejected_without_unbounded_paid_loop():
    query = {"objects": ["Pad"], "fields": ["properties"]}
    provider = ProviderTurns(*[[call("freecad_inspect", query, str(i))] for i in range(3)])
    with pytest.raises(ValueError, match="repeated without new information"):
        await generate(provider, {"objects": [{"name": "Pad", "properties": {"Length": 10}}]})
    assert len(provider.requests) == 3


def test_schemas_reuse_runtime_contract_and_do_not_expose_authority_fields():
    for spec in tool_schemas(has_document=True):
        schema = spec["function"]["parameters"]
        jsonschema.Draft202012Validator.check_schema(schema)
        assert not {"confirmed", "tenant_id", "principal_id", "object_key"} & schema["properties"].keys()
    assert "freecad_inspect" not in {t["function"]["name"] for t in tool_schemas(has_document=False)}
    described = read_tool("freecad_describe_operation", {"action": "feature.chamfer"}, state=None)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"name": "C", "target": "Body", "size_mm": 1, "edge_scope": "outer"}, described["parameters"])


def test_receipt_does_not_claim_independent_validation_or_saved_version():
    with pytest.raises(ValueError, match="native artifact"):
        execution_receipt(source_id="s", source_hash="h", attempt_id="a", outputs=[])
    result = execution_receipt(source_id="s", source_hash="h", attempt_id="a", outputs=[{"format": "fcstd"}])
    assert result["status"] == "executed" and result["engineering_validation"] == "pending"
    assert result["saved_revision"] is None
