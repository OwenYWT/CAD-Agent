"""PERSISTENCE angle: sqlite history (sessions/panels/messages) + feedback + the
in-memory LRU session cache in websocket.py.

Hermetic: no Docker, no LLM, no network. Each test gets a fresh temp DB by
monkeypatching settings.history_db_path and resetting the module-level _db
singleton via history.close_db().
"""
import asyncio

import pytest
import pytest_asyncio

from app.config import settings
from app.storage import history
import app.api.websocket as ws


@pytest_asyncio.fixture
async def temp_db(tmp_path, monkeypatch):
    """Point history at a fresh temp DB and reset the singleton before/after."""
    db_file = tmp_path / "history_test.db"
    monkeypatch.setattr(settings, "history_db_path", str(db_file))
    # Reset any pre-existing connection bound to a different path.
    await history.close_db()
    yield db_file
    await history.close_db()


# --------------------------------------------------------------------------
# Sessions / panels / messages round trip
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_panel_message_round_trip(temp_db):
    sess = await history.create_session("s1", title="My Session")
    assert sess["id"] == "s1"
    assert sess["title"] == "My Session"
    assert sess["created_at"] == sess["updated_at"]

    panel = await history.create_panel("s1", "p1", title="Panel One")
    assert panel == {
        "id": "p1",
        "session_id": "s1",
        "title": "Panel One",
        "created_at": panel["created_at"],
    }

    await history.save_message("p1", "user", "make a box")
    await history.save_message("p1", "assistant", "done")

    msgs = await history.get_messages("p1")
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert [m["content"] for m in msgs] == ["make a box", "done"]
    # No result attached -> no "result" key.
    assert all("result" not in m for m in msgs)


@pytest.mark.asyncio
async def test_create_panel_autocreates_session(temp_db):
    # create_panel calls create_session internally; session must show up.
    await history.create_panel("auto-sess", "panelA")
    sessions = await history.list_sessions()
    assert any(s["id"] == "auto-sess" for s in sessions)
    panels = await history.list_panels("auto-sess")
    assert [p["id"] for p in panels] == ["panelA"]


# --------------------------------------------------------------------------
# Message result JSON survives a round trip intact
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_message_result_json_intact(temp_db):
    await history.create_panel("s2", "p2")
    result = {
        "request_id": "req-123",
        "files": {"stl": "/x/result.stl", "step": "/x/result.step"},
        "params": [{"name": "w", "value": 20, "unit": "mm"}],
        "printability": {"printable": True, "warnings": ["thin wall"]},
        "nested": {"a": [1, 2, {"b": "c"}]},
        "unicode": "宽 30mm",
    }
    await history.save_message("p2", "assistant", "generated", result=result)

    msgs = await history.get_messages("p2")
    assert len(msgs) == 1
    assert msgs[0]["result"] == result  # deep-equal after json dump/load


@pytest.mark.asyncio
async def test_message_falsey_result_not_stored(temp_db):
    # save_message uses `if result else None` -> empty dict is treated as no result.
    await history.create_panel("s3", "p3")
    await history.save_message("p3", "assistant", "x", result={})
    msgs = await history.get_messages("p3")
    assert "result" not in msgs[0]


# --------------------------------------------------------------------------
# update_panel_code
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_panel_code_and_params(temp_db):
    await history.create_panel("s4", "p4")
    # Initially no code.
    panels = await history.list_panels("s4")
    assert panels[0]["current_code"] is None

    await history.update_panel_code("p4", "result = box(1,2,3)", params={"w": 10})
    panels = await history.list_panels("s4")
    assert panels[0]["current_code"] == "result = box(1,2,3)"

    # Overwrite with None code (allowed by signature).
    await history.update_panel_code("p4", None)
    panels = await history.list_panels("s4")
    assert panels[0]["current_code"] is None


# --------------------------------------------------------------------------
# list_sessions ordering by updated_at; touch_session
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_sessions_ordered_by_updated_at_desc(temp_db, monkeypatch):
    # Create three sessions, then touch them in a deliberate order.
    await history.create_session("a")
    await history.create_session("b")
    await history.create_session("c")

    # touch_session stamps updated_at with _now(). Ensure monotonic timestamps by
    # forcing a distinct ISO time per touch.
    from itertools import count
    base = count()

    def fake_now():
        # strictly increasing ISO-8601 timestamps
        n = next(base)
        return f"2030-01-01T00:00:{n:02d}+00:00"

    # Re-touch b, then a, then c -> expected order c, a, b (most recent first).
    monkeypatch.setattr(history, "_now", fake_now)
    await history.touch_session("b")
    await history.touch_session("a")
    await history.touch_session("c")

    ordered = [s["id"] for s in await history.list_sessions()]
    # c touched last -> first; then a; then b.
    assert ordered[:3] == ["c", "a", "b"]


@pytest.mark.asyncio
async def test_touch_session_updates_timestamp(temp_db, monkeypatch):
    sess = await history.create_session("touch-me")
    original = sess["updated_at"]

    monkeypatch.setattr(history, "_now", lambda: "2099-12-31T23:59:59+00:00")
    await history.touch_session("touch-me")

    rows = await history.list_sessions()
    row = next(s for s in rows if s["id"] == "touch-me")
    assert row["updated_at"] == "2099-12-31T23:59:59+00:00"
    assert row["updated_at"] != original


