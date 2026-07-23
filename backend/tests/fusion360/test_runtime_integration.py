import hashlib
import uuid
from pathlib import Path

import httpx
import pytest

from app.fusion360.runtime_app import RuntimeConfig, create_runtime_app


BACKEND_HEADERS = {"Authorization": "Bearer backend-test"}
CONNECTOR_HEADERS = {"Authorization": "Bearer connector-test"}


def _registration(app, connector_id):
    return {
        "connector_instance_id": connector_id,
        "protocol_versions": [1],
        "fusion_version": "2.0.1",
        "addin_version": "1.0.0",
        "platform": "macos",
        "artifact_root_fingerprint": app.state.artifacts.fingerprint,
        "capabilities": {
            "adapter": "fusion360", "contract_version": "1.0.0", "protocol_versions": [1],
            "available": True, "runtime_online": True, "connector_online": True,
            "fusion_version": "2.0.1", "actions": ["cad.export", "cad.update_parameter"],
            "context_sections": ["application"], "limitations": [],
        },
    }


@pytest.fixture
def app(tmp_path):
    return create_runtime_app(RuntimeConfig(
        database_path=tmp_path / "runtime.db",
        artifact_root=tmp_path / "artifacts",
        backend_secret="backend-test",
        connector_secret="connector-test",
    ))


