import json
import queue
import sys
import threading
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from fusion_addin.CADAgentFusionConnector.config import ConnectorConfig
from fusion_addin.CADAgentFusionConnector.palette import (
    ApprovalBinding,
    ApprovalLedger,
    ControllerError,
    PaletteController,
    action_intent_hash,
    cloud_context,
    context_fingerprint,
    parse_palette_event,
    _validate_agent_action,
)


ASSET_ROOT = Path(__file__).resolve().parents[3] / "fusion_addin" / "CADAgentFusionConnector"


def test_palette_assets_are_local_csp_fenced_and_render_text_only():
    html = (ASSET_ROOT / "palette.html").read_text(encoding="utf-8")
    js = (ASSET_ROOT / "palette.js").read_text(encoding="utf-8")
    assert "default-src 'none'" in html
    assert "connect-src 'none'" in html
    assert html.count("<script") == 1
    assert 'src="palette.js"' in html
    assert "http://" not in html and "https://" not in html
    assert "innerHTML" not in js
    assert "textContent" in js
    assert "adsk.fusionSendData" in js
    assert "await adsk.fusionSendData" in js


@pytest.mark.parametrize(
    ("event", "data"),
    [
        ("unknown", {}),
        ("request_plan", {"prompt": "x", "action": {"action": "cad.run_python"}}),
        ("request_plan", {"prompt": "x" * 4001}),
        ("approve", {"approval_nonce": "n", "action": {}}),
        ("approve", {"approval_nonce": ""}),
        ("cancel", {"request_id": "agent-owned"}),
    ],
)
def test_palette_input_event_schema_and_size_allowlist(event, data):
    with pytest.raises(ControllerError):
        parse_palette_event(event, json.dumps(data))


def test_palette_input_accepts_only_minimal_user_intent_and_opaque_nonce():
    assert parse_palette_event("request_plan", '{"prompt":"make it 3 mm"}') == {
        "prompt": "make it 3 mm",
        "export_artifact_upload_consent": False,
        "f3d_upload_authorized": False,
    }
    assert parse_palette_event("approve", '{"approval_nonce":"opaque-value"}') == {
        "approval_nonce": "opaque-value"
    }
    assert parse_palette_event("cancel", "{}") == {}


def _binding(now=100.0):
    return ApprovalBinding(
        credential_subject="tenant:user",
        connector_instance_id=str(uuid.uuid4()),
        document_id="doc-1",
        intent_scheme="CAD-C14N-1",
        intent_hash="intent-1",
        context_fingerprint="context-1",
        proposal_id=str(uuid.uuid4()),
        preview_id=str(uuid.uuid4()),
        request_id=str(uuid.uuid4()),
        risk="medium",
        purpose="execute",
        expires_at=now + 10,
    )


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        pytest.param(field, changed, id=field)
        for field, changed in [
            ("credential_subject", "tenant:other"),
            ("connector_instance_id", str(uuid.uuid4())),
            ("document_id", "doc-2"),
            ("intent_scheme", "some-other-canonicalization"),
            ("intent_hash", "changed-action"),
            ("context_fingerprint", "changed-context"),
            ("proposal_id", str(uuid.uuid4())),
            ("preview_id", str(uuid.uuid4())),
            ("request_id", str(uuid.uuid4())),
            ("risk", "high"),
            ("purpose", "f3d_upload"),
            ("expires_at", 999.0),
        ]
    ],
)
def test_approval_is_bound_to_every_security_dimension_and_mismatch_invalidates(field, changed):
    ledger = ApprovalLedger(clock=lambda: 100.0)
    binding = _binding()
    nonce = ledger.issue(binding)
    with pytest.raises(ControllerError):
        ledger.consume(nonce, replace(binding, **{field: changed}))
    with pytest.raises(ControllerError, match="invalid|used"):
        ledger.consume(nonce, binding)