# --------------------------------------------------------------------------
# delete_session cascade
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_session_cascades_panels_and_messages(temp_db):
    """delete_session() must purge child panels + messages (privacy + storage).

    Regression guard for the FK-cascade fix: get_db() now sets PRAGMA foreign_keys=ON,
    so the ON DELETE CASCADE declared on panels.session_id / messages.panel_id fires.
    Before the fix, SQLite ignored the FK and orphaned every panel + message on disk.
    """
    await history.create_panel("doomed", "dp1")
    await history.create_panel("doomed", "dp2")
    await history.save_message("dp1", "user", "hi")
    await history.save_message("dp2", "assistant", "bye", result={"k": "v"})

    # Sanity: data present.
    assert await history.list_panels("doomed")
    assert await history.get_messages("dp1")
    assert await history.get_messages("dp2")

    await history.delete_session("doomed")

    # Session AND all children are gone.
    assert all(s["id"] != "doomed" for s in await history.list_sessions())
    assert await history.list_panels("doomed") == []
    assert await history.get_messages("dp1") == []
    assert await history.get_messages("dp2") == []


# --------------------------------------------------------------------------
# Feedback: multiple rows per request_id
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_feedback_multiple_rows(temp_db):
    r1 = await history.save_feedback("req-1", rating="up", printed="not_yet", note="first")
    assert r1["request_id"] == "req-1"
    assert r1["rating"] == "up"
    assert r1["printed"] == "not_yet"

    # Second feedback for the same request (e.g. user later actually printed it).
    await history.save_feedback("req-1", rating="up", printed="yes", note="printed fine")
    # A different request_id should not bleed in.
    await history.save_feedback("req-2", rating="down")

    rows = await history.get_feedback("req-1")
    assert len(rows) == 2
    # ORDER BY id -> insertion order preserved.
    assert [r["note"] for r in rows] == ["first", "printed fine"]
    assert [r["printed"] for r in rows] == ["not_yet", "yes"]

    other = await history.get_feedback("req-2")
    assert len(other) == 1
    assert other[0]["rating"] == "down"
    assert other[0]["printed"] is None

    # Unknown request -> empty list, not error.
    assert await history.get_feedback("nope") == []


# --------------------------------------------------------------------------
# WAL mode + sequential concurrent-ish writes don't corrupt
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wal_mode_enabled(temp_db):
    db = await history.get_db()
    cur = await db.execute("PRAGMA journal_mode")
    row = await cur.fetchone()
    assert row[0].lower() == "wal"


@pytest.mark.asyncio
async def test_many_sequential_writes_no_corruption(temp_db):
    await history.create_panel("bulk", "bp")
    # Fire a batch of awaited writes; the single shared connection serializes them.
    for i in range(50):
        await history.save_message("bp", "user", f"msg {i}", result={"i": i})

    msgs = await history.get_messages("bp")
    assert len(msgs) == 50
    # Ordering preserved by autoincrement id.
    assert [m["content"] for m in msgs] == [f"msg {i}" for i in range(50)]
    assert [m["result"]["i"] for m in msgs] == list(range(50))


@pytest.mark.asyncio
async def test_gathered_writes_share_connection(temp_db):
    # asyncio.gather of writes on one event loop / one connection: aiosqlite
    # serializes on its worker thread, so this must not corrupt or drop rows.
    await history.create_panel("g", "gp")
    await asyncio.gather(*[
        history.save_message("gp", "user", f"c{i}") for i in range(20)
    ])
    msgs = await history.get_messages("gp")
    assert len(msgs) == 20
    # All distinct contents present (ordering across gather is not guaranteed).
    assert {m["content"] for m in msgs} == {f"c{i}" for i in range(20)}


# --------------------------------------------------------------------------
# LRU in-memory session cache in websocket.py
# --------------------------------------------------------------------------

@pytest.fixture
def clean_ws_sessions(monkeypatch):
    """Reset the module-level OrderedDict before/after; keep _MAX_SESSIONS patchable."""
    ws.sessions.clear()
    yield
    ws.sessions.clear()


def test_get_context_returns_same_context_for_same_keys(clean_ws_sessions):
    c1 = ws._get_context("sess", "panel")
    c2 = ws._get_context("sess", "panel")
    assert c1 is c2
    assert c1.session_id == "sess/panel"

    # Different panel -> different context object.
    c3 = ws._get_context("sess", "other")
    assert c3 is not c1
    assert c3.session_id == "sess/other"


def test_lru_evicts_oldest_session(clean_ws_sessions, monkeypatch):
    monkeypatch.setattr(ws, "_MAX_SESSIONS", 3)

    ws._get_context("s0", "p")
    ws._get_context("s1", "p")
    ws._get_context("s2", "p")
    assert list(ws.sessions.keys()) == ["s0", "s1", "s2"]

    # Adding a 4th over capacity evicts the least-recently-used (s0).
    ws._get_context("s3", "p")
    assert "s0" not in ws.sessions
    assert list(ws.sessions.keys()) == ["s1", "s2", "s3"]


def test_lru_access_marks_recently_used(clean_ws_sessions, monkeypatch):
    monkeypatch.setattr(ws, "_MAX_SESSIONS", 3)

    ws._get_context("s0", "p")
    ws._get_context("s1", "p")
    ws._get_context("s2", "p")

    # Re-access s0 -> moves it to the most-recent end.
    ws._get_context("s0", "p")
    assert list(ws.sessions.keys()) == ["s1", "s2", "s0"]

    # Now adding s3 should evict s1 (now the oldest), not s0.
    ws._get_context("s3", "p")
    assert "s1" not in ws.sessions
    assert "s0" in ws.sessions
    assert list(ws.sessions.keys()) == ["s2", "s0", "s3"]