@pytest.mark.asyncio
async def test_role_credentials_and_full_export_task_lifecycle(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runtime") as client:
        invalid = await client.post(
            "/v1/connector/register", headers=CONNECTOR_HEADERS,
            json={"connector_instance_id": "not-a-uuid"},
        )
        assert invalid.status_code == 422
        assert "detail" not in invalid.json()
        assert invalid.json()["error"]["code"] == "INVALID_ACTION"
        wrong = await client.get("/v1/backend/status", headers=CONNECTOR_HEADERS)
        assert wrong.status_code == 403
        connector_id = str(uuid.uuid4())
        registered = await client.post("/v1/connector/register", headers=CONNECTOR_HEADERS, json=_registration(app, connector_id))
        assert registered.status_code == 200

        request_id = str(uuid.uuid4())
        action = {
            "request_id": request_id,
            "action": "cad.export",
            "target": {"document_id": "doc"},
            "format": "step",
            "filename": "part.step",
        }
        submitted = await client.post(
            "/v1/backend/tasks", headers=BACKEND_HEADERS,
            json={"owner_id": "owner-a", "operation": "execute", "payload": action, "wait": False},
        )
        assert submitted.status_code == 200
        assert submitted.json()["status"] == "queued"

        leased = await client.get(f"/v1/connector/{connector_id}/tasks/next", headers=CONNECTOR_HEADERS)
        task = leased.json()
        identity = {
            "connector_instance_id": connector_id,
            "request_id": request_id,
            "lease_id": task["lease_id"],
            "attempt": task["attempt"],
            "intent_hash": task["intent_hash"],
        }
        started = await client.post(f"/v1/connector/tasks/{request_id}/started", headers=CONNECTOR_HEADERS, json=identity)
        assert started.json()["status"] == "running"
        artifact_id = str(uuid.uuid4())
        content = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
        artifact_path = Path(task["execution_context"]["artifact_dir"]) / "part.step"
        artifact_path.write_bytes(content)
        result = {
            **identity,
            "result": {
                "request_id": request_id, "status": "success", "action": "cad.export",
                "data": {"kind": "export", "format": "step", "artifact_ids": [artifact_id]},
                "changes": [], "warnings": [], "verification": {
                    "passed": True,
                    "checks": [{"check": "artifact_valid", "passed": True, "actual": True}],
                    "compute_completed": True,
                    "new_feature_errors": [],
                },
                "artifacts": [], "approval": None, "error": None,
            },
            "local_artifacts": [{
                "artifact_id": artifact_id, "kind": "step", "filename": "part.step",
                "media_type": "model/step", "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(), "relative_path": "part.step",
            }],
        }
        completed = await client.post(f"/v1/connector/tasks/{request_id}/result", headers=CONNECTOR_HEADERS, json=result)
        assert completed.status_code == 200
        fetched = await client.get(
            f"/v1/backend/requests/{request_id}",
            headers={**BACKEND_HEADERS, "X-Owner-ID": "owner-a"},
        )
        assert fetched.json()["status"] == "success"
        assert fetched.json()["artifacts"][0]["sha256"] == hashlib.sha256(content).hexdigest()
        denied = await client.get(
            f"/v1/backend/requests/{request_id}",
            headers={**BACKEND_HEADERS, "X-Owner-ID": "owner-b"},
        )
        assert denied.status_code == 404

        missing_id = str(uuid.uuid4())
        missing_action = {
            **action, "request_id": missing_id, "idempotency_key": "missing-artifact",
        }
        await client.post(
            "/v1/backend/tasks", headers=BACKEND_HEADERS,
            json={"owner_id": "owner-a", "operation": "execute", "payload": missing_action, "wait": False},
        )
        missing_lease = (await client.get(
            f"/v1/connector/{connector_id}/tasks/next", headers=CONNECTOR_HEADERS
        )).json()
        missing_identity = {
            "connector_instance_id": connector_id, "request_id": missing_id,
            "lease_id": missing_lease["lease_id"], "attempt": missing_lease["attempt"],
            "intent_hash": missing_lease["intent_hash"],
        }
        await client.post(
            f"/v1/connector/tasks/{missing_id}/started",
            headers=CONNECTOR_HEADERS, json=missing_identity,
        )
        missing_result = await client.post(
            f"/v1/connector/tasks/{missing_id}/result",
            headers=CONNECTOR_HEADERS,
            json={
                **missing_identity,
                "result": {
                    "request_id": missing_id, "status": "success", "action": "cad.export",
                    "data": {"kind": "export", "format": "step", "artifact_ids": []},
                    "changes": [], "warnings": [],
                    "verification": {
                        "passed": False,
                        "checks": [{"check": "computeAll", "passed": False, "actual": False}],
                        "compute_completed": False,
                        "new_feature_errors": ["export-compute-error"],
                    },
                    "artifacts": [], "approval": None, "error": None,
                },
            },
        )
        assert missing_result.json()["status"] == "failed"
        assert missing_result.json()["error"]["code"] == "ARTIFACT_INVALID"
        assert missing_result.json()["verification"]["compute_completed"] is False
        assert missing_result.json()["verification"]["new_feature_errors"] == ["export-compute-error"]

        verify_id = str(uuid.uuid4())
        submitted_verify = await client.post(
            "/v1/backend/tasks", headers=BACKEND_HEADERS,
            json={
                "owner_id": "owner-a", "operation": "verify", "wait": False,
                "payload": {
                    "request_id": verify_id,
                    "specification": {
                        "document_id": "doc", "source_request_id": request_id,
                        "checks": [{"check": "artifact_valid", "artifact_id": artifact_id}],
                    },
                },
            },
        )
        assert submitted_verify.json()["status"] == "queued"
        verify_lease = (await client.get(
            f"/v1/connector/{connector_id}/tasks/next", headers=CONNECTOR_HEADERS
        )).json()
        evidence = verify_lease["payload"]["_references"]["artifact_evidence"][artifact_id]
        assert evidence["valid"] is True
        assert evidence["sha256"] == hashlib.sha256(content).hexdigest()


@pytest.mark.asyncio
async def test_export_with_a_real_artifact_but_failed_rebuild_evidence_is_not_success(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runtime") as client:
        connector_id = str(uuid.uuid4())
        await client.post(
            "/v1/connector/register",
            headers=CONNECTOR_HEADERS,
            json=_registration(app, connector_id),
        )
        request_id = str(uuid.uuid4())
        await client.post(
            "/v1/backend/tasks",
            headers=BACKEND_HEADERS,
            json={
                "owner_id": "owner-a",
                "operation": "execute",
                "wait": False,
                "payload": {
                    "request_id": request_id,
                    "action": "cad.export",
                    "target": {"document_id": "doc"},
                    "format": "step",
                    "filename": "unverified.step",
                },
            },
        )
        lease = (
            await client.get(
                f"/v1/connector/{connector_id}/tasks/next", headers=CONNECTOR_HEADERS
            )
        ).json()
        identity = {
            "connector_instance_id": connector_id,
            "request_id": request_id,
            "lease_id": lease["lease_id"],
            "attempt": lease["attempt"],
            "intent_hash": lease["intent_hash"],
        }
        await client.post(
            f"/v1/connector/tasks/{request_id}/started",
            headers=CONNECTOR_HEADERS,
            json=identity,
        )
        artifact_id = str(uuid.uuid4())
        content = b"ISO-10303-21;\nEND-ISO-10303-21;\n"
        path = Path(lease["execution_context"]["artifact_dir"]) / "unverified.step"
        path.write_bytes(content)
        completed = await client.post(
            f"/v1/connector/tasks/{request_id}/result",
            headers=CONNECTOR_HEADERS,
            json={
                **identity,
                "result": {
                    "request_id": request_id,
                    "status": "success",
                    "action": "cad.export",
                    "data": {"kind": "export", "format": "step", "artifact_ids": [artifact_id]},
                    "changes": [],
                    "warnings": [],
                    "verification": {
                        "passed": True,
                        "checks": [{"check": "computeAll", "passed": False, "actual": False}],
                        "compute_completed": False,
                        "new_feature_errors": [],
                    },
                    "artifacts": [],
                    "approval": None,
                    "error": None,
                },
                "local_artifacts": [{
                    "artifact_id": artifact_id,
                    "kind": "step",
                    "filename": "unverified.step",
                    "media_type": "model/step",
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "relative_path": "unverified.step",
                }],
            },
        )

    assert completed.status_code == 200
    assert completed.json()["status"] == "failed"
    assert completed.json()["error"]["code"] == "VERIFICATION_FAILED"


@pytest.mark.asyncio
async def test_preview_creates_bound_approval_and_execute_requires_it(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runtime") as client:
        connector_id = str(uuid.uuid4())
        await client.post("/v1/connector/register", headers=CONNECTOR_HEADERS, json=_registration(app, connector_id))
        base = {
            "action": "cad.update_parameter",
            "idempotency_key": "resize",
            "target": {"document_id": "doc", "component_id": "component", "parameter_id": "parameter"},
            "value": {"amount": 3, "unit": "mm"},
        }
        refused = await client.post(
            "/v1/backend/tasks", headers=BACKEND_HEADERS,
            json={"owner_id": "owner", "operation": "execute", "payload": {**base, "execution_mode": "execute"}, "wait": False},
        )
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "APPROVAL_REQUIRED"

        preview_id = str(uuid.uuid4())
        preview = {**base, "request_id": preview_id, "execution_mode": "preview"}
        await client.post(
            "/v1/backend/tasks", headers=BACKEND_HEADERS,
            json={"owner_id": "owner", "operation": "execute", "payload": preview, "wait": False},
        )
        lease = (await client.get(f"/v1/connector/{connector_id}/tasks/next", headers=CONNECTOR_HEADERS)).json()
        identity = {
            "connector_instance_id": connector_id, "request_id": preview_id,
            "lease_id": lease["lease_id"], "attempt": lease["attempt"], "intent_hash": lease["intent_hash"],
        }
        await client.post(f"/v1/connector/tasks/{preview_id}/started", headers=CONNECTOR_HEADERS, json=identity)
        result = {
            **identity,
            "result": {
                "request_id": preview_id, "status": "success", "action": "cad.update_parameter",
                "data": {"kind": "preview", "planned_changes": [], "verification_plan": ["parameter_equals"]},
                "changes": [], "warnings": [], "verification": {"passed": True, "checks": [], "compute_completed": True, "new_feature_errors": []},
                "artifacts": [], "approval": None, "error": None,
            },
        }
        stale = await client.post(
            f"/v1/connector/tasks/{preview_id}/result",
            headers=CONNECTOR_HEADERS,
            json={**result, "lease_id": str(uuid.uuid4())},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "STALE_LEASE"
        response = await client.post(f"/v1/connector/tasks/{preview_id}/result", headers=CONNECTOR_HEADERS, json=result)
        assert response.json()["status"] == "approval_required"
        assert response.json()["verification"] is None
        approval_id = response.json()["approval"]["approval_id"]
        assert app.state.store.get_task_internal(preview_id)["status"] == "approval_required"
        approval_count = app.state.store._db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
        assert approval_count == 1

        execute_id = str(uuid.uuid4())
        accepted = await client.post(
            "/v1/backend/tasks", headers=BACKEND_HEADERS,
            json={
                "owner_id": "owner", "operation": "execute", "wait": False,
                "payload": {**base, "request_id": execute_id, "execution_mode": "execute", "approval_id": approval_id},
            },
        )
        assert accepted.status_code == 200
        assert accepted.json()["status"] == "queued"

        replay = await client.post(
            "/v1/backend/tasks", headers=BACKEND_HEADERS,
            json={
                "owner_id": "owner", "operation": "execute", "wait": False,
                "payload": {
                    **base, "request_id": str(uuid.uuid4()), "execution_mode": "execute",
                    "approval_id": approval_id,
                },
            },
        )
        assert replay.status_code == 200
        assert replay.json()["request_id"] == execute_id


@pytest.mark.asyncio
async def test_low_risk_export_preview_does_not_issue_approval(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runtime") as client:
        connector_id = str(uuid.uuid4())
        await client.post("/v1/connector/register", headers=CONNECTOR_HEADERS, json=_registration(app, connector_id))
        request_id = str(uuid.uuid4())
        await client.post(
            "/v1/backend/tasks", headers=BACKEND_HEADERS,
            json={
                "owner_id": "owner", "operation": "execute", "wait": False,
                "payload": {
                    "request_id": request_id, "action": "cad.export", "execution_mode": "preview",
                    "target": {"document_id": "doc"}, "format": "step", "filename": "part.step",
                },
            },
        )
        lease = (await client.get(
            f"/v1/connector/{connector_id}/tasks/next", headers=CONNECTOR_HEADERS
        )).json()
        identity = {
            "connector_instance_id": connector_id, "request_id": request_id,
            "lease_id": lease["lease_id"], "attempt": lease["attempt"], "intent_hash": lease["intent_hash"],
        }
        await client.post(
            f"/v1/connector/tasks/{request_id}/started", headers=CONNECTOR_HEADERS, json=identity
        )
        completed = await client.post(
            f"/v1/connector/tasks/{request_id}/result", headers=CONNECTOR_HEADERS,
            json={
                **identity,
                "result": {
                    "request_id": request_id, "status": "success", "action": "cad.export",
                    "data": {"kind": "preview", "planned_changes": [], "verification_plan": ["artifact_valid"]},
                    "changes": [], "warnings": [],
                    "verification": {"passed": True, "checks": [], "compute_completed": True, "new_feature_errors": []},
                    "artifacts": [], "approval": None, "error": None,
                },
            },
        )
        assert completed.json()["status"] == "success"
        assert completed.json()["approval"] is None
        assert app.state.store.get_task_internal(request_id)["status"] == "success"


@pytest.mark.asyncio
async def test_runtime_persists_real_snapshot_and_resolves_snapshot_verify_baseline(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runtime") as client:
        connector_id = str(uuid.uuid4())
        await client.post(
            "/v1/connector/register", headers=CONNECTOR_HEADERS,
            json=_registration(app, connector_id),
        )
        request_id = str(uuid.uuid4())
        snapshot_id = str(uuid.uuid4())
        task, _ = app.state.store.submit_task(
            request_id=request_id,
            owner_id="owner",
            operation="execute",
            phase="execute",
            action_name="cad.update_parameter",
            intent_hash="intent",
            idempotency_key=None,
            payload_hash="payload",
            payload={
                "request_id": request_id,
                "action": "cad.update_parameter",
                "target": {"document_id": "doc"},
            },
            connector_instance_id=connector_id,
            deadline=10**12,
        )
        lease = app.state.store.lease_next(connector_id)
        app.state.store.mark_started(
            connector_id, request_id, lease["lease_id"], lease["attempt"], "intent"
        )
        identity = {
            "connector_instance_id": connector_id,
            "request_id": request_id,
            "lease_id": lease["lease_id"],
            "attempt": lease["attempt"],
            "intent_hash": "intent",
        }
        pre_mutation = {
            "snapshot_id": snapshot_id,
            "document_id": "doc",
            "document_modified": False,
            "feature_errors": ["existing-feature-error"],
            "expression": "2 mm",
        }
        completed = await client.post(
            f"/v1/connector/tasks/{request_id}/result",
            headers=CONNECTOR_HEADERS,
            json={
                **identity,
                "result": {
                    # The Runtime binds public identity to the leased task.
                    "request_id": str(uuid.uuid4()),
                    "status": "success",
                    "action": "cad.update_parameter",
                    "data": {
                        "kind": "mutation", "snapshot_id": snapshot_id,
                        "created_or_updated_entity_ids": ["parameter"],
                    },
                    "changes": [{
                        "target_id": "parameter", "path": "parameter.expression",
                        "kind": "updated", "before": "2 mm", "after": "3 mm",
                    }], "warnings": [],
                    "verification": {
                        "passed": True,
                        "checks": [{"check": "parameter_equals", "passed": True}],
                        "compute_completed": True,
                        "new_feature_errors": [],
                    },
                        "artifacts": [], "approval": None, "error": None,
                    },
                    "snapshot_evidence": pre_mutation,
                },
        )
        assert completed.status_code == 200
        assert completed.json()["request_id"] == request_id
        assert "_snapshot" not in completed.json()
        assert app.state.store.get_snapshot("owner", snapshot_id) == pre_mutation

        verify_id = str(uuid.uuid4())
        submitted = await client.post(
            "/v1/backend/tasks", headers=BACKEND_HEADERS,
            json={
                "owner_id": "owner", "operation": "verify", "wait": False,
                "payload": {
                    "request_id": verify_id,
                    "specification": {
                        "document_id": "doc",
                        "checks": [{
                            "check": "no_new_feature_errors", "snapshot_id": snapshot_id,
                        }],
                    },
                },
            },
        )
        assert submitted.status_code == 200
        verify_lease = (await client.get(
            f"/v1/connector/{connector_id}/tasks/next", headers=CONNECTOR_HEADERS
        )).json()
        assert verify_lease["payload"]["_references"]["snapshot_feature_errors"] == {
            snapshot_id: ["existing-feature-error"],
        }
