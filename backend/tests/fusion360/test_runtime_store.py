import time
import uuid

import pytest

from app.fusion360.errors import FusionConnectorError
from app.fusion360.runtime_store import RuntimeStore


def _capabilities():
    return {
        "adapter": "fusion360", "contract_version": "1.0.0", "protocol_versions": [1],
        "available": True, "runtime_online": True, "connector_online": True,
        "actions": [], "context_sections": [], "limitations": [],
    }


def _register(store, connector_id, now=None):
    return store.register_connector({
        "connector_instance_id": connector_id,
        "protocol_versions": [1],
        "fusion_version": "2.0.1",
        "addin_version": "1.0.0",
        "platform": "macos",
        "artifact_root_fingerprint": "fingerprint",
        "capabilities": _capabilities(),
    }, now=now)


def _submit(store, connector_id, **overrides):
    values = dict(
        request_id=str(uuid.uuid4()), owner_id="owner", operation="execute", phase="execute",
        action_name="cad.update_parameter", intent_hash="intent", idempotency_key="key",
        payload_hash="hash", payload={"action": "cad.update_parameter"},
        connector_instance_id=connector_id, deadline=time.time() + 60,
    )
    values.update(overrides)
    return store.submit_task(**values)


def test_connector_protocol_heartbeat_and_offline(tmp_path):
    store = RuntimeStore(tmp_path / "runtime.db")
    connector = str(uuid.uuid4())
    _register(store, connector, now=100)
    assert store.connectors(now=110)[0]["online"] is True
    assert store.connectors(now=116)[0]["online"] is False
    with pytest.raises(FusionConnectorError) as raised:
        store.register_connector({
            "connector_instance_id": str(uuid.uuid4()), "protocol_versions": [99],
            "fusion_version": "x", "addin_version": "x", "platform": "macos",
            "artifact_root_fingerprint": "f", "capabilities": _capabilities(),
        })
    assert raised.value.code == "PROTOCOL_MISMATCH"


def test_phase_aware_idempotency_and_conflict(tmp_path):
    store = RuntimeStore(tmp_path / "runtime.db")
    connector = str(uuid.uuid4())
    _register(store, connector)
    first, replay = _submit(store, connector)
    assert replay is False
    second, replay = _submit(store, connector, request_id=str(uuid.uuid4()))
    assert replay is True and second["request_id"] == first["request_id"]
    with pytest.raises(FusionConnectorError) as raised:
        _submit(store, connector, request_id=str(uuid.uuid4()), payload_hash="different")
    assert raised.value.code == "IDEMPOTENCY_CONFLICT"
    preview, replay = _submit(
        store, connector, request_id=str(uuid.uuid4()), phase="preview", payload_hash="preview-hash"
    )
    assert replay is False and preview["is_mutation"] is False


def test_lease_fencing_duplicate_result_and_reopen(tmp_path):
    path = tmp_path / "runtime.db"
    store = RuntimeStore(path)
    connector = str(uuid.uuid4())
    _register(store, connector)
    task, _ = _submit(store, connector)
    leased = store.lease_next(connector)
    with pytest.raises(FusionConnectorError):
        store.mark_started(connector, task["request_id"], str(uuid.uuid4()), 1, "intent")
    store.mark_started(connector, task["request_id"], leased["lease_id"], leased["attempt"], "intent")
    completed = store.complete_task(
        connector, task["request_id"], leased["lease_id"], leased["attempt"], "intent",
        {"request_id": task["request_id"], "status": "success", "action": "cad.update_parameter"},
    )
    duplicate = store.complete_task(
        connector, task["request_id"], leased["lease_id"], leased["attempt"], "intent",
        {"request_id": task["request_id"], "status": "success", "action": "cad.update_parameter"},
    )
    assert completed["status"] == duplicate["status"] == "success"
    store.close()
    reopened = RuntimeStore(path)
    assert reopened.get_task("owner", task["request_id"])["status"] == "success"


def test_started_mutation_deadline_is_indeterminate_and_not_released(tmp_path):
    store = RuntimeStore(tmp_path / "runtime.db")
    connector = str(uuid.uuid4())
    _register(store, connector)
    task, _ = _submit(store, connector, deadline=200, now=100)
    leased = store.lease_next(connector, now=110)
    store.mark_started(connector, task["request_id"], leased["lease_id"], 1, "intent", now=111)
    store.reap(now=201)
    assert store.get_task("owner", task["request_id"])["status"] == "indeterminate"
    assert store.lease_next(connector, now=202) is None


def test_late_result_is_rejected_even_before_next_heartbeat(tmp_path):
    store = RuntimeStore(tmp_path / "runtime.db")
    connector = str(uuid.uuid4())
    _register(store, connector, now=100)
    task, _ = _submit(store, connector, deadline=105, now=100)
    leased = store.lease_next(connector, now=101)
    store.mark_started(connector, task["request_id"], leased["lease_id"], 1, "intent", now=102)
    with pytest.raises(FusionConnectorError) as late:
        store.complete_task(
            connector, task["request_id"], leased["lease_id"], 1, "intent",
            {"request_id": task["request_id"], "status": "success", "action": "cad.update_parameter"},
            now=106,
        )
    assert late.value.code == "STALE_LEASE"
    assert store.get_task("owner", task["request_id"])["status"] == "indeterminate"


def test_owner_isolation_cancel_and_one_time_approval(tmp_path):
    store = RuntimeStore(tmp_path / "runtime.db")
    connector = str(uuid.uuid4())
    _register(store, connector)
    task, _ = _submit(store, connector)
    with pytest.raises(FusionConnectorError) as raised:
        store.get_task("other", task["request_id"])
    assert raised.value.code == "OWNER_MISMATCH"
    assert store.request_cancel("owner", task["request_id"])["status"] == "cancelled"
    approval = store.create_approval(
        owner_id="owner", connector_instance_id=connector, document_id="doc",
        intent_hash="intent", risk="medium", now=100,
    )
    store.consume_approval(
        approval["approval_id"], owner_id="owner", connector_instance_id=connector,
        document_id="doc", intent_hash="intent", request_id="r", now=101,
    )
    with pytest.raises(FusionConnectorError) as used:
        store.consume_approval(
            approval["approval_id"], owner_id="owner", connector_instance_id=connector,
            document_id="doc", intent_hash="intent", request_id="r2", now=102,
        )
    assert used.value.code == "APPROVAL_ALREADY_USED"


def test_queue_full_rolls_back_approval_consumption(tmp_path):
    store = RuntimeStore(tmp_path / "runtime.db", max_queue=1)
    connector = str(uuid.uuid4())
    _register(store, connector)
    occupied, _ = _submit(store, connector, idempotency_key="occupied")
    request_id = str(uuid.uuid4())
    approval = store.create_approval(
        owner_id="owner", connector_instance_id=connector, document_id="doc",
        intent_hash="approved-intent", risk="medium",
    )
    approval_binding = {
        "approval_id": approval["approval_id"], "owner_id": "owner",
        "connector_instance_id": connector, "document_id": "doc",
        "intent_hash": "approved-intent", "request_id": request_id,
    }

    with pytest.raises(FusionConnectorError) as full:
        _submit(
            store, connector, request_id=request_id, idempotency_key="approved",
            intent_hash="approved-intent", approval=approval_binding,
        )
    assert full.value.code == "QUEUE_FULL"

    store.request_cancel("owner", occupied["request_id"])
    task, replay = _submit(
        store, connector, request_id=request_id, idempotency_key="approved",
        intent_hash="approved-intent", approval=approval_binding,
    )
    assert replay is False
    assert task["request_id"] == request_id
