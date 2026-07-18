from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.api.auth import rate_limiter, verify_api_key
from app.fusion360.agent_api import get_agent_planner, get_agent_store, router
from app.fusion360.agent_contract import AGENT_CONTRACT_VERSION, context_fingerprint
from app.fusion360.agent_planner import AgentPlanner, AgentPlanningError
from app.fusion360.agent_store import AgentStore
from app.fusion360.contract import ContextData


def _context(*, truncated: bool = False) -> ContextData:
    return ContextData.model_validate(
        {
            "document": {
                "document_id": "doc-1", "name": "Part", "document_type": "design",
                "is_saved": True, "is_modified": False, "is_read_only": False,
            },
            "design": {"design_type": "parametric", "root_component_id": "component-root", "units": "mm"},
            "components": [{"id": "component-root", "name": "Root", "kind": "component"}],
            "selection": [{"id": "face-1", "name": "Face", "kind": "face", "component_id": "component-root"}],
            "parameters": [
                {
                    "id": "parameter-1", "name": "width", "expression": "2 mm", "unit": "mm",
                    "is_user_parameter": True, "component_id": "component-root",
                }
            ],
            "features": [
                {"id": "feature-1", "name": "Extrude", "kind": "extrude", "component_id": "component-root"}
            ],
            "sketches": [
                {
                    "id": "sketch-1", "name": "Sketch", "kind": "sketch", "component_id": "component-root",
                    "vendor_extensions": {"profile_ids": ["profile-1"], "sketch_point_ids": ["point-1"]},
                }
            ],
            "bodies": [
                {
                    "id": "body-1", "name": "Body", "kind": "body", "component_id": "component-root",
                    "vendor_extensions": {"edge_ids": ["edge-1"]},
                }
            ],
            "materials": [{"id": "material-1", "name": "Steel", "library_id": "library-1", "component_id": "component-root"}],
            "cloud": {"folder_id": "folder-1"},
            "truncated": truncated,
        }
    )


def _turn(request_id: uuid.UUID | None = None, *, context: ContextData | None = None, **updates):
    request_id = request_id or uuid.uuid4()
    context = context or _context()
    body = {
        "contract_version": AGENT_CONTRACT_VERSION,
        "request_id": str(request_id),
        "connector_instance_id": str(uuid.uuid4()),
        "prompt": "Set width to 3 mm",
        "context": context.model_dump(mode="json"),
        "context_fingerprint": context_fingerprint(context),
        "capabilities": {
            "adapter": "fusion360", "available": True, "connector_online": True,
            "actions": ["cad.update_parameter", "cad.export"],
            "context_sections": ["document", "components", "parameters"],
        },
    }
    body.update(updates)
    return body


class _Completion:
    def __init__(self, content: str):
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=content))]


class _Client:
    def __init__(self, content: str):
        self.content = content
        self.calls = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return _Completion(self.content)


@pytest.mark.asyncio
async def test_planner_rejects_non_json_arbitrary_code_and_unknown_context_ids():
    turn = _turn()
    from app.fusion360.agent_contract import AgentTurnRequest

    parsed_turn = AgentTurnRequest.model_validate(turn)
    with pytest.raises(AgentPlanningError, match="JSON"):
        await AgentPlanner(client=_Client("```python\nprint(1)\n```"), model="test").plan(parsed_turn)

    code = json.dumps({"status": "proposed", "action": {"action": "cad.run_python", "source": "print(1)"}})
    with pytest.raises(AgentPlanningError):
        await AgentPlanner(client=_Client(code), model="test").plan(parsed_turn)

    missing = json.dumps(
        {
            "status": "proposed",
            "action": {
                "action": "cad.update_parameter",
                "target": {"document_id": "doc-1", "component_id": "component-root", "parameter_id": "invented"},
                "value": {"amount": 3, "unit": "mm"},
            },
        }
    )
    with pytest.raises(AgentPlanningError, match="context"):
        await AgentPlanner(client=_Client(missing), model="test").plan(parsed_turn)