def test_approval_is_opaque_expiring_and_atomically_single_use():
    current = [100.0]
    ledger = ApprovalLedger(clock=lambda: current[0])
    binding = _binding()
    nonce = ledger.issue(binding)
    assert binding.intent_hash not in nonce
    assert ledger.consume(nonce, binding) == binding
    with pytest.raises(ControllerError, match="invalid|used"):
        ledger.consume(nonce, binding)

    expired = ledger.issue(binding)
    current[0] = binding.expires_at + 1
    with pytest.raises(ControllerError, match="expired"):
        ledger.consume(expired, binding)

    current[0] = 100
    nonce = ledger.issue(binding)
    outcomes = []

    def consume():
        try:
            ledger.consume(nonce, binding)
            outcomes.append("ok")
        except ControllerError:
            outcomes.append("rejected")

    threads = [threading.Thread(target=consume), threading.Thread(target=consume)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["ok", "rejected"]


def test_f3d_upload_requires_a_separate_purpose_bound_consent():
    ledger = ApprovalLedger(clock=lambda: 100.0)
    execute = _binding()
    upload = replace(execute, purpose="f3d_upload", request_id=str(uuid.uuid4()))
    execute_nonce = ledger.issue(execute)
    upload_nonce = ledger.issue(upload)
    with pytest.raises(ControllerError):
        ledger.consume(execute_nonce, upload)
    assert ledger.consume(upload_nonce, upload).purpose == "f3d_upload"


def _context(parameter_expression="2 mm", document_id="doc-1"):
    return {
        "application": {"name": "Autodesk Fusion", "version": "2.0", "language": None, "user_name": "U"},
        "document": {
            "document_id": document_id,
            "name": "Part",
            "document_type": "FusionDesignDocumentType",
            "is_saved": True,
            "is_modified": False,
            "is_read_only": False,
        },
        "design": {"design_type": "parametric", "root_component_id": "component-1", "units": "mm"},
        "components": [{"id": "component-1", "name": "Root", "kind": "component"}],
        "occurrences": [],
        "selection": [{"id": "parameter-1", "name": "Width", "kind": "parameter"}],
        "parameters": [{
            "id": "parameter-1", "name": "Width", "expression": parameter_expression,
            "value": 2.0, "unit": "mm", "is_user_parameter": True,
            "component_id": "component-1", "created_by_id": None,
        }],
        "materials": [], "mass_properties": [], "sketches": [],
        "features": [{"id": "feature-1", "name": "Extrude", "kind": "feature", "health_state": "healthy"}],
        "timeline": [{
            "index": 0, "name": "Extrude", "kind": "ExtrudeFeature", "entity_id": "feature-1",
            "health_state": "healthy", "is_group": False, "is_rolled_back": False,
            "is_suppressed": False, "parent": None, "error_or_warning": None,
        }],
        "bodies": [], "assembly": [],
        "cloud": {"version_id": "v1", "version_number": 1, "is_complete": True},
        "truncated": False, "vendor_extensions": {},
    }


def test_context_fingerprint_tracks_document_selection_feature_timeline_and_parameter():
    baseline = _context()
    initial = context_fingerprint(baseline)
    for changed in (
        _context(document_id="doc-2"),
        {**baseline, "selection": []},
        {**baseline, "features": [{**baseline["features"][0], "health_state": "error"}]},
        {**baseline, "timeline": [{**baseline["timeline"][0], "is_suppressed": True}]},
        _context(parameter_expression="3 mm"),
    ):
        assert context_fingerprint(changed) != initial


def test_cloud_context_removes_local_user_identity_without_changing_context_fingerprint():
    local = _context()
    outbound = cloud_context(local)

    assert local["application"]["user_name"] == "U"
    assert outbound["application"]["user_name"] is None
    assert context_fingerprint(outbound) == context_fingerprint(local)


def test_dependency_free_fingerprint_and_intent_match_backend_contract():
    from app.fusion360.agent_contract import context_fingerprint as backend_fingerprint
    from app.fusion360.contract import CAD_ACTION_ADAPTER, ContextData
    from app.fusion360.policy import action_intent_hash as backend_intent

    context = _context()
    assert context_fingerprint(context) == backend_fingerprint(ContextData.model_validate(context))
    with_omitted_defaults = {
        **context,
        "selection": [{"id": "body-1", "name": "Body", "kind": "body"}],
        "features": [{"id": "feature-1", "name": "Extrude", "kind": "feature"}],
        "timeline": [{"index": 0, "name": "Extrude", "kind": "ExtrudeFeature"}],
        "cloud": None,
    }
    assert context_fingerprint(with_omitted_defaults) == backend_fingerprint(
        ContextData.model_validate(with_omitted_defaults)
    )
    action = CAD_ACTION_ADAPTER.validate_python({
        **_action(),
        "request_id": str(uuid.uuid4()),
        "connector_instance_id": str(uuid.uuid4()),
        "execution_mode": "preview",
    })
    dumped = action.model_dump(mode="json")
    assert action_intent_hash(dumped) == backend_intent(action)


def test_addin_revalidates_profile_kind_and_component_ownership():
    from app.fusion360.contract import CAD_ACTION_ADAPTER

    context = _context()
    context["selection"] = [{
        "id": "profile-1", "name": "Profile", "kind": "profile",
        "component_id": "component-1",
    }]
    raw = CAD_ACTION_ADAPTER.validate_python({
        "request_id": str(uuid.uuid4()),
        "idempotency_key": None,
        "timeout_ms": 30000,
        "execution_mode": "preview",
        "approval_id": None,
        "connector_instance_id": str(uuid.uuid4()),
        "action": "cad.create_extrude",
        "target": {"document_id": "doc-1", "component_id": "component-1"},
        "profile_id": "profile-1",
        "operation": "new_body",
        "distance": {"amount": 3, "unit": "mm"},
        "direction": "positive",
        "participant_body_ids": [],
    }).model_dump(mode="json")

    assert _validate_agent_action(
        raw,
        turn_request_id=raw["request_id"],
        connector_instance_id=raw["connector_instance_id"],
        document_id="doc-1",
        context=context,
    )["profile_id"] == "profile-1"

    context["selection"][0]["kind"] = "edge"
    with pytest.raises(ControllerError, match="semantic kind"):
        _validate_agent_action(
            raw,
            turn_request_id=raw["request_id"],
            connector_instance_id=raw["connector_instance_id"],
            document_id="doc-1",
            context=context,
        )

    context["selection"][0].update({"kind": "profile", "component_id": "other-component"})
    with pytest.raises(ControllerError, match="target component"):
        _validate_agent_action(
            raw,
            turn_request_id=raw["request_id"],
            connector_instance_id=raw["connector_instance_id"],
            document_id="doc-1",
            context=context,
        )


class FakeDispatcher:
    def __init__(self):
        self.context = _context()
        self.calls = []

    def dispatch(self, task, cancel):
        self.calls.append((task["operation"], task))
        if task["operation"] == "context":
            return {
                "request_id": task["request_id"], "status": "success", "action": "cad.get_context",
                "data": json.loads(json.dumps(self.context)), "changes": [], "warnings": [],
                "verification": None, "artifacts": [], "approval": None, "error": None,
            }
        action = task["payload"]
        if action["execution_mode"] == "preview":
            return {
                "request_id": action["request_id"], "status": "success", "action": action["action"],
                "data": {"kind": "preview", "planned_changes": [{
                    "target_id": "parameter-1", "path": "expression", "kind": "updated",
                    "before": "2 mm", "after": "3 mm",
                }], "verification_plan": ["parameter_equals"]},
                "changes": [], "warnings": [], "verification": None, "artifacts": [],
                "approval": None, "error": None,
            }
        return {
            "request_id": action["request_id"], "status": "success", "action": action["action"],
            "data": {"kind": "mutation", "snapshot_id": "snapshot-1", "created_or_updated_entity_ids": ["parameter-1"]},
            "changes": [{"target_id": "parameter-1", "path": "expression", "kind": "updated", "before": "2 mm", "after": "3 mm"}],
            "warnings": [],
            "verification": {
                "passed": True, "checks": [{"check": "entity_resolves", "passed": True, "_private": "nested-secret"}],
                "compute_completed": True, "new_feature_errors": [],
            },
            "artifacts": [], "approval": None, "error": None,
            "_snapshot": {"expression": "private-before-expression", "feature_errors": ["private-baseline"]},
            "_local_artifacts": [{
                "artifact_id": str(uuid.uuid4()), "kind": "f3d", "filename": "private-local-path.f3d",
                "media_type": "application/vnd.autodesk.fusion360", "size_bytes": 123,
                "sha256": "b" * 64, "relative_path": "private-local-path.f3d",
            }],
        }


class FakeTransport:
    def __init__(self):
        self.messages = []

    def submit(self, message):
        self.messages.append(json.loads(json.dumps(message)))
        return True


def _config(tmp_path):
    return ConnectorConfig(
        connector_instance_id=str(uuid.uuid4()),
        artifact_root=tmp_path / "artifacts",
        journal_path=tmp_path / "journal.json",
        agent_url="https://agent.test",
    )


def _action(name="cad.update_parameter"):
    if name == "cad.save_document":
        return {"action": name, "target": {"document_id": "doc-1"}, "version_description": "save"}
    return {
        "action": name,
        "target": {"document_id": "doc-1", "component_id": "component-1", "parameter_id": "parameter-1"},
        "value": {"amount": 3, "unit": "mm"},
    }


def _proposed(turn, action=None, risk="medium"):
    from app.fusion360.contract import CAD_ACTION_ADAPTER

    normalized_action = CAD_ACTION_ADAPTER.validate_python({
        **(action or _action()),
        "request_id": turn["request_id"],
        "idempotency_key": None,
        "timeout_ms": 30000,
        "execution_mode": "preview",
        "approval_id": None,
        "connector_instance_id": turn["connector_instance_id"],
    }).model_dump(mode="json")
    return {
        "contract_version": "1.0.0",
        "request_id": turn["request_id"],
        "connector_instance_id": turn["connector_instance_id"],
        "status": "proposed",
        "context_fingerprint": turn["context_fingerprint"],
        "proposal_id": str(uuid.uuid4()),
        "risk": risk,
        "action": normalized_action,
        "question": None,
        "reason": "Proposed change",
        "expires_at": "1970-01-01T00:03:20Z",
    }


def _plan_message(turn, payload):
    return {
        "kind": "plan_result", "request_id": turn["request_id"], "payload": payload,
        "credential_subject": "bearer-sha256:" + "a" * 64,
    }


def _controller(tmp_path, *, confirm=lambda _message: True):
    dispatcher = FakeDispatcher()
    transport = FakeTransport()
    messages = []
    controller = PaletteController(
        _config(tmp_path), dispatcher, transport,
        lambda event, data: messages.append((event, json.loads(data))),
        confirm_high_risk=confirm,
        clock=lambda: 100.0,
    )
    return controller, dispatcher, transport, messages


def test_controller_captures_bounded_context_then_native_previews_before_approval(tmp_path):
    from app.fusion360.agent_contract import AgentTurnRequest

    controller, dispatcher, transport, messages = _controller(tmp_path)
    controller.handle_palette_event("request_plan", json.dumps({"prompt": "Set width to 3 mm"}))
    turn = transport.messages[-1]["payload"]
    assert transport.messages[-1]["kind"] == "plan"
    context_task = dispatcher.calls[0][1]
    assert "timeline" in context_task["payload"]["query"]["sections"]
    expected_cloud_context = _context()
    expected_cloud_context["application"]["user_name"] = None
    assert turn["context"] == expected_cloud_context
    assert turn["context_fingerprint"] == context_fingerprint(_context())
    assert AgentTurnRequest.model_validate(turn).context_fingerprint == turn["context_fingerprint"]

    controller.handle_worker_message(_plan_message(turn, _proposed(turn)))
    assert [name for name, _ in dispatcher.calls] == ["context", "context", "execute"]
    preview_task = dispatcher.calls[-1][1]
    assert preview_task["payload"]["execution_mode"] == "preview"
    state = messages[-1][1]
    assert state["state"] == "awaiting_approval"
    assert set(state["proposal"]) >= {"action", "risk", "proposal_id"}
    assert state["preview"]["data"]["kind"] == "preview"
    assert state["approval_nonce"]


def test_controller_approval_executes_once_reports_and_rejects_replay(tmp_path):
    from app.fusion360.agent_contract import AgentExecutionReport

    controller, dispatcher, transport, messages = _controller(tmp_path)
    controller.handle_palette_event("request_plan", '{"prompt":"Set width to 3 mm"}')
    turn = transport.messages[-1]["payload"]
    controller.handle_worker_message(_plan_message(turn, _proposed(turn)))
    nonce = messages[-1][1]["approval_nonce"]
    controller.handle_palette_event("approve", json.dumps({"approval_nonce": nonce}))
    execute_calls = [task for name, task in dispatcher.calls if name == "execute" and task["payload"]["execution_mode"] == "execute"]
    assert len(execute_calls) == 1
    assert transport.messages[-1]["kind"] == "report"
    assert transport.messages[-1]["payload"]["result"]["status"] == "success"
    assert AgentExecutionReport.model_validate(transport.messages[-1]["payload"]).result.status == "success"
    serialized = json.dumps({"transport": transport.messages, "palette": messages})
    assert "private-before-expression" not in serialized
    assert "private-baseline" not in serialized
    assert "private-local-path.f3d" not in serialized
    assert "nested-secret" not in serialized
    with pytest.raises(ControllerError):
        controller.handle_palette_event("approve", json.dumps({"approval_nonce": nonce}))
    assert len([task for name, task in dispatcher.calls if name == "execute" and task["payload"]["execution_mode"] == "execute"]) == 1


def test_post_execution_report_and_artifact_failures_are_truthful_without_replay(tmp_path):
    controller, dispatcher, transport, messages = _controller(tmp_path)
    controller.handle_palette_event("request_plan", '{"prompt":"Set width to 3 mm"}')
    turn = transport.messages[-1]["payload"]
    controller.handle_worker_message(_plan_message(turn, _proposed(turn)))
    nonce = messages[-1][1]["approval_nonce"]
    controller.handle_palette_event("approve", json.dumps({"approval_nonce": nonce}))

    controller.handle_worker_message({
        "kind": "transport_error",
        "operation": "report",
        "request_id": turn["request_id"],
        "error": {"code": "NETWORK_UNAVAILABLE", "message": "offline"},
    })
    assert messages[-1][1]["state"] == "indeterminate"
    assert messages[-1][1]["result"]["error"]["code"] == "AGENT_REPORT_UNCONFIRMED"
    assert len([
        task for name, task in dispatcher.calls
        if name == "execute" and task["payload"]["execution_mode"] == "execute"
    ]) == 1

    controller.handle_worker_message({
        "kind": "artifact_result",
        "request_id": turn["request_id"],
        "payload": {
            "request_id": turn["request_id"], "filename": "part.step",
            "status": "accepted", "download_url": "/api/cad/fusion360/agent/artifacts/x/part.step",
        },
    })
    assert messages[-1][1]["uploaded_artifacts"][0]["filename"] == "part.step"


def test_orphaned_artifact_receipt_fails_closed(tmp_path):
    controller, _dispatcher, _transport, messages = _controller(tmp_path)
    controller.handle_worker_message({
        "kind": "artifact_result",
        "request_id": "orphan",
        "payload": {"filename": "unverified.step", "status": "accepted"},
    })
    assert messages[-1][1]["state"] == "failed"
    assert messages[-1][1]["error"]["code"] == "ARTIFACT_RESULT_ORPHANED"


def test_controller_stale_context_fails_closed_before_execute(tmp_path):
    controller, dispatcher, transport, messages = _controller(tmp_path)
    controller.handle_palette_event("request_plan", '{"prompt":"Set width"}')
    turn = transport.messages[-1]["payload"]
    controller.handle_worker_message(_plan_message(turn, _proposed(turn)))
    nonce = messages[-1][1]["approval_nonce"]
    dispatcher.context = _context(parameter_expression="changed")
    with pytest.raises(ControllerError, match="context"):
        controller.handle_palette_event("approve", json.dumps({"approval_nonce": nonce}))
    assert not [task for name, task in dispatcher.calls if name == "execute" and task["payload"]["execution_mode"] == "execute"]


def test_high_risk_requires_fusion_native_confirmation(tmp_path):
    confirmations = []
    controller, dispatcher, transport, messages = _controller(
        tmp_path, confirm=lambda message: confirmations.append(message) or False
    )
    controller.handle_palette_event("request_plan", '{"prompt":"Save"}')
    turn = transport.messages[-1]["payload"]
    controller.handle_worker_message(_plan_message(
        turn, _proposed(turn, _action("cad.save_document"), "high")
    ))
    nonce = messages[-1][1]["approval_nonce"]
    with pytest.raises(ControllerError, match="native confirmation"):
        controller.handle_palette_event("approve", json.dumps({"approval_nonce": nonce}))
    assert confirmations
    assert not [task for name, task in dispatcher.calls if name == "execute" and task["payload"]["execution_mode"] == "execute"]


def test_explicit_f3d_upload_requires_turn_consent_and_separate_nonce(tmp_path):
    controller, _dispatcher, transport, messages = _controller(tmp_path)
    controller.handle_palette_event("request_plan", json.dumps({
        "prompt": "Export the design archive",
        "export_artifact_upload_consent": True,
        "f3d_upload_authorized": True,
    }))
    turn = transport.messages[-1]["payload"]
    assert turn["export_artifact_upload_consent"] is True
    assert turn["f3d_upload_authorized"] is True
    export = {
        "action": "cad.export", "target": {"document_id": "doc-1"},
        "format": "f3d", "filename": "design.f3d",
    }
    controller.handle_worker_message(_plan_message(turn, _proposed(turn, export, "low")))
    state = messages[-1][1]
    action_nonce = state["approval_nonce"]
    f3d_nonce = state["f3d_consent_nonce"]
    controller.handle_palette_event("approve_f3d_upload", json.dumps({"approval_nonce": f3d_nonce}))
    assert messages[-1][1]["approval_nonce"] == action_nonce
    controller.handle_palette_event("approve", json.dumps({"approval_nonce": action_nonce}))
    assert [message["kind"] for message in transport.messages[-2:]] == ["report", "artifact"]
    upload = transport.messages[-1]["payload"]
    assert upload["consent"]["f3d_upload_authorized"] is True
    assert upload["consent"]["upload_authorized"] is True
    assert "private-local-path.f3d" not in json.dumps(messages)


def test_controller_rejects_unknown_action_and_emits_structured_ui_error(tmp_path):
    controller, dispatcher, transport, messages = _controller(tmp_path)
    controller.handle_palette_event("request_plan", '{"prompt":"do something"}')
    turn = transport.messages[-1]["payload"]
    bad = _proposed(turn)
    bad["action"] = {"action": "cad.run_python", "source": "pass"}
    controller.handle_worker_message(_plan_message(turn, bad))
    assert messages[-1][0] == "controller_state"
    assert messages[-1][1]["state"] == "failed"
    assert messages[-1][1]["error"]["code"] == "INVALID_AGENT_PLAN"
    assert [name for name, _ in dispatcher.calls] == ["context"]


def test_controller_requires_main_thread_and_stop_rejects_late_messages(tmp_path):
    controller, _dispatcher, transport, messages = _controller(tmp_path)
    failures = []

    def off_thread():
        try:
            controller.handle_palette_event("request_plan", '{"prompt":"x"}')
        except ControllerError as exc:
            failures.append(exc.code)

    thread = threading.Thread(target=off_thread)
    thread.start()
    thread.join()
    assert failures == ["THREAD_VIOLATION"]
    controller.stop()
    with pytest.raises(ControllerError, match="stopped"):
        controller.handle_palette_event("request_plan", '{"prompt":"x"}')
    before = len(messages)
    controller.handle_worker_message({"kind": "transport_error", "request_id": "late", "error": {}})
    assert len(messages) == before
