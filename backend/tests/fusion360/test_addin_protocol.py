import json
import os
import queue
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from fusion_addin.CADAgentFusionConnector.config import ConnectorConfig
from fusion_addin.CADAgentFusionConnector.dispatcher import CancelToken, Dispatcher, failure_result
from fusion_addin.CADAgentFusionConnector.protocol import (
    ExecutionJournal,
    JournalSafetyError,
    ProtocolError,
    validate_task,
)
from fusion_addin.CADAgentFusionConnector.transport import ConnectorWorker


def _task(**overrides):
    value = {
        "protocol_version": 1,
        "request_id": str(uuid.uuid4()),
        "operation": "execute",
        "intent_hash": "intent",
        "lease_id": str(uuid.uuid4()),
        "attempt": 1,
        "leased_until": 100,
        "deadline": 200,
        "payload": {
            "action": "cad.update_parameter", "execution_mode": "execute",
            "target": {"document_id": "d", "component_id": "c", "parameter_id": "p"},
            "value": {"amount": 3, "unit": "mm"},
        },
        "execution_context": {"artifact_dir": "/tmp/a", "artifact_root_fingerprint": "f"},
    }
    value.update(overrides)
    return value


class FakeFacade:
    def __init__(self):
        self.calls = []

    def snapshot(self, payload):
        self.calls.append("snapshot")
        return {"expression": "2 mm"}

    def execute(self, payload, context, cancel):
        self.calls.append("execute")
        cancel.check()
        return {
            "request_id": payload.get("request_id", "00000000-0000-0000-0000-000000000000"),
            "status": "success", "action": payload["action"],
            "data": {"kind": "mutation", "snapshot_id": "s", "created_or_updated_entity_ids": ["p"]},
            "changes": [], "warnings": [],
            "verification": {"passed": True, "checks": [], "compute_completed": True, "new_feature_errors": []},
            "artifacts": [], "approval": None, "error": None,
        }

    def compensate(self, payload, snapshot):
        self.calls.append("compensate")

    def preview(self, payload, cancel):
        self.calls.append("preview")
        return {"request_id": payload.get("request_id"), "status": "success", "action": payload["action"]}


def test_transport_validator_is_strict_and_allowlisted():
    assert validate_task(_task())["protocol_version"] == 1
    with pytest.raises(ProtocolError):
        validate_task(_task(protocol_version=2))
    with pytest.raises(ProtocolError):
        validate_task(_task(payload={"action": "cad.run_python"}))
    with pytest.raises(ProtocolError):
        validate_task({**_task(), "source": "pass"})


def test_preview_has_no_snapshot_or_write(tmp_path):
    facade = FakeFacade()
    task = _task()
    task["payload"]["execution_mode"] = "preview"
    dispatcher = Dispatcher(facade, ExecutionJournal(tmp_path / "journal.json"))
    dispatcher.dispatch(task, CancelToken())
    assert facade.calls == ["preview"]
    assert not (tmp_path / "journal.json").exists()


def test_mutation_journals_before_execute_and_completed_result_replays(tmp_path):
    facade = FakeFacade()
    task = _task()
    dispatcher = Dispatcher(facade, ExecutionJournal(tmp_path / "journal.json"))
    first = dispatcher.dispatch(task, CancelToken())
    assert facade.calls == ["snapshot", "execute"]
    persisted = json.loads((tmp_path / "journal.json").read_text())
    assert persisted["entries"][task["request_id"]]["state"] == "completed"
    second = Dispatcher(FakeFacade(), ExecutionJournal(tmp_path / "journal.json")).dispatch(task, CancelToken())
    assert second == first


def test_completed_mutation_report_survives_restart_until_exact_receipt(tmp_path):
    path = tmp_path / "journal.json"
    task = _task()
    journal = ExecutionJournal(path)
    Dispatcher(FakeFacade(), journal).dispatch(task, CancelToken())
    report = {
        "contract_version": "1.0.0",
        "report_id": str(uuid.uuid4()),
        "request_id": task["request_id"],
        "result": {"status": "success"},
    }

    journal.record_agent_report(task["request_id"], report)
    restarted = ExecutionJournal(path)
    assert restarted.pending_agent_reports() == [report]

    with pytest.raises(JournalSafetyError):
        restarted.mark_agent_reported(task["request_id"], str(uuid.uuid4()))
    restarted.mark_agent_reported(task["request_id"], report["report_id"])
    assert ExecutionJournal(path).pending_agent_reports() == []