@pytest.mark.asyncio
async def test_planner_overrides_server_owned_fields_and_requires_complete_context():
    turn_body = _turn()
    from app.fusion360.agent_contract import AgentTurnRequest

    turn = AgentTurnRequest.model_validate(turn_body)
    output = json.dumps(
        {
            "status": "proposed",
            "action": {
                "action": "cad.update_parameter",
                "request_id": str(uuid.uuid4()),
                "connector_instance_id": str(uuid.uuid4()),
                "execution_mode": "execute",
                "approval_id": str(uuid.uuid4()),
                "idempotency_key": "model-owned",
                "target": {"document_id": "doc-1", "component_id": "component-root", "parameter_id": "parameter-1"},
                "value": {"amount": 3, "unit": "mm"},
            },
        }
    )
    plan = await AgentPlanner(client=_Client(output), model="test").plan(turn)
    assert plan.action and plan.action.request_id == turn.request_id
    assert plan.action.connector_instance_id == turn.connector_instance_id
    assert plan.action.execution_mode == "preview"
    assert plan.action.approval_id is None and plan.action.idempotency_key is None

    truncated = AgentTurnRequest.model_validate(_turn(context=_context(truncated=True)))
    clarification = await AgentPlanner(client=_Client(output), model="test").plan(truncated)
    assert clarification.status == "needs_clarification"
    assert clarification.action is None


@pytest.mark.asyncio
async def test_planner_supplies_the_normative_action_schema_to_the_model():
    from app.fusion360.agent_contract import AgentTurnRequest

    output = json.dumps(
        {
            "status": "proposed",
            "action": {
                "action": "cad.update_parameter",
                "target": {
                    "document_id": "doc-1",
                    "component_id": "component-root",
                    "parameter_id": "parameter-1",
                },
                "value": {"amount": 3, "unit": "mm"},
            },
        }
    )
    client = _Client(output)
    await AgentPlanner(client=client, model="test").plan(AgentTurnRequest.model_validate(_turn()))

    messages = client.calls[0]["messages"]
    payload = json.loads(messages[1]["content"])
    assert payload["cad_action_json_schema"]["$defs"]["DimensionValue"]["oneOf"]
    assert "exactly one representation" in messages[0]["content"]


@pytest.mark.asyncio
async def test_semantic_id_ownership_covers_feature_tree_creation_property_and_cloud_targets():
    from app.fusion360.agent_contract import AgentTurnRequest

    context_json = _context().model_dump(mode="json")
    context_json["parameters"][0]["created_by_id"] = "feature-1"
    context = ContextData.model_validate(context_json)
    actions = [
        {
            "action": "cad.update_feature_parameter",
            "target": {
                "document_id": "doc-1", "component_id": "component-root",
                "feature_id": "feature-1", "parameter_id": "parameter-1",
            },
            "value": {"amount": 4, "unit": "mm"},
        },
        {
            "action": "cad.create_sketch",
            "target": {"document_id": "doc-1", "component_id": "component-root"},
            "plane": {"kind": "entity", "entity_id": "face-1"},
            "primitives": [{"kind": "circle", "center": {"x": 0, "y": 0}, "radius": 2}],
        },
        {
            "action": "cad.create_extrude",
            "target": {"document_id": "doc-1", "component_id": "component-root"},
            "profile_id": "profile-1", "distance": {"amount": 2, "unit": "mm"},
        },
        {
            "action": "cad.create_hole",
            "target": {"document_id": "doc-1", "component_id": "component-root"},
            "sketch_point_ids": ["point-1"], "diameter": {"amount": 2, "unit": "mm"},
            "extent": {"kind": "through_all"},
        },
        {
            "action": "cad.create_fillet",
            "target": {"document_id": "doc-1", "component_id": "component-root"},
            "edge_ids": ["edge-1"], "radius": {"amount": 1, "unit": "mm"},
        },
        {
            "action": "cad.create_chamfer",
            "target": {"document_id": "doc-1", "component_id": "component-root"},
            "edge_ids": ["edge-1"], "distance": {"amount": 1, "unit": "mm"},
        },
        {
            "action": "cad.update_entity_properties",
            "target": {
                "document_id": "doc-1", "component_id": "component-root",
                "entity_id": "body-1", "entity_kind": "body",
            },
            "properties": {"material": {"library_id": "library-1", "material_id": "material-1"}},
        },
        {
            "action": "cad.save_as", "target": {"document_id": "doc-1"},
            "data_folder_id": "folder-1", "name": "Copy",
        },
        {
            "action": "cad.export", "target": {"document_id": "doc-1"},
            "format": "stl", "filename": "part.stl", "options": {"body_id": "body-1"},
        },
    ]
    for action in actions:
        body = _turn(
            context=context,
            capabilities={
                "adapter": "fusion360", "available": True, "connector_online": True,
                "actions": [action["action"]], "context_sections": ["document"],
            },
        )
        turn = AgentTurnRequest.model_validate(body)
        output = json.dumps({"status": "proposed", "action": action})
        response = await AgentPlanner(client=_Client(output), model="test").plan(turn)
        assert response.status == "proposed"
        assert response.action and response.action.action == action["action"]


