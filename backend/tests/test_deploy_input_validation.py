"""Input validation / fuzzing tests for the REST surface.

Angle: malformed / hostile / boundary input must yield a CLEAN 4xx (422 from
pydantic, 400 from explicit endpoint guards) — NEVER a 500 or an unhandled crash.

All hermetic: no containers, LLM or network. Valid bodies use a successful fake
at the Durable submission/projection boundary; schema and route logic stay real.
"""
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import batch as batch_api
from app.api import execute as execute_api
from app.api import generate as generate_api
from app.config import settings
from app.main import app
from app.models.schemas import GenerateResponse


def _identity() -> dict[str, str]:
    return {
        "project_id": str(uuid.uuid4()),
        "branch_id": str(uuid.uuid4()),
        "expected_base_revision_id": str(uuid.uuid4()),
        "idempotency_key": f"input-validation-{uuid.uuid4()}",
    }


def _batch_items(count: int) -> list[dict]:
    return [{"prompt": f"box {i}", **_identity()} for i in range(count)]


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient with isolated storage/DB, auth OFF, rate-limit effectively off,
    and a successful Durable boundary."""
    monkeypatch.setattr(settings, "file_storage_dir", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "history_db_path", str(tmp_path / "history.db"))
    monkeypatch.setattr(settings, "api_keys", [])  # auth off
    monkeypatch.setattr(settings, "rate_limit_per_minute", 0)  # disable limiter

    async def submit(_principal, **_kwargs):
        return SimpleNamespace(workflow_run_id=uuid.uuid4())

    async def wait(_principal, submission, *, timeout_seconds):
        return GenerateResponse(
            request_id=str(submission.workflow_run_id),
            success=True,
            task_status="succeeded",
            files={"stl": "x"},
        )

    for module in (generate_api, execute_api, batch_api):
        monkeypatch.setattr(module, "submit_durable_workflow", submit)
    for module in (generate_api, execute_api, batch_api):
        monkeypatch.setattr(module, "wait_for_compatibility_response", wait)

    with TestClient(app) as c:
        yield c


# ============================================================================ #
# /api/generate                                                                #
# ============================================================================ #

def test_generate_missing_prompt_422(client):
    r = client.post("/api/generate", json={})
    assert r.status_code == 422


def test_generate_empty_prompt_422(client):
    # min_length=1
    r = client.post("/api/generate", json={"prompt": ""})
    assert r.status_code == 422


def test_generate_prompt_too_long_422(client):
    r = client.post("/api/generate", json={"prompt": "x" * 10001})
    assert r.status_code == 422


def test_generate_prompt_at_max_len_ok(client):
    # exactly 10000 chars is the boundary -> valid
    r = client.post(
        "/api/generate", json={"prompt": "x" * 10000, **_identity()}
    )
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_generate_prompt_wrong_type_int_422(client):
    r = client.post("/api/generate", json={"prompt": 12345})
    assert r.status_code == 422


def test_generate_prompt_wrong_type_list_422(client):
    r = client.post("/api/generate", json={"prompt": ["a", "b"]})
    assert r.status_code == 422


def test_generate_prompt_null_422(client):
    r = client.post("/api/generate", json={"prompt": None})
    assert r.status_code == 422


def test_generate_unicode_emoji_control_chars_accepted(client):
    # unicode + emoji + control chars (NUL, bell, vertical tab) pass schema and
    # are handed through; must NOT 500.
    prompt = "螺栓 M8 \U0001f527 设计\x00\x07\x0b‮gadget"
    r = client.post(
        "/api/generate", json={"prompt": prompt, **_identity()}
    )
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True


def test_generate_output_formats_junk_value_rejected(client):
    # output_formats is now validated against _VALID_FORMATS {step,stl,dxf,svg};
    # unknown formats are rejected at the schema boundary with 422 (was silently
    # accepted before — a real validation gap that is now closed).
    r = client.post("/api/generate", json={"prompt": "box", "output_formats": ["lol", "exe"]})
    assert r.status_code == 422, r.text


def test_generate_output_formats_valid_accepted(client):
    # the declared allowlist still passes
    r = client.post(
        "/api/generate",
        json={"prompt": "box", "output_formats": ["stl", "step", "dxf", "svg"], **_identity()},
    )
    assert r.status_code == 200, r.text


def test_generate_output_formats_wrong_type_422(client):
    # output_formats declared list[str]; a bare string is NOT a list -> 422
    r = client.post("/api/generate", json={"prompt": "box", "output_formats": "stl"})
    assert r.status_code == 422


def test_generate_output_formats_list_of_int_422(client):
    # list[str] with int members -> pydantic coerces? str() of int is allowed in
    # lax mode... assert it never 500s and is a clean status.
    r = client.post(
        "/api/generate",
        json={"prompt": "box", "output_formats": [1, 2], **_identity()},
    )
    assert r.status_code in (200, 422), r.text
    assert r.status_code != 500


def test_generate_extra_unknown_fields_ignored(client):
    r = client.post(
        "/api/generate",
        json={"prompt": "box", "totally_unknown": "x", "nested": {"a": 1}, **_identity()},
    )
    assert r.status_code == 200, r.text


def test_generate_malformed_json_body_422(client):
    r = client.post(
        "/api/generate",
        content=b"{not valid json,,,",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422


def test_generate_empty_body_no_content_type_422(client):
    r = client.post("/api/generate", content=b"")
    assert r.status_code == 422


def test_generate_array_instead_of_object_422(client):
    r = client.post("/api/generate", json=["prompt", "box"])
    assert r.status_code == 422


# ============================================================================ #
# /api/modify                                                                  #
# ============================================================================ #

def test_modify_missing_code_422(client):
    r = client.post("/api/modify", json={"prompt": "make it bigger"})
    assert r.status_code == 422


def test_modify_empty_code_422(client):
    r = client.post("/api/modify", json={"code": "", "prompt": "x"})
    assert r.status_code == 422


def test_modify_code_too_long_422(client):
    r = client.post("/api/modify", json={"code": "x" * 50001, "prompt": "x"})
    assert r.status_code == 422


def test_modify_missing_prompt_422(client):
    r = client.post("/api/modify", json={"code": "result = box(1,1)"})
    assert r.status_code == 422


# ============================================================================ #
# /api/execute                                                                 #
# ============================================================================ #

def test_execute_missing_code_422(client):
    r = client.post("/api/execute", json={})
    assert r.status_code == 422


def test_execute_empty_code_422(client):
    r = client.post("/api/execute", json={"code": ""})
    assert r.status_code == 422


def test_execute_code_too_long_422(client):
    r = client.post("/api/execute", json={"code": "x" * 50001})
    assert r.status_code == 422


def test_execute_code_at_max_len_ok(client):
    # exactly 50000 chars -> valid boundary (fake orchestrator returns success)
    r = client.post(
        "/api/execute", json={"code": "x" * 50000, **_identity()}
    )
    assert r.status_code == 200, r.text


def test_execute_code_wrong_type_422(client):
    r = client.post("/api/execute", json={"code": {"a": 1}})
    assert r.status_code == 422


# ============================================================================ #
# /api/feedback                                                                #
# ============================================================================ #

def test_feedback_missing_request_id_422(client):
    r = client.post("/api/feedback", json={"rating": "up"})
    assert r.status_code == 422


def test_feedback_empty_request_id_422(client):
    r = client.post("/api/feedback", json={"request_id": ""})
    assert r.status_code == 422


def test_feedback_request_id_too_long_422(client):
    r = client.post("/api/feedback", json={"request_id": "x" * 129})
    assert r.status_code == 422


def test_feedback_bad_rating_enum_422(client):
    r = client.post("/api/feedback", json={"request_id": "abc", "rating": "meh"})
    assert r.status_code == 422


def test_feedback_bad_printed_enum_422(client):
    r = client.post("/api/feedback", json={"request_id": "abc", "printed": "maybe"})
    assert r.status_code == 422


def test_feedback_note_too_long_422(client):
    r = client.post("/api/feedback", json={"request_id": "abc", "note": "x" * 2001})
    assert r.status_code == 422


def test_feedback_valid_minimal_ok(client):
    # request_id only -> valid; rating/printed/note optional. Hits real sqlite,
    # but DB is isolated to tmp_path. Must be 200, not 500.
    r = client.post("/api/feedback", json={"request_id": "abc-123"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_feedback_valid_full_ok(client):
    r = client.post(
        "/api/feedback",
        json={"request_id": "abc-123", "rating": "down", "printed": "not_yet", "note": "edge"},
    )
    assert r.status_code == 200, r.text


def test_feedback_malformed_json_422(client):
    r = client.post(
        "/api/feedback",
        content=b"{broken",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422


# ============================================================================ #
# /api/batch/generate                                                          #
# ============================================================================ #

def test_batch_21_items_400(client):
    items = _batch_items(21)
    r = client.post("/api/batch/generate", json={"items": items})
    assert r.status_code == 400, r.text


def test_batch_exactly_20_items_ok(client):
    items = _batch_items(20)
    r = client.post("/api/batch/generate", json={"items": items})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["results"]) == 20
    assert all(x["success"] for x in body["results"])


def test_batch_zero_items_accepted_empty(client):
    r = client.post("/api/batch/generate", json={"items": []})
    assert r.status_code == 200, r.text
    assert r.json()["results"] == []


def test_batch_missing_items_422(client):
    r = client.post("/api/batch/generate", json={"max_concurrent": 3})
    assert r.status_code == 422


def test_batch_item_missing_prompt_422(client):
    r = client.post("/api/batch/generate", json={"items": [{"output_formats": ["stl"]}]})
    assert r.status_code == 422


def test_batch_items_wrong_type_422(client):
    r = client.post("/api/batch/generate", json={"items": "not-a-list"})
    assert r.status_code == 422


# ============================================================================ #
# /api/files/{request_id}/{filename}                                           #
# ============================================================================ #

def test_files_bad_extension_400(client):
    r = client.get("/api/files/abc123/model.exe")
    assert r.status_code == 400, r.text


def test_files_bad_request_id_400(client):
    r = client.get("/api/files/bad..id/model.step")
    assert r.status_code == 400, r.text


def test_files_bad_filename_400(client):
    r = client.get("/api/files/abc123/bad%5Cname.step")
    assert r.status_code in (400, 404), r.text
    assert r.status_code != 500


def test_files_valid_name_but_missing_file_404(client):
    r = client.get("/api/files/abc123/model.step")
    assert r.status_code == 404, r.text


# ============================================================================ #
# cross-cutting: NONE of the malformed-input cases should ever 500             #
# ============================================================================ #

def test_no_endpoint_returns_500_on_garbage(client):
    """Sweep a pile of garbage bodies across every fuzzed endpoint; assert no 500."""
    cases = [
        ("/api/generate", {}),
        ("/api/generate", {"prompt": ""}),
        ("/api/generate", {"prompt": 1}),
        ("/api/generate", {"prompt": None}),
        ("/api/generate", {"prompt": "x" * 20000}),
        ("/api/modify", {"prompt": "x"}),
        ("/api/modify", {"code": ""}),
        ("/api/execute", {}),
        ("/api/execute", {"code": ""}),
        ("/api/feedback", {"rating": "up"}),
        ("/api/feedback", {"request_id": "abc", "rating": "??"}),
        ("/api/batch/generate", {"items": [{"prompt": f"b{i}"} for i in range(21)]}),
        ("/api/batch/generate", {"items": "nope"}),
    ]
    for path, body in cases:
        r = client.post(path, json=body)
        assert r.status_code != 500, f"{path} {body!r} -> 500 ({r.text})"
        assert r.status_code < 500, f"{path} {body!r} -> {r.status_code}"
