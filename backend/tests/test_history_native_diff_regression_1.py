"""Q05: compare immutable native evidence, not empty legacy placeholders."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.config import settings
from app.storage import history, postgres_history


def _native_row():
    workflow_id = str(uuid4())
    return {
        "id": uuid4(), "project_id": uuid4(), "branch_id": uuid4(),
        "parent_revision_id": uuid4(), "revision_number": 3,
        "created_at": datetime.now(timezone.utc), "workflow_status": "succeeded",
        "manifest": {
            "schema_version": "mcad-agent-revision-manifest.v1",
            "workflow_run_id": workflow_id, "operation": "modify",
            "objective": "将平板增厚到 12 mm",
            "artifacts": [
                {"artifact_kind": "fcstd", "filename": "model.FCStd", "sha256": "a" * 64},
                {"artifact_kind": "step", "filename": "model.step", "sha256": "b" * 64},
            ],
            "validation": {"gates": [
                {"gate": "geometry", "mode": "required", "outcome": "passed"},
                {"gate": "visual", "mode": "advisory", "outcome": "indeterminate"},
            ]},
        },
    }


def test_native_history_projects_sealed_files_and_real_source():
    row = _native_row()
    row["manifest"]["artifacts"].append({"artifact_kind": "visual_render", "filename": "top.png", "sha256": "c" * 64})
    snapshot = postgres_history._snapshot_from_revision(row)
    assert snapshot["source"] == "modify_part"
    assert snapshot["prompt"] == "将平板增厚到 12 mm"
    assert snapshot["code"] == ""
    assert snapshot["files"]["fcstd"] == (
        f"/api/files/{row['manifest']['workflow_run_id']}/model.FCStd"
    )
    assert snapshot["result"]["files"] == snapshot["files"]
    assert snapshot["result"]["validation"] == row["manifest"]["validation"]
    assert snapshot["available_exports"] == ["fcstd", "step"]


def test_artifact_comparison_identity_survives_another_file_of_the_same_kind():
    original = {"artifact_kind": "dfm_report", "filename": "model-dfm.json", "sha256": "a" * 64}
    extra = {"artifact_kind": "dfm_report", "filename": "manual-dfm.json", "sha256": "b" * 64}
    _, before = postgres_history._revision_artifact_projection([original], "workflow")
    _, after = postgres_history._revision_artifact_projection([original, extra], "workflow")
    assert before == {"dfm_report:model-dfm.json": "a" * 64}
    assert after["dfm_report:model-dfm.json"] == before["dfm_report:model-dfm.json"]


def test_incomplete_artifact_hashes_never_claim_known_content():
    files, fingerprints = postgres_history._revision_artifact_projection([
        {"artifact_kind": "step", "filename": "model.step", "sha256": ""},
    ], "workflow")
    assert files["step"] == "/api/files/workflow/model.step"
    assert fingerprints is None


def _snapshot(value=10):
    return {
        "code": "", "files": {"fcstd": "/api/files/a/model.FCStd", "step": "/api/files/a/model.step"},
        "params": None,
        "parameters": [{"name": "Pad.Length", "value": value, "unit": "mm", "source": "freecad"}],
        "_file_fingerprints": {"fcstd": "a" * 64, "step": "b" * 64},
        "validation": {"gates": [{"gate": "geometry", "mode": "required", "outcome": "passed"}]},
    }


@pytest.fixture
def snapshots(monkeypatch):
    monkeypatch.setattr(settings, "durable_control_plane_enabled", False)
    data = {"before": _snapshot(), "after": _snapshot(12)}

    async def owned(_snapshot_id, _user_id):
        return True

    async def get(snapshot_id, **_kwargs):
        return deepcopy(data[snapshot_id])

    monkeypatch.setattr(history, "snapshot_belongs_to_user", owned)
    monkeypatch.setattr(history, "get_model_snapshot", get)
    return data


@pytest.mark.asyncio
async def test_native_diff_uses_parameter_identity_and_artifact_hashes(snapshots):
    snapshots["after"]["files"] = {"fcstd": "/api/files/b/model.FCStd", "step": "/api/files/b/model.step"}
    snapshots["after"]["_file_fingerprints"]["fcstd"] = "c" * 64
    snapshots["after"]["validation"]["gates"].append(
        {"gate": "visual", "mode": "advisory", "outcome": "indeterminate"},
    )
    diff = await history.diff_model_snapshots("before", "after")
    assert diff["parameter_changes"]["changed"] == ["Pad.Length"]
    assert diff["file_changes"]["changed"] == ["fcstd"]
    assert diff["file_changes"]["unchanged"] == ["step"]
    assert "code_changed" not in diff["model_changes"]
    assert diff["model_changes"]["inspect_verdict"] == {"from": "pass", "to": "warn"}


@pytest.mark.asyncio
async def test_identical_native_version_has_actual_unchanged_evidence(snapshots):
    diff = await history.diff_model_snapshots("before", "before")
    assert diff["parameter_changes"]["unchanged"] == ["Pad.Length"]
    assert diff["file_changes"]["unchanged"] == ["fcstd", "step"]


@pytest.mark.asyncio
async def test_missing_evidence_is_not_reported_as_zero_changes(snapshots):
    snapshots["before"] = {"code": "", "files": {}, "params": None}
    snapshots["after"] = dict(snapshots["before"])
    diff = await history.diff_model_snapshots("before", "after")
    assert "code_changed" not in diff["model_changes"]
    assert "file_changes" not in diff
    assert "parameter_changes" not in diff
    assert "part_changes" not in diff


@pytest.mark.asyncio
async def test_duplicate_parameter_ids_are_not_silently_compared(snapshots):
    snapshots["after"]["parameters"].append(dict(snapshots["after"]["parameters"][0]))
    diff = await history.diff_model_snapshots("before", "after")
    assert "parameter_changes" not in diff


@pytest.mark.asyncio
async def test_missing_parts_on_one_side_are_not_reported_as_added_parts(snapshots):
    snapshots["after"]["assembly_parts"] = [{"part_id": "base", "name": "base", "code_hash": "a"}]
    diff = await history.diff_model_snapshots("before", "after")
    assert "part_changes" not in diff


@pytest.mark.asyncio
async def test_disabled_gates_do_not_claim_inspection_pass(snapshots):
    for snapshot in snapshots.values():
        snapshot["validation"] = {"gates": [{"gate": "visual", "mode": "disabled", "outcome": "disabled"}]}
    diff = await history.diff_model_snapshots("before", "after")
    assert diff["model_changes"]["inspect_verdict"] == {"from": None, "to": None}


@pytest.mark.asyncio
async def test_diff_does_not_read_an_unauthorized_snapshot(monkeypatch):
    async def denied(_snapshot_id, _user_id):
        return False

    async def forbidden(*_args, **_kwargs):
        pytest.fail("unauthorized snapshot was read")

    monkeypatch.setattr(history, "snapshot_belongs_to_user", denied)
    monkeypatch.setattr(history, "get_model_snapshot", forbidden)
    assert await history.diff_model_snapshots("before", "after", "other-user") is None