@pytest.fixture
def api_app(tmp_path: Path):
    store = AgentStore(tmp_path / "agent.db", artifact_root=tmp_path / "artifacts", max_artifact_bytes=1024)
    output = json.dumps(
        {
            "status": "proposed",
            "reason": "Update the requested parameter",
            "action": {
                "action": "cad.update_parameter",
                "target": {"document_id": "doc-1", "component_id": "component-root", "parameter_id": "parameter-1"},
                "value": {"amount": 3, "unit": "mm"},
            },
        }
    )
    planner = AgentPlanner(client=_Client(output), model="test")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[verify_api_key] = lambda: "user:test"
    app.dependency_overrides[get_agent_planner] = lambda: planner
    app.dependency_overrides[get_agent_store] = lambda: store
    yield app, store
    store.close()


@pytest.mark.asyncio
async def test_plan_endpoint_is_authenticated_dependency_compatible_and_idempotent(api_app):
    app, _ = api_app
    request_id = uuid.uuid4()
    body = _turn(request_id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/api/cad/fusion360/agent/plan", json=body)
        second = await client.post("/api/cad/fusion360/agent/plan", json=body)
    assert first.status_code == 200
    assert first.json() == second.json()
    assert first.json()["action"]["execution_mode"] == "preview"


@pytest.mark.asyncio
async def test_heartbeat_provides_owner_scoped_connector_and_fusion_status(api_app):
    app, store = api_app
    connector_id = str(uuid.uuid4())
    heartbeat = {
        "contract_version": "1.0.0",
        "connector_instance_id": connector_id,
        "fusion_version": "2.x",
        "addin_version": "1.0.0",
        "platform": "macos",
        "capabilities": {
            "adapter": "fusion360",
            "available": True,
            "connector_online": True,
            "fusion_running": True,
            "actions": ["cad.update_parameter"],
            "context_sections": ["document", "parameters"],
        },
        "sent_at": "2030-01-01T00:00:00Z",
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        receipt = await client.post("/api/cad/fusion360/agent/heartbeat", json=heartbeat)
        online = await client.get(f"/api/cad/fusion360/agent/connectors/{connector_id}/status")
        unknown = await client.get(
            f"/api/cad/fusion360/agent/connectors/{uuid.uuid4()}/status"
        )
        store._db.execute(
            "UPDATE agent_connectors SET last_seen_at=last_seen_at-60 WHERE owner_id=? AND connector_instance_id=?",
            ("user:test", connector_id),
        )
        offline = await client.get(f"/api/cad/fusion360/agent/connectors/{connector_id}/status")

    assert receipt.status_code == 200 and receipt.json()["status"] == "online"
    assert online.json()["connector_online"] is True
    assert online.json()["fusion_running"] is True
    assert unknown.json()["connector_online"] is False
    assert unknown.json()["fusion_running"] is None
    assert offline.json()["connector_online"] is False
    assert offline.json()["fusion_running"] is None


def test_main_application_registers_direct_agent_routes():
    from app.main import create_app

    paths = {route.path for route in create_app().routes}
    assert {
        "/api/cad/fusion360/agent/heartbeat",
        "/api/cad/fusion360/agent/plan",
        "/api/cad/fusion360/agent/results",
        "/api/cad/fusion360/agent/artifacts/{request_id}/{filename}",
    } <= paths


@pytest.mark.asyncio
async def test_result_audit_is_idempotent_and_bound_to_plan(api_app):
    app, store = api_app
    body = _turn(export_artifact_upload_consent=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        plan_response = (await client.post("/api/cad/fusion360/agent/plan", json=body)).json()
        action_json = plan_response["action"]
        from app.fusion360.contract import CAD_ACTION_ADAPTER
        from app.fusion360.policy import action_intent_hash
        action = CAD_ACTION_ADAPTER.validate_python(action_json)
        report = {
            "contract_version": AGENT_CONTRACT_VERSION,
            "report_id": str(uuid.uuid4()),
            "request_id": body["request_id"],
            "proposal_id": plan_response["proposal_id"],
            "connector_instance_id": body["connector_instance_id"],
            "context_fingerprint": body["context_fingerprint"],
            "action_intent_hash": action_intent_hash(action),
            "completed_at": "2030-01-01T00:00:00Z",
            "result": {
                "request_id": body["request_id"], "status": "success", "action": "cad.update_parameter",
                "data": {"kind": "mutation", "snapshot_id": "snapshot-1"},
                "changes": [{
                    "target_id": "parameter-1", "path": "parameter.expression",
                    "kind": "updated", "before": "2 mm", "after": "3 mm",
                }],
                "verification": {
                    "passed": True,
                    "checks": [{"check": "parameter_equals", "passed": True}],
                    "compute_completed": True,
                    "new_feature_errors": [],
                },
            },
        }
        first = await client.post("/api/cad/fusion360/agent/results", json=report)
        second = await client.post("/api/cad/fusion360/agent/results", json=report)
        changed = await client.post(
            "/api/cad/fusion360/agent/results",
            json={**report, "result": {**report["result"], "status": "failed", "error": {"code": "X", "message": "x", "category": "x", "retryable": False}}},
        )
    assert first.status_code == 200 and first.json()["status"] == "accepted"
    assert second.status_code == 200 and second.json()["status"] == "duplicate"
    assert changed.status_code == 409
    audit_dump = "\n".join(store._db.iterdump())
    assert "Set width to 3 mm" not in audit_dump
    assert "snapshot-1" not in audit_dump
    assert "/tmp/" not in audit_dump


@pytest.mark.asyncio
async def test_result_rejects_private_snapshot_and_local_artifact_evidence(api_app):
    app, _ = api_app
    body = _turn()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        plan_response = (await client.post("/api/cad/fusion360/agent/plan", json=body)).json()
        from app.fusion360.contract import CAD_ACTION_ADAPTER
        from app.fusion360.policy import action_intent_hash

        action = CAD_ACTION_ADAPTER.validate_python(plan_response["action"])
        base_report = {
            "contract_version": AGENT_CONTRACT_VERSION,
            "report_id": str(uuid.uuid4()),
            "request_id": body["request_id"],
            "proposal_id": plan_response["proposal_id"],
            "connector_instance_id": body["connector_instance_id"],
            "context_fingerprint": body["context_fingerprint"],
            "action_intent_hash": action_intent_hash(action),
            "completed_at": "2030-01-01T00:00:00Z",
            "result": {
                "request_id": body["request_id"], "status": "success", "action": "cad.update_parameter",
                "data": {"kind": "mutation", "snapshot_id": "opaque-id"},
                "changes": [{
                    "target_id": "parameter-1", "path": "parameter.expression",
                    "kind": "updated", "before": "2 mm", "after": "3 mm",
                }],
                "verification": {
                    "passed": True,
                    "checks": [{"check": "parameter_equals", "passed": True}],
                    "compute_completed": True,
                    "new_feature_errors": [],
                },
            },
        }
        snapshot = await client.post(
            "/api/cad/fusion360/agent/results",
            json={**base_report, "result": {**base_report["result"], "_snapshot": {"local_path": "/tmp/private"}}},
        )
        local_artifacts = await client.post(
            "/api/cad/fusion360/agent/results",
            json={**base_report, "report_id": str(uuid.uuid4()), "result": {**base_report["result"], "_local_artifacts": []}},
        )
    assert snapshot.status_code == 422
    assert local_artifacts.status_code == 422
    assert "/tmp/private" not in snapshot.text


@pytest.mark.asyncio
async def test_artifact_upload_requires_matching_export_consent_hash_and_f3d_claim(api_app):
    app, _ = api_app
    request_id = uuid.uuid4()
    no_export = _turn(request_id)
    content = b"ISO-10303-21;\nEND-ISO-10303-21;"
    digest = hashlib.sha256(content).hexdigest()
    headers = {"X-Artifact-SHA256": digest, "X-Artifact-Size": str(len(content)), "Content-Type": "model/step"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/cad/fusion360/agent/plan", json=no_export)
        rejected = await client.put(
            f"/api/cad/fusion360/agent/artifacts/{request_id}/part.step", content=content, headers=headers
        )
    assert rejected.status_code == 403


def _set_planner_output(app: FastAPI, output: dict):
    planner = app.dependency_overrides[get_agent_planner]()
    planner._client.content = json.dumps(output)


async def _create_export_report(client, body, plan_response, export_format: str):
    from app.fusion360.contract import CAD_ACTION_ADAPTER
    from app.fusion360.policy import action_intent_hash

    action = CAD_ACTION_ADAPTER.validate_python(plan_response["action"])
    report = {
        "contract_version": AGENT_CONTRACT_VERSION,
        "report_id": str(uuid.uuid4()),
        "request_id": body["request_id"],
        "proposal_id": plan_response["proposal_id"],
        "connector_instance_id": body["connector_instance_id"],
        "context_fingerprint": body["context_fingerprint"],
        "action_intent_hash": action_intent_hash(action),
        "completed_at": "2030-01-01T00:00:00Z",
        "result": {
            "request_id": body["request_id"],
            "status": "success",
            "action": "cad.export",
            "data": {"kind": "export", "format": export_format, "artifact_ids": ["artifact-1"]},
            "verification": {
                "passed": True,
                "checks": [{"check": "artifact_valid", "passed": True}],
                "compute_completed": True,
                "new_feature_errors": [],
            },
        },
    }
    return await client.post("/api/cad/fusion360/agent/results", json=report)


@pytest.mark.asyncio
async def test_request_scoped_step_upload_streams_and_is_idempotent(api_app):
    app, _ = api_app
    _set_planner_output(
        app,
        {
            "status": "proposed",
            "action": {
                "action": "cad.export",
                "target": {"document_id": "doc-1"},
                "format": "step",
                "filename": "part.step",
            },
        },
    )
    body = _turn(export_artifact_upload_consent=True)
    content = b"ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;"
    digest = hashlib.sha256(content).hexdigest()
    headers = {"X-Artifact-SHA256": digest, "X-Artifact-Size": str(len(content)), "Content-Type": "model/step"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        planned = await client.post("/api/cad/fusion360/agent/plan", json=body)
        assert planned.status_code == 200
        reported = await _create_export_report(client, body, planned.json(), "step")
        first = await client.put(
            f"/api/cad/fusion360/agent/artifacts/{body['request_id']}/part.step", content=content, headers=headers
        )
        second = await client.put(
            f"/api/cad/fusion360/agent/artifacts/{body['request_id']}/part.step", content=content, headers=headers
        )
        downloaded = await client.get(first.json()["download_url"])
    assert reported.status_code == 200
    assert first.status_code == 200 and first.json()["status"] == "accepted"
    assert second.status_code == 200 and second.json()["status"] == "duplicate"
    assert downloaded.status_code == 200 and downloaded.content == content
    assert first.json()["download_url"].endswith(f"/{body['request_id']}/part.step")


@pytest.mark.asyncio
async def test_full_f3d_upload_requires_the_separate_authorization_claim(api_app):
    app, _ = api_app
    _set_planner_output(
        app,
        {
            "status": "proposed",
            "action": {
                "action": "cad.export",
                "target": {"document_id": "doc-1"},
                "format": "f3d",
                "filename": "archive.f3d",
            },
        },
    )
    body = _turn(export_artifact_upload_consent=True, f3d_upload_authorized=False)
    content = b"PK\x03\x04" + b"fusion-archive"
    headers = {
        "X-Artifact-SHA256": hashlib.sha256(content).hexdigest(),
        "X-Artifact-Size": str(len(content)),
        "Content-Type": "application/vnd.autodesk.fusion360",
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        planned = (await client.post("/api/cad/fusion360/agent/plan", json=body)).json()
        await _create_export_report(client, body, planned, "f3d")
        rejected = await client.put(
            f"/api/cad/fusion360/agent/artifacts/{body['request_id']}/archive.f3d",
            content=content,
            headers=headers,
        )
    assert rejected.status_code == 403
    assert rejected.json()["error"]["code"] == "AGENT_F3D_AUTHORIZATION_REQUIRED"


@pytest.mark.asyncio
async def test_agent_rate_limit_is_structured(api_app, monkeypatch):
    app, _ = api_app

    async def reject(_request, _credential):
        raise HTTPException(status_code=429, detail="limited")

    monkeypatch.setattr(rate_limiter, "check", reject)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/cad/fusion360/agent/plan", json=_turn())
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "AGENT_RATE_LIMITED"


@pytest.mark.auth
@pytest.mark.asyncio
async def test_agent_plan_requires_backend_authentication(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "api_keys", [])
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/cad/fusion360/agent/plan", json=_turn())
    assert response.status_code == 401
    assert "detail" not in response.json()
    assert response.json()["error"] == {
        "code": "AGENT_AUTH_REQUIRED",
        "message": "Fusion Cloud Agent authentication is required",
        "retryable": False,
    }


@pytest.mark.asyncio
async def test_missing_llm_credentials_is_not_replaced_by_a_static_plan(monkeypatch):
    from app.fusion360 import agent_api

    monkeypatch.setattr(agent_api.settings, "moonshot_api_key", None)
    monkeypatch.setattr(agent_api.settings, "llm_provider", "moonshot")
    agent_api._planner_cache = None
    with pytest.raises(HTTPException) as exc_info:
        agent_api.get_agent_planner()
    assert exc_info.value.status_code == 503
    assert "MOONSHOT_API_KEY" in str(exc_info.value.detail)
