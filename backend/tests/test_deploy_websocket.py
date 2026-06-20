"""Hermetic WebSocket end-to-end tests for app.api.websocket.websocket_endpoint.

Drives the REAL FastAPI WS route via TestClient.websocket_connect, with a FAKE
orchestrator injected (set websocket._orchestrator). The fake's
handle_message / execute_code / modify_assembly_part are async and emit on_step,
so we exercise the exact payload shapes the endpoint sends.

No Docker, no LLM, no network. DB + storage isolated per test via monkeypatched
settings. Auth is OFF by default (settings.api_keys == []).
"""
import asyncio
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.api.websocket as ws_mod
from app.config import settings
from app.main import app
from app.models.schemas import GenerateResponse, GenerationResult, StepUpdate
from app.storage import history


# --- fake orchestrator --------------------------------------------------------

@dataclass
class FakeOrchestrator:
    """Stands in for the real Orchestrator at the WS boundary.

    handle_message / modify_assembly_part emit one or more StepUpdate via on_step
    and then return a GenerationResult. execute_code returns a GenerateResponse.
    Behaviour is scriptable: set .succeed False to flip the result.
    """
    succeed: bool = True
    code: str = "result = box(10, 10)\nshow_object(result)"
    handle_calls: list = field(default_factory=list)
    modify_calls: list = field(default_factory=list)
    execute_calls: list = field(default_factory=list)

    async def handle_message(self, context, text, on_step=None):
        self.handle_calls.append(text)
        if on_step:
            await on_step(StepUpdate(step="planning", message="规划中..."))
            await on_step(StepUpdate(step="generating_code", message="生成代码..."))
        return GenerationResult(
            success=self.succeed,
            request_id="req-handle",
            files={"stl": f"/api/files/req-handle/result.stl"},
            code=self.code if self.succeed else None,
            attempts=1,
            error=None if self.succeed else {"type": "ExecutionError", "message": "boom"},
        )

    async def modify_assembly_part(self, context, part_name, instruction, on_step=None):
        self.modify_calls.append((part_name, instruction))
        if on_step:
            await on_step(StepUpdate(step="assembly_part", message="修改零件...", part_name=part_name))
        return GenerationResult(
            success=self.succeed,
            request_id="req-modify",
            code=self.code if self.succeed else None,
            attempts=1,
            error=None if self.succeed else {"type": "ExecutionError", "message": "boom"},
        )

    async def execute_code(self, code, output_formats=None):
        self.execute_calls.append(code)
        return GenerateResponse(
            request_id="req-exec",
            success=self.succeed,
            files={"stl": "/api/files/req-exec/result.stl"} if self.succeed else {},
            code=code if self.succeed else None,
            execution_time_ms=5,
            attempts=1,
            error=None if self.succeed else {"type": "ExecutionError", "message": "boom"},
        )


# --- fixtures -----------------------------------------------------------------

@pytest.fixture
def fake_orch(tmp_path, monkeypatch):
    """Isolate storage + DB, inject a fresh FakeOrchestrator into the WS singleton,
    and ensure auth is OFF. Resets global state on teardown."""
    monkeypatch.setattr(settings, "file_storage_dir", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "history_db_path", str(tmp_path / "history.db"))
    monkeypatch.setattr(settings, "api_keys", [])

    # Fresh DB connection bound to the tmp path.
    asyncio.run(history.close_db())

    orch = FakeOrchestrator()
    monkeypatch.setattr(ws_mod, "_orchestrator", orch)
    # Clear any cached per-session context from previous tests.
    ws_mod.sessions.clear()

    yield orch

    asyncio.run(history.close_db())
    ws_mod.sessions.clear()


@pytest.fixture
def client():
    return TestClient(app)


# --- (1) user_message: step_update(s) then generation_result -----------------

