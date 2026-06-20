"""REST HTTP contract tests — happy paths + status codes for every endpoint.

Hermetic: no Docker, no LLM, no network. The orchestrator singleton used by the
REST routes (app.api.websocket._orchestrator) is replaced with a fake whose
async generate/modify/execute_code return canned GenerateResponse objects, OR
with the REAL harness orchestrator (build_orchestrator) when we want the real
code_filter / geometry validator to run.

Storage + DB are isolated per test via settings.file_storage_dir / history_db_path.

Run:
  cd /Users/wentao/CAD/datachat_new/backend && \
  python3 -m pytest tests/test_deploy_http_contracts.py -p no:cacheprovider -o addopts="" -q
"""
import asyncio
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

import app.api.websocket as websocket_mod
from app.config import settings
from app.main import app
from app.models.schemas import GenerateResponse, ValidationResult
from app.storage import history

from tests.e2e_harness import build_orchestrator, patch_single_step


# --------------------------------------------------------------------------- #
# Fake orchestrator: returns canned GenerateResponse from async methods.
# REST routes call _get_orchestrator() which returns websocket_mod._orchestrator
# when it is already set, so injecting here fully bypasses Orchestrator().
# --------------------------------------------------------------------------- #
@dataclass
class FakeOrchestrator:
    response: GenerateResponse | None = None
    raises: Exception | None = None
    calls: list = field(default_factory=list)

    def _result(self):
        if self.raises is not None:
            raise self.raises
        return self.response

    async def generate(self, prompt, output_formats):
        self.calls.append(("generate", prompt, tuple(output_formats)))
        return self._result()

    async def modify(self, code, prompt, output_formats):
        self.calls.append(("modify", code, prompt, tuple(output_formats)))
        return self._result()

    async def execute_code(self, code, output_formats):
        self.calls.append(("execute_code", code, tuple(output_formats)))
        return self._result()


def _success_response(request_id="req-http-1") -> GenerateResponse:
    return GenerateResponse(
        request_id=request_id,
        success=True,
        files={"stl": f"/api/files/{request_id}/result.stl",
               "step": f"/api/files/{request_id}/result.step"},
        code="result = box(20, 30, 40)\nshow_object(result)",
        execution_time_ms=42,
        attempts=1,
        validation=ValidationResult(
            is_watertight=True, volume=24000.0, printable=True,
            fits_build_volume=True,
        ),
    )


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def isolated_storage(tmp_path, monkeypatch):
    """Point file storage + history DB at tmp, ensure a fresh DB connection."""
    monkeypatch.setattr(settings, "file_storage_dir", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "history_db_path", str(tmp_path / "history.db"))
    # Make sure no stale global DB connection leaks across tests.
    asyncio.get_event_loop_policy()
    asyncio.run(history.close_db())
    yield tmp_path
    asyncio.run(history.close_db())


@pytest.fixture
def client(isolated_storage):
    """TestClient runs the lifespan (startup self-check -> startup_problems set)."""
    with TestClient(app) as c:
        yield c
    # Reset injected orchestrator so tests don't bleed into each other.
    websocket_mod._orchestrator = None


@pytest.fixture
def inject_orch():
    """Return a setter; auto-reset the singleton on teardown."""
    def _set(orch):
        websocket_mod._orchestrator = orch
        return orch
    yield _set
    websocket_mod._orchestrator = None


