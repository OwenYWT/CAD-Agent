"""Explicitly gated end-to-end test against a live disposable Fusion fixture.

Set FUSION_E2E_CONFIG to a JSON file only after reading the manual checklist.
This test performs real writes and restores the parameter in a finally block.
"""

import json
import os
import uuid
from pathlib import Path

import pytest

from app.fusion360.adapter import RemoteFusionAdapter
from app.fusion360.contract import CAD_ACTION_ADAPTER, ContextRequest, VerifyRequest


pytestmark = pytest.mark.fusion_e2e


def _configuration():
    path = os.environ.get("FUSION_E2E_CONFIG")
    if not path:
        pytest.skip("FUSION_E2E_CONFIG is not set; real Fusion E2E was not run")
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _parameter_action(config, value, mode, approval_id=None, suffix="test"):
    raw = {
        "request_id": str(uuid.uuid4()),
        "idempotency_key": f"fusion-e2e-{suffix}-{uuid.uuid4()}",
        "execution_mode": mode,
        "approval_id": approval_id,
        "action": "cad.update_parameter",
        "target": {
            "document_id": config["document_id"],
            "component_id": config["component_id"],
            "parameter_id": config["parameter_id"],
        },
        "value": value,
    }
    return CAD_ACTION_ADAPTER.validate_python(raw)


@pytest.mark.asyncio
async def test_live_context_preview_parameter_verify_restore_and_step_export():
    config = _configuration()
    adapter = RemoteFusionAdapter(
        base_url=config["runtime_url"],
        backend_secret=config["backend_secret"],
        owner_id=config.get("owner_id", "fusion-e2e-fixture"),
        timeout_s=305,
    )
    status = await adapter.refresh_status()
    assert status.available, "Fusion connector is not online"
    context = await adapter.get_context(ContextRequest.model_validate({
        "query": {"sections": ["application", "document", "design", "parameters", "features", "cloud"]}
    }))
    assert context.status == "success"
    assert context.data.document.document_id == config["document_id"]

    original = config["original_value"]
    test_value = config["test_value"]
    changed = False
    try:
        preview = await adapter.execute(_parameter_action(config, test_value, "preview", suffix="preview"))
        assert preview.status == "approval_required"
        execute = await adapter.execute(_parameter_action(
            config, test_value, "execute", str(preview.approval.approval_id), suffix="execute"
        ))
        changed = True
        assert execute.status == "success"
        assert execute.verification and execute.verification.passed
        assert execute.changes and execute.changes[0].after
        verified = await adapter.verify(VerifyRequest.model_validate({
            "specification": {
                "document_id": config["document_id"],
                "baseline_request_id": str(preview.request_id),
                "checks": [
                    {"check": "parameter_equals", "target_id": config["parameter_id"], "expected": test_value},
                    {"check": "entity_resolves", "target_id": config["parameter_id"]},
                    {"check": "no_new_feature_errors"},
                ],
            }
        }))
        assert verified.status == "success"
        assert verified.verification and verified.verification.passed

        export = CAD_ACTION_ADAPTER.validate_python({
            "request_id": str(uuid.uuid4()), "idempotency_key": f"fusion-e2e-step-{uuid.uuid4()}",
            "action": "cad.export", "target": {"document_id": config["document_id"]},
            "format": "step", "filename": config.get("step_filename", "fusion-e2e.step"),
        })
        exported = await adapter.execute(export)
        assert exported.status == "success"
        assert exported.verification and exported.verification.passed
        assert exported.artifacts and exported.artifacts[0].size_bytes > 0
        artifact_verification = await adapter.verify(VerifyRequest.model_validate({
            "specification": {
                "document_id": config["document_id"],
                "source_request_id": str(exported.request_id),
                "checks": [{
                    "check": "artifact_valid",
                    "artifact_id": str(exported.artifacts[0].artifact_id),
                }],
            }
        }))
        assert artifact_verification.status == "success"
        assert artifact_verification.verification and artifact_verification.verification.passed
    finally:
        if changed:
            restore_preview = await adapter.execute(_parameter_action(config, original, "preview", suffix="restore-preview"))
            restored = await adapter.execute(_parameter_action(
                config, original, "execute", str(restore_preview.approval.approval_id), suffix="restore-execute"
            ))
            assert restored.status == "success" and restored.verification.passed
