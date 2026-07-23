from __future__ import annotations

import copy
import uuid

import pytest
from pydantic import ValidationError

from app.fusion360.agent_contract import (
    AGENT_CONTRACT_VERSION,
    AgentArtifactUploadClaim,
    AgentExecutionReport,
    AgentPlanResponse,
    AgentProtocolCapabilities,
    AgentTurnRequest,
    context_fingerprint,
    negotiate_contract_version,
)
from app.fusion360.contract import ActionData, CadResult, ContextData


def _context() -> ContextData:
    return ContextData.model_validate(
        {
            "document": {
                "document_id": "doc-1",
                "name": "Bracket",
                "document_type": "FusionDesignDocumentType",
                "is_saved": True,
                "is_modified": False,
                "is_read_only": False,
            },
            "design": {"design_type": "parametric", "root_component_id": "component-root", "units": "mm"},
            "components": [{"id": "component-root", "name": "Root", "kind": "component"}],
            "selection": [{"id": "body-1", "name": "Body", "kind": "body", "component_id": "component-root"}],
            "parameters": [
                {
                    "id": "parameter-1",
                    "name": "width",
                    "expression": "2 mm",
                    "value": 0.2,
                    "unit": "cm",
                    "is_user_parameter": True,
                    "component_id": "component-root",
                }
            ],
            "features": [
                {
                    "id": "feature-1",
                    "name": "Extrude1",
                    "kind": "extrude",
                    "component_id": "component-root",
                    "health_state": "healthy",
                }
            ],
            "timeline": [
                {
                    "index": 0,
                    "name": "Extrude1",
                    "kind": "ExtrudeFeature",
                    "entity_id": "feature-1",
                    "health_state": "healthy",
                }
            ],
            "bodies": [{"id": "body-1", "name": "Body", "kind": "body", "component_id": "component-root"}],
            "cloud": {"data_file_id": "file-1", "version_id": "version-3", "version_number": 3},
        }
    )


def _turn(**updates) -> AgentTurnRequest:
    context = updates.pop("context", _context())
    body = {
        "contract_version": AGENT_CONTRACT_VERSION,
        "request_id": str(uuid.uuid4()),
        "connector_instance_id": str(uuid.uuid4()),
        "prompt": "Set width to 3 mm",
        "context": context.model_dump(mode="json"),
        "context_fingerprint": context_fingerprint(context),
        "capabilities": {
            "adapter": "fusion360",
            "available": True,
            "runtime_online": False,
            "connector_online": True,
            "fusion_running": True,
            "actions": ["cad.update_parameter", "cad.export"],
            "context_sections": ["document", "design", "components", "parameters"],
        },
    }
    body.update(updates)
    return AgentTurnRequest.model_validate(body)


def test_contract_negotiation_selects_only_an_exact_supported_version():
    capabilities = AgentProtocolCapabilities()
    assert capabilities.contract_versions == [AGENT_CONTRACT_VERSION]
    assert negotiate_contract_version(["0.9.0", AGENT_CONTRACT_VERSION]) == AGENT_CONTRACT_VERSION
    with pytest.raises(ValueError, match="compatible"):
        negotiate_contract_version(["0.9.0"])


def test_turn_is_strict_bounded_and_recomputes_context_fingerprint():
    turn = _turn()
    assert turn.context.document and turn.context.document.document_id == "doc-1"
    with pytest.raises(ValidationError):
        AgentTurnRequest.model_validate({**turn.model_dump(mode="json"), "unexpected": True})
    with pytest.raises(ValidationError, match="fingerprint"):
        AgentTurnRequest.model_validate({**turn.model_dump(mode="json"), "context_fingerprint": "ctx-c14n-1:" + "0" * 64})
    with pytest.raises(ValidationError):
        AgentTurnRequest.model_validate({**turn.model_dump(mode="json"), "prompt": "x" * 20_000})