# --------------------------------------------------------------------------- #
# /health  &  /ready
# --------------------------------------------------------------------------- #
def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_ready_degraded_no_docker_no_key(client):
    """No Docker + no LLM key in this env => startup self-check finds problems => 503."""
    r = client.get("/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert isinstance(body["problems"], list) and body["problems"]


# --------------------------------------------------------------------------- #
# POST /api/generate
# --------------------------------------------------------------------------- #
def test_generate_success_shape(client, inject_orch):
    inject_orch(FakeOrchestrator(response=_success_response("gen-1")))
    r = client.post("/api/generate", json={"prompt": "a 20x30x40 box"})
    assert r.status_code == 200
    body = r.json()
    # GenerateResponse shape
    assert body["request_id"] == "gen-1"
    assert body["success"] is True
    assert body["files"]["stl"].endswith("result.stl")
    assert body["files"]["step"].endswith("result.step")
    assert "show_object" in body["code"]
    assert body["attempts"] == 1
    assert body["execution_time_ms"] == 42
    # validation block present + typed
    assert body["validation"]["is_watertight"] is True
    assert body["validation"]["printable"] is True
    assert body["error"] is None


def test_generate_failure_is_500_with_error(client, inject_orch):
    fail = GenerateResponse(
        request_id="gen-fail",
        success=False,
        error={"type": "ExecutionError", "message": "boom"},
    )
    inject_orch(FakeOrchestrator(response=fail))
    r = client.post("/api/generate", json={"prompt": "broken"})
    assert r.status_code == 500
    body = r.json()
    assert body["success"] is False
    assert body["error"]["type"] == "ExecutionError"
    assert body["request_id"] == "gen-fail"


def test_generate_unhandled_exception_is_500(client, inject_orch):
    inject_orch(FakeOrchestrator(raises=RuntimeError("kapow")))
    r = client.post("/api/generate", json={"prompt": "x"})
    assert r.status_code == 500
    body = r.json()
    assert body["success"] is False
    assert body["error"]["type"] == "RuntimeError"
    assert "kapow" in body["error"]["message"]


def test_generate_validation_422_empty_prompt(client):
    # min_length=1 -> empty string rejected by pydantic before orchestrator.
    r = client.post("/api/generate", json={"prompt": ""})
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# POST /api/modify
# --------------------------------------------------------------------------- #
def test_modify_success_shape(client, inject_orch):
    inject_orch(FakeOrchestrator(response=_success_response("mod-1")))
    r = client.post(
        "/api/modify",
        json={"code": "result = box(1, 2, 3)\nshow_object(result)",
              "prompt": "make it bigger"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["request_id"] == "mod-1"
    assert body["success"] is True
    assert body["files"]["step"]


def test_modify_failure_is_500(client, inject_orch):
    fail = GenerateResponse(request_id="mod-f", success=False,
                            error={"type": "ValidationError", "message": "nope"})
    inject_orch(FakeOrchestrator(response=fail))
    r = client.post("/api/modify",
                    json={"code": "result = box(1,2,3)", "prompt": "x"})
    assert r.status_code == 500
    assert r.json()["error"]["type"] == "ValidationError"


def test_modify_validation_422_blank_code(client):
    r = client.post("/api/modify", json={"code": "", "prompt": "x"})
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# POST /api/execute
#   - happy path with a fake orchestrator
#   - REAL orchestrator + code_filter rejection (import os) => success=False
#     => route returns 500 with error.type == "ValidationError"
# --------------------------------------------------------------------------- #
def test_execute_success_shape(client, inject_orch):
    inject_orch(FakeOrchestrator(response=_success_response("exec-1")))
    r = client.post("/api/execute",
                    json={"code": "result = box(20, 30, 40)\nshow_object(result)"})
    assert r.status_code == 200
    body = r.json()
    assert body["request_id"] == "exec-1"
    assert body["success"] is True
    assert body["files"]["stl"]


def test_execute_code_filter_rejection_real_orchestrator(client, inject_orch):
    """The REAL orchestrator.execute_code runs validate_code first; 'import os'
    is rejected -> GenerateResponse(success=False, error.type=ValidationError)
    -> route maps to HTTP 500. This exercises the real code_filter gate, no Docker."""
    inject_orch(build_orchestrator())  # real Orchestrator, faked LLM/Docker boundaries
    r = client.post("/api/execute",
                    json={"code": "import os\nos.system('rm -rf /')"})
    assert r.status_code == 500
    body = r.json()
    assert body["success"] is False
    assert body["error"]["type"] == "ValidationError"
    # message mentions the forbidden module (Chinese: 禁止导入模块: os)
    assert "os" in body["error"]["message"]


def test_execute_real_orchestrator_happy_path(client, inject_orch, monkeypatch):
    """End-to-end execute through the REAL orchestrator (FakeExecutor writes a real
    printable STL, copied for real). Confirms the 200 success contract.

    CONTRACT (fixed): /api/execute now runs the SAME printability gate as /api/generate
    on the 3D success path, so parameter edits get watertight/printable/build-volume
    feedback (previously validation was silently null).
    """
    patch_single_step(monkeypatch)
    inject_orch(build_orchestrator(
        executor_outcomes=[{"success": True, "stl": "printable"}],
    ))
    r = client.post(
        "/api/execute",
        json={"code": "w = 20  # width\nresult = box(w, 30, 40)\nshow_object(result)"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["request_id"]
    assert body["attempts"] == 1
    assert "show_object" in body["code"]
    # execute now runs the printability gate (a real printable STL → printable True)
    assert body["validation"] is not None
    assert body["validation"]["printable"] is True
    assert body["validation"]["is_watertight"] is True
    assert body["files"]  # at least one output file copied (real STL/STEP)
    assert any(k in body["files"] for k in ("stl", "step"))


def test_execute_validation_422_blank_code(client):
    r = client.post("/api/execute", json={"code": ""})
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# GET /api/parts/{designation}
# --------------------------------------------------------------------------- #
def test_parts_known_screw_m3(client):
    r = client.get("/api/parts/M3")
    assert r.status_code == 200
    body = r.json()
    assert body["designation"] == "M3"
    assert body["params"]["pitch"] == 0.5
    assert "tap" in body["params"]


def test_parts_known_bearing_608(client):
    r = client.get("/api/parts/608")
    assert r.status_code == 200
    body = r.json()
    assert body["designation"] == "608"
    assert body["params"]["inner"] == 8
    assert body["params"]["outer"] == 22


def test_parts_lowercase_is_upcased(client):
    r = client.get("/api/parts/m3")
    assert r.status_code == 200
    assert r.json()["designation"] == "M3"


def test_parts_unknown_404(client):
    r = client.get("/api/parts/NOPE999")
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# GET /api/files/{request_id}/{filename}
# --------------------------------------------------------------------------- #
def test_files_bad_extension_400(client):
    r = client.get("/api/files/somereq/model.exe")
    assert r.status_code == 400
    assert "not allowed" in r.json()["detail"]


def test_files_bad_request_id_400(client):
    r = client.get("/api/files/bad..id/model.step")
    assert r.status_code == 400
    assert "Invalid request ID" in r.json()["detail"]


def test_files_missing_file_404(client, isolated_storage):
    r = client.get("/api/files/req-without-file/result.step")
    assert r.status_code == 404
    assert "File not found" in r.json()["detail"]


def test_files_success_when_file_present(client, isolated_storage):
    from pathlib import Path
    req_id = "req-with-file"
    d = Path(settings.file_storage_dir) / req_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "result.step").write_bytes(b"ISO-10303-21;\n")
    r = client.get(f"/api/files/{req_id}/result.step")
    assert r.status_code == 200
    assert r.content == b"ISO-10303-21;\n"
    assert "attachment" in r.headers["content-disposition"]


# --------------------------------------------------------------------------- #
# POST/GET /api/feedback  (round trip + 422)
# --------------------------------------------------------------------------- #
def test_feedback_round_trip(client):
    rid = "fb-req-1"
    post = client.post(
        "/api/feedback",
        json={"request_id": rid, "rating": "up", "printed": "yes", "note": "great"},
    )
    assert post.status_code == 200
    pbody = post.json()
    assert pbody["ok"] is True
    assert pbody["feedback"]["request_id"] == rid
    assert pbody["feedback"]["rating"] == "up"
    assert pbody["feedback"]["printed"] == "yes"

    get = client.get(f"/api/feedback/{rid}")
    assert get.status_code == 200
    rows = get.json()
    assert isinstance(rows, list) and len(rows) == 1
    assert rows[0]["request_id"] == rid
    assert rows[0]["rating"] == "up"
    assert rows[0]["printed"] == "yes"
    assert rows[0]["note"] == "great"


def test_feedback_invalid_rating_422(client):
    r = client.post("/api/feedback",
                    json={"request_id": "fb-bad", "rating": "meh"})
    assert r.status_code == 422


def test_feedback_invalid_printed_422(client):
    r = client.post("/api/feedback",
                    json={"request_id": "fb-bad2", "printed": "maybe"})
    assert r.status_code == 422


def test_feedback_get_empty_returns_empty_list(client):
    r = client.get("/api/feedback/never-seen")
    assert r.status_code == 200
    assert r.json() == []


# --------------------------------------------------------------------------- #
# /api/history  sessions / panels / messages happy path
#   write via history.* then read back through the REST endpoints.
# --------------------------------------------------------------------------- #
def test_history_sessions_panels_messages_round_trip(client):
    session_id = "sess-http-1"
    panel_id = "panel-http-1"

    async def _seed():
        await history.create_session(session_id, title="My Session")
        await history.create_panel(session_id, panel_id, title="Panel A")
        await history.save_message(panel_id, "user", "make a box")
        await history.save_message(panel_id, "assistant", "done",
                                   result={"request_id": "r1"})

    asyncio.run(_seed())

    # sessions
    rs = client.get("/api/history/sessions")
    assert rs.status_code == 200
    sessions = rs.json()
    assert any(s["id"] == session_id and s["title"] == "My Session" for s in sessions)

    # panels
    rp = client.get(f"/api/history/sessions/{session_id}/panels")
    assert rp.status_code == 200
    panels = rp.json()
    assert any(p["id"] == panel_id for p in panels)

    # messages
    rm = client.get(f"/api/history/panels/{panel_id}/messages")
    assert rm.status_code == 200
    msgs = rm.json()
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "make a box"
    assert msgs[1]["result"] == {"request_id": "r1"}


def test_history_delete_session(client):
    session_id = "sess-del-1"
    asyncio.run(history.create_session(session_id, title="ToDelete"))

    d = client.delete(f"/api/history/sessions/{session_id}")
    assert d.status_code == 200
    assert d.json() == {"ok": True}

    rs = client.get("/api/history/sessions")
    assert all(s["id"] != session_id for s in rs.json())


def test_history_empty_panels_for_unknown_session(client):
    r = client.get("/api/history/sessions/does-not-exist/panels")
    assert r.status_code == 200
    assert r.json() == []