def test_user_message_streams_steps_then_success_result(client, fake_orch):
    with client.websocket_connect("/ws/sess-1") as wsk:
        wsk.send_json({"type": "user_message", "text": "make a box", "panel_id": "p1"})

        m1 = wsk.receive_json()
        m2 = wsk.receive_json()
        final = wsk.receive_json()

    assert m1["type"] == "step_update"
    assert m1["data"]["step"] == "planning"
    assert m1["data"]["panel_id"] == "p1"

    assert m2["type"] == "step_update"
    assert m2["data"]["step"] == "generating_code"
    assert m2["data"]["panel_id"] == "p1"

    assert final["type"] == "generation_result"
    assert final["data"]["success"] is True
    assert final["data"]["panel_id"] == "p1"
    assert final["data"]["code"] == fake_orch.code
    assert fake_orch.handle_calls == ["make a box"]


def test_user_message_default_panel_id(client, fake_orch):
    with client.websocket_connect("/ws/sess-defpanel") as wsk:
        wsk.send_json({"type": "user_message", "text": "hello"})
        # drain the two step_updates
        wsk.receive_json()
        wsk.receive_json()
        final = wsk.receive_json()
    assert final["type"] == "generation_result"
    assert final["data"]["panel_id"] == "default"


# --- (2) empty / oversized text -> ValidationError ---------------------------

def test_user_message_empty_text_validation_error(client, fake_orch):
    with client.websocket_connect("/ws/sess-2") as wsk:
        wsk.send_json({"type": "user_message", "text": "", "panel_id": "p1"})
        msg = wsk.receive_json()

    assert msg["type"] == "generation_result"
    assert msg["data"]["success"] is False
    assert msg["data"]["error"]["type"] == "ValidationError"
    assert msg["data"]["panel_id"] == "p1"
    # validation rejected before any orchestrator call
    assert fake_orch.handle_calls == []


def test_user_message_oversized_text_validation_error(client, fake_orch):
    big = "x" * 10001
    with client.websocket_connect("/ws/sess-3") as wsk:
        wsk.send_json({"type": "user_message", "text": big})
        msg = wsk.receive_json()

    assert msg["type"] == "generation_result"
    assert msg["data"]["error"]["type"] == "ValidationError"
    assert fake_orch.handle_calls == []


# --- (3) execute_code -> generation_result -----------------------------------

def test_execute_code_returns_generation_result(client, fake_orch):
    code = "result = box(5, 5)\nshow_object(result)"
    with client.websocket_connect("/ws/sess-4") as wsk:
        wsk.send_json({"type": "execute_code", "code": code, "panel_id": "pX"})
        msg = wsk.receive_json()

    assert msg["type"] == "generation_result"
    assert msg["data"]["success"] is True
    assert msg["data"]["request_id"] == "req-exec"
    assert msg["data"]["code"] == code
    assert msg["data"]["panel_id"] == "pX"
    assert fake_orch.execute_calls == [code]


def test_execute_code_empty_is_silently_ignored(client, fake_orch):
    """Empty code hits `continue` with no response — so a follow-up cancel is what
    we actually receive back (proves the empty execute produced nothing)."""
    with client.websocket_connect("/ws/sess-4b") as wsk:
        wsk.send_json({"type": "execute_code", "code": "", "panel_id": "p1"})
        wsk.send_json({"type": "cancel", "panel_id": "p1"})
        msg = wsk.receive_json()

    assert msg["type"] == "generation_result"
    assert msg["data"]["error"]["type"] == "Cancelled"
    assert fake_orch.execute_calls == []


# --- (4) modify_part missing fields -> ValidationError -----------------------

def test_modify_part_missing_part_name_validation_error(client, fake_orch):
    with client.websocket_connect("/ws/sess-5") as wsk:
        wsk.send_json({"type": "modify_part", "instruction": "make it bigger", "panel_id": "p1"})
        msg = wsk.receive_json()

    assert msg["type"] == "generation_result"
    assert msg["data"]["success"] is False
    assert msg["data"]["error"]["type"] == "ValidationError"
    assert msg["data"]["panel_id"] == "p1"
    assert fake_orch.modify_calls == []