def test_context_fingerprint_is_deterministic_and_tracks_timeline_and_parameters():
    context = _context()
    reordered = ContextData.model_validate(
        {**context.model_dump(mode="json"), "selection": list(reversed(context.model_dump(mode="json")["selection"]))}
    )
    assert context_fingerprint(context) == context_fingerprint(reordered)
    changed = copy.deepcopy(context.model_dump(mode="json"))
    changed["parameters"][0]["expression"] = "3 mm"
    assert context_fingerprint(context) != context_fingerprint(ContextData.model_validate(changed))
    changed = copy.deepcopy(context.model_dump(mode="json"))
    changed["timeline"][0]["health_state"] = "error"
    assert context_fingerprint(context) != context_fingerprint(ContextData.model_validate(changed))


def test_upload_consent_is_explicit_and_f3d_authorization_is_separate():
    with pytest.raises(ValidationError, match="F3D"):
        _turn(f3d_upload_authorized=True)
    assert _turn(export_artifact_upload_consent=True).f3d_upload_authorized is False
    assert _turn(export_artifact_upload_consent=True, f3d_upload_authorized=True).f3d_upload_authorized is True


def test_plan_allows_exactly_one_typed_action_and_server_binding_fields():
    turn = _turn()
    response = AgentPlanResponse(
        request_id=turn.request_id,
        proposal_id=uuid.uuid4(),
        connector_instance_id=turn.connector_instance_id,
        status="proposed",
        context_fingerprint=turn.context_fingerprint,
        action={
            "action": "cad.update_parameter",
            "request_id": str(turn.request_id),
            "connector_instance_id": str(turn.connector_instance_id),
            "execution_mode": "preview",
            "target": {"document_id": "doc-1", "component_id": "component-root", "parameter_id": "parameter-1"},
            "value": {"amount": 3, "unit": "mm"},
        },
        risk="medium",
        expires_at="2030-01-01T00:00:00Z",
    )
    assert response.action and response.action.action == "cad.update_parameter"
    with pytest.raises(ValidationError):
        AgentPlanResponse.model_validate({**response.model_dump(mode="json"), "actions": []})
    with pytest.raises(ValidationError):
        AgentPlanResponse.model_validate({**response.model_dump(mode="json"), "action": {"action": "cad.run_python", "source": "pass"}})
    clarification = AgentPlanResponse(
        request_id=turn.request_id,
        proposal_id=uuid.uuid4(),
        connector_instance_id=turn.connector_instance_id,
        status="needs_clarification",
        context_fingerprint=turn.context_fingerprint,
        question="Which parameter?",
    )
    assert clarification.action is None


def test_execution_report_and_upload_claim_are_strict_and_bounded():
    turn = _turn()
    result = CadResult[ActionData](
        request_id=turn.request_id,
        status="success",
        action="cad.update_parameter",
        data={"kind": "mutation", "snapshot_id": "snapshot-1"},
        changes=[{
            "target_id": "parameter-1", "path": "parameter.expression",
            "kind": "updated", "before": "2 mm", "after": "3 mm",
        }],
        verification={
            "passed": True,
            "checks": [{"check": "parameter_equals", "passed": True}],
            "compute_completed": True,
            "new_feature_errors": [],
        },
    )
    report = AgentExecutionReport(
        report_id=uuid.uuid4(),
        request_id=turn.request_id,
        proposal_id=uuid.uuid4(),
        connector_instance_id=turn.connector_instance_id,
        context_fingerprint=turn.context_fingerprint,
        action_intent_hash="a" * 64,
        result=result,
        completed_at="2030-01-01T00:00:00Z",
    )
    assert report.result.status == "success"
    with pytest.raises(ValidationError):
        AgentExecutionReport.model_validate({**report.model_dump(mode="json"), "result": {**result.model_dump(mode="json"), "debug": "secret"}})

    for invalid_result in (
        {**result.model_dump(mode="json"), "verification": None},
        {**result.model_dump(mode="json"), "changes": []},
        {**result.model_dump(mode="json"), "status": "running"},
    ):
        with pytest.raises(ValidationError):
            AgentExecutionReport.model_validate({
                **report.model_dump(mode="json"), "result": invalid_result,
            })

    claim = AgentArtifactUploadClaim(
        request_id=turn.request_id,
        filename="bracket.step",
        size_bytes=10,
        sha256="b" * 64,
    )
    assert claim.filename == "bracket.step"
    with pytest.raises(ValidationError):
        AgentArtifactUploadClaim(request_id=turn.request_id, filename="../bracket.step", size_bytes=10, sha256="b" * 64)