def test_started_only_journal_is_indeterminate_and_never_replays(tmp_path):
    task = _task()
    journal = ExecutionJournal(tmp_path / "journal.json")
    journal.record_started(task, {"before": "x"})
    facade = FakeFacade()
    result = Dispatcher(facade, ExecutionJournal(tmp_path / "journal.json")).dispatch(task, CancelToken())
    assert result["status"] == "indeterminate"
    assert facade.calls == []


@pytest.mark.parametrize("content", [b'{"version":1,"entries":', b'{"version":9,"entries":{}}', b'not-json'])
def test_corrupt_or_truncated_journal_fails_closed(tmp_path, content):
    path = tmp_path / "journal.json"
    path.write_bytes(content)
    with pytest.raises(JournalSafetyError):
        ExecutionJournal(path)


def test_lease_or_intent_mismatch_fails_closed(tmp_path):
    task = _task()
    journal = ExecutionJournal(tmp_path / "journal.json")
    journal.record_started(task, {})
    with pytest.raises(JournalSafetyError):
        journal.reconcile({**task, "intent_hash": "changed"})
    with pytest.raises(JournalSafetyError):
        journal.reconcile({**task, "lease_id": str(uuid.uuid4())})


def test_atomic_replace_failure_prevents_execute(tmp_path, monkeypatch):
    facade = FakeFacade()
    task = _task()
    journal = ExecutionJournal(tmp_path / "journal.json")
    monkeypatch.setattr(os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("disk")))
    result = Dispatcher(facade, journal).dispatch(task, CancelToken())
    assert result["status"] == "indeterminate"
    assert facade.calls == ["snapshot"]


def test_config_rejects_non_loopback_and_short_secret(tmp_path):
    config = tmp_path / "connector.json"
    config.write_text(json.dumps({
        "runtime_url": "https://example.com", "connector_secret": "short",
        "connector_instance_id": str(uuid.uuid4()), "artifact_root": str(tmp_path / "a"),
    }))
    from fusion_addin.CADAgentFusionConnector.config import load_config
    with pytest.raises(RuntimeError):
        load_config(config)


@pytest.mark.parametrize(
    "runtime_url",
    [
        "http://localhost:8765",
        "http://127.0.0.1:8765/redirect",
        "http://user:password@127.0.0.1:8765",
        "http://127.0.0.1:8765?next=https://example.com",
    ],
)
def test_config_rejects_ambiguous_loopback_urls(tmp_path, runtime_url):
    config = tmp_path / "connector.json"
    config.write_text(json.dumps({
        "runtime_url": runtime_url, "connector_secret": "x" * 32,
        "connector_instance_id": str(uuid.uuid4()), "artifact_root": str(tmp_path / "a"),
    }))
    from fusion_addin.CADAgentFusionConnector.config import load_config
    with pytest.raises(RuntimeError):
        load_config(config)


def test_failed_result_post_is_requeued_without_losing_artifact_metadata(tmp_path):
    config = ConnectorConfig(
        runtime_url="http://127.0.0.1:8765", connector_secret="x" * 32,
        connector_instance_id=str(uuid.uuid4()), artifact_root=tmp_path,
        journal_path=tmp_path / "journal.json",
    )

    class FlakyClient:
        def __init__(self):
            self.calls = 0
            self.results = []

        def result(self, task, connector_id, result):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("connection lost after dequeue")
            self.results.append(result)

    outbound = queue.Queue()
    task = _task()
    result = {"status": "success", "_local_artifacts": [{"relative_path": "part.step"}]}
    outbound.put((task, result))
    client = FlakyClient()
    worker = ConnectorWorker(config, {}, queue.Queue(), outbound, lambda: None, client=client)
    worker.current_task = task
    with pytest.raises(RuntimeError):
        worker._flush_results()
    assert outbound.qsize() == 1
    assert result["_local_artifacts"][0]["relative_path"] == "part.step"
    worker._flush_results()
    assert client.results[0]["_local_artifacts"][0]["relative_path"] == "part.step"
    assert worker.current_task is None


@pytest.mark.parametrize(
    ("code", "category"),
    [
        ("DOCUMENT_MISMATCH", "conflict"),
        ("TARGET_AMBIGUOUS", "conflict"),
        ("FEATURE_NOT_FOUND", "not_found"),
        ("INVALID_ACTION", "validation"),
        ("ARTIFACT_INVALID", "artifact"),
    ],
)
def test_addin_error_categories_match_the_public_contract(code, category):
    result = failure_result(_task(), code, "safe message", {})
    assert result["error"]["category"] == category