def test_modify_part_missing_instruction_validation_error(client, fake_orch):
    with client.websocket_connect("/ws/sess-5b") as wsk:
        wsk.send_json({"type": "modify_part", "part_name": "bolt", "panel_id": "p1"})
        msg = wsk.receive_json()

    assert msg["data"]["error"]["type"] == "ValidationError"
    assert fake_orch.modify_calls == []


def test_modify_part_valid_streams_step_then_result(client, fake_orch):
    with client.websocket_connect("/ws/sess-5c") as wsk:
        wsk.send_json({
            "type": "modify_part", "part_name": "bolt",
            "instruction": "make it longer", "panel_id": "p2",
        })
        step = wsk.receive_json()
        final = wsk.receive_json()

    assert step["type"] == "step_update"
    assert step["data"]["step"] == "assembly_part"
    assert step["data"]["part_name"] == "bolt"
    assert step["data"]["panel_id"] == "p2"

    assert final["type"] == "generation_result"
    assert final["data"]["success"] is True
    assert final["data"]["panel_id"] == "p2"
    assert fake_orch.modify_calls == [("bolt", "make it longer")]


# --- (5) cancel -> Cancelled result ------------------------------------------

def test_cancel_returns_cancelled_result(client, fake_orch):
    with client.websocket_connect("/ws/sess-6") as wsk:
        wsk.send_json({"type": "cancel", "panel_id": "p9"})
        msg = wsk.receive_json()

    assert msg["type"] == "generation_result"
    assert msg["data"]["success"] is False
    assert msg["data"]["error"]["type"] == "Cancelled"
    assert msg["data"]["panel_id"] == "p9"


# --- (6) restore_context: sets context, no crash, no response ----------------

def test_restore_context_no_response_then_still_alive(client, fake_orch):
    """restore_context sends nothing back. Prove it (a) didn't crash and (b) the
    connection stays usable by following with a cancel and reading its reply."""
    with client.websocket_connect("/ws/sess-7") as wsk:
        wsk.send_json({"type": "restore_context", "code": "result = box(1,1)", "panel_id": "pr"})
        wsk.send_json({"type": "cancel", "panel_id": "pr"})
        msg = wsk.receive_json()

    assert msg["type"] == "generation_result"
    assert msg["data"]["error"]["type"] == "Cancelled"
    # context was actually stored on the panel
    ctx = ws_mod.sessions["sess-7"]["pr"]
    assert ctx.current_code == "result = box(1,1)"


# --- (7) invalid session_id -> close 4001 ------------------------------------

def test_invalid_session_id_closes_4001(client, fake_orch):
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/ws/bad id!") as wsk:
            # the endpoint closes before accept; any recv raises disconnect
            wsk.receive_json()
    assert excinfo.value.code == 4001


# --- (8) panel_id isolation: different panels don't cross --------------------

def test_panel_id_isolation_results_echo_correct_panel(client, fake_orch):
    with client.websocket_connect("/ws/sess-8") as wsk:
        wsk.send_json({"type": "user_message", "text": "panel A msg", "panel_id": "A"})
        a1 = wsk.receive_json()
        a2 = wsk.receive_json()
        a_final = wsk.receive_json()

        wsk.send_json({"type": "user_message", "text": "panel B msg", "panel_id": "B"})
        b1 = wsk.receive_json()
        b2 = wsk.receive_json()
        b_final = wsk.receive_json()

    for m in (a1, a2, a_final):
        assert m["data"]["panel_id"] == "A"
    for m in (b1, b2, b_final):
        assert m["data"]["panel_id"] == "B"

    # two distinct ConversationContexts cached under the same session
    panels = ws_mod.sessions["sess-8"]
    assert set(panels.keys()) == {"A", "B"}
    assert panels["A"] is not panels["B"]
    assert fake_orch.handle_calls == ["panel A msg", "panel B msg"]
