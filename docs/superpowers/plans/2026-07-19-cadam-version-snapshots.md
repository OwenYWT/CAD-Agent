# CADAM-Style Version Snapshots Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add durable panel-level model snapshots so users can restore previous successful CAD versions without another LLM call.

**Architecture:** Build on existing SQLite history storage, FastAPI history routes, WebSocket result persistence, and Zustand session state. Store full result payloads in `model_snapshots`, expose list/detail/restore endpoints, create snapshots after successful model-producing operations, and add a compact frontend version history panel. Keep the UI linear while storing `parent_snapshot_id` for future branching.

**Tech Stack:** Python 3.11, FastAPI, Pydantic, aiosqlite, pytest, React 19, TypeScript, Zustand, Tailwind CSS, Vite.

---

## File Structure

- `backend/app/models/schemas.py`: add optional `snapshot_id` and `version` fields to response models.
- `backend/app/storage/history.py`: create `model_snapshots` table and storage functions.
- `backend/app/api/history.py`: add snapshot list/detail/restore endpoints with ownership checks.
- `backend/app/api/websocket.py`: create snapshots after successful generation, part modification, and direct execution.
- `backend/tests/test_model_snapshots.py`: storage/API tests for snapshots.
- `backend/tests/test_deploy_websocket.py`: WebSocket snapshot metadata tests.
- `frontend/src/types/index.ts`: snapshot summary/detail types and result snapshot fields.
- `frontend/src/stores/sessionStore.ts`: add `restorePanelResult` action.
- `frontend/src/components/VersionHistoryPanel.tsx`: snapshot list and restore UI.
- `frontend/src/App.tsx`: render version panel in the analysis tab.
- `docs/case-study-adaptations/CAD-Agent-adaptation-report.md`: Adaptation 5 record.

---

### Task 1: Snapshot Response Contract

**Files:**
- Modify: `backend/app/models/schemas.py`
- Test: `backend/tests/test_model_snapshots.py`

- [x] **Step 1: Create failing response serialization test**

Create `backend/tests/test_model_snapshots.py`:

```python
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models.schemas import GenerateResponse, GenerationResult
from app.storage import history


@pytest.fixture(autouse=True)
async def isolated_history_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "history_db_path", str(tmp_path / "history.db"))
    await history.close_db()
    yield
    await history.close_db()


def test_generate_response_serializes_snapshot_metadata():
    response = GenerateResponse(
        request_id="req-1",
        success=True,
        snapshot_id="snap-1",
        version=3,
    )
    dumped = response.model_dump()
    assert dumped["snapshot_id"] == "snap-1"
    assert dumped["version"] == 3


def test_generation_result_serializes_snapshot_metadata():
    result = GenerationResult(
        request_id="req-1",
        success=True,
        snapshot_id="snap-1",
        version=3,
    )
    dumped = result.model_dump()
    assert dumped["snapshot_id"] == "snap-1"
    assert dumped["version"] == 3
```

- [x] **Step 2: Run tests and verify red**

Run:

```powershell
python -m pytest backend\tests\test_model_snapshots.py::test_generate_response_serializes_snapshot_metadata backend\tests\test_model_snapshots.py::test_generation_result_serializes_snapshot_metadata -q
```

Expected: fail because `snapshot_id` and `version` are not model fields.

- [x] **Step 3: Add response fields**

In `backend/app/models/schemas.py`, add to both `GenerationResult` and `GenerateResponse` near `request_id`:

```python
snapshot_id: str | None = None
version: int | None = None
```

- [x] **Step 4: Run tests and verify green**

Run the same command from Step 2. Expected: pass.

---

### Task 2: Snapshot Storage Create/List

**Files:**
- Modify: `backend/app/storage/history.py`
- Test: `backend/tests/test_model_snapshots.py`

- [x] **Step 1: Add failing storage test**

Append to `backend/tests/test_model_snapshots.py`:

```python
@pytest.mark.asyncio
async def test_create_and_list_model_snapshots_versions_per_panel():
    await history.create_session("session-1", user_id="user-1")
    await history.create_panel("session-1", "panel-1", user_id="user-1")
    result = {
        "success": True,
        "request_id": "req-1",
        "code": "result = None",
        "files": {"stl": "/api/files/req-1/result.stl"},
        "inspect_report": {"verdict": "pass", "available_exports": ["stl"]},
    }
    first = await history.create_model_snapshot("panel-1", result, source="generation", prompt="make a box")
    second = await history.create_model_snapshot(
        "panel-1",
        result,
        source="execute_code",
        prompt="parameter edit",
        parent_snapshot_id=first["id"],
    )
    snapshots = await history.list_model_snapshots("panel-1")
    assert first["version"] == 1
    assert second["version"] == 2
    assert second["parent_snapshot_id"] == first["id"]
    assert [s["version"] for s in snapshots] == [2, 1]
    assert snapshots[0]["status"] == "pass"
    assert snapshots[0]["available_exports"] == ["stl"]
```

- [x] **Step 2: Run test and verify red**

```powershell
python -m pytest backend\tests\test_model_snapshots.py::test_create_and_list_model_snapshots_versions_per_panel -q
```

Expected: fail because snapshot storage functions do not exist.

- [x] **Step 3: Add table and imports**

In `backend/app/storage/history.py`, add `import uuid`. Add this SQL to `_init_tables` after `messages`:

```sql
CREATE TABLE IF NOT EXISTS model_snapshots (
    id TEXT PRIMARY KEY,
    panel_id TEXT NOT NULL REFERENCES panels(id) ON DELETE CASCADE,
    parent_snapshot_id TEXT REFERENCES model_snapshots(id) ON DELETE SET NULL,
    version INTEGER NOT NULL,
    source TEXT NOT NULL,
    prompt TEXT NOT NULL DEFAULT '',
    code TEXT NOT NULL,
    result TEXT NOT NULL,
    files TEXT NOT NULL DEFAULT '{}',
    params TEXT,
    parameters TEXT,
    validation TEXT,
    inspect_report TEXT,
    repair_history TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model_snapshots_panel_version ON model_snapshots(panel_id, version);
CREATE INDEX IF NOT EXISTS idx_model_snapshots_panel_created ON model_snapshots(panel_id, created_at);
```

- [x] **Step 4: Add helpers and create/list functions**

In `backend/app/storage/history.py`, add below `_now()`:

```python
def _json_dump(value) -> str | None:
    if value is None:
        return None
    return json.dumps(value)


def _json_load(value, default=None):
    if value is None:
        return default
    return json.loads(value)


def _snapshot_status(result: dict) -> str:
    if not result.get("success"):
        return "fail"
    verdict = (result.get("inspect_report") or {}).get("verdict")
    return verdict if verdict in {"pass", "warn", "fail"} else "unknown"


def _snapshot_from_row(row) -> dict:
    result = _json_load(row["result"], {})
    inspect_report = _json_load(row["inspect_report"], None)
    return {
        "id": row["id"],
        "panel_id": row["panel_id"],
        "parent_snapshot_id": row["parent_snapshot_id"],
        "version": row["version"],
        "source": row["source"],
        "prompt": row["prompt"],
        "code": row["code"],
        "result": result,
        "files": _json_load(row["files"], {}),
        "params": _json_load(row["params"], None),
        "parameters": _json_load(row["parameters"], None),
        "validation": _json_load(row["validation"], None),
        "inspect_report": inspect_report,
        "repair_history": _json_load(row["repair_history"], []),
        "status": row["status"],
        "created_at": row["created_at"],
        "available_exports": (inspect_report or {}).get("available_exports", []),
        "inspect_verdict": (inspect_report or {}).get("verdict"),
    }
```

Add after `get_messages()`:

```python
async def create_model_snapshot(panel_id: str, result: dict, source: str, prompt: str = "", parent_snapshot_id: str | None = None) -> dict:
    db = await get_db()
    cursor = await db.execute("SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM model_snapshots WHERE panel_id = ?", (panel_id,))
    row = await cursor.fetchone()
    version = int(row["next_version"])
    snapshot_id = str(uuid.uuid4())
    now = _now()
    status = _snapshot_status(result)
    code = result.get("code") or ""
    files = result.get("files") or {}
    params = result.get("params")
    parameters = result.get("parameters")
    validation = result.get("validation")
    inspect_report = result.get("inspect_report")
    repair_history = result.get("repair_history") or []
    await db.execute(
        """
        INSERT INTO model_snapshots (
            id, panel_id, parent_snapshot_id, version, source, prompt, code,
            result, files, params, parameters, validation, inspect_report,
            repair_history, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id, panel_id, parent_snapshot_id, version, source, prompt, code,
            json.dumps(result), json.dumps(files), _json_dump(params),
            _json_dump(parameters), _json_dump(validation), _json_dump(inspect_report),
            json.dumps(repair_history), status, now,
        ),
    )
    await db.commit()
    snapshot = await get_model_snapshot(snapshot_id)
    return snapshot


async def list_model_snapshots(panel_id: str) -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM model_snapshots WHERE panel_id = ? ORDER BY version DESC", (panel_id,))
    return [_snapshot_from_row(row) for row in rows]


async def get_model_snapshot(snapshot_id: str) -> dict | None:
    db = await get_db()
    cursor = await db.execute("SELECT * FROM model_snapshots WHERE id = ?", (snapshot_id,))
    row = await cursor.fetchone()
    return _snapshot_from_row(row) if row else None
```

- [x] **Step 5: Run storage test and verify green**

Run the command from Step 2. Expected: pass.

---

### Task 3: Snapshot Detail, Restore, And Cascade

**Files:**
- Modify: `backend/app/storage/history.py`
- Test: `backend/tests/test_model_snapshots.py`

- [x] **Step 1: Add failing restore/cascade tests**

Append to `backend/tests/test_model_snapshots.py`:

```python
@pytest.mark.asyncio
async def test_restore_model_snapshot_updates_panel_current_code():
    await history.create_session("session-1", user_id="user-1")
    await history.create_panel("session-1", "panel-1", user_id="user-1")
    snapshot = await history.create_model_snapshot(
        "panel-1",
        {
            "success": True,
            "request_id": "req-1",
            "code": "result = restored",
            "params": {"width": {"value": 10, "comment": "mm"}},
            "inspect_report": {"verdict": "warn"},
        },
        source="generation",
        prompt="make restored model",
    )
    restored = await history.restore_model_snapshot(snapshot["id"], user_id="user-1")
    panels = await history.list_panels("session-1")
    assert restored["code"] == "result = restored"
    assert restored["result"]["code"] == "result = restored"
    assert restored["status"] == "warn"
    assert panels[0]["current_code"] == "result = restored"


@pytest.mark.asyncio
async def test_delete_session_cascades_model_snapshots():
    await history.create_session("session-1", user_id="user-1")
    await history.create_panel("session-1", "panel-1", user_id="user-1")
    snapshot = await history.create_model_snapshot(
        "panel-1",
        {"success": True, "request_id": "req-1", "code": "result = x"},
        source="generation",
    )
    await history.delete_session("session-1", user_id="user-1")
    assert await history.get_model_snapshot(snapshot["id"]) is None
```

- [x] **Step 2: Run tests and verify red**

```powershell
python -m pytest backend\tests\test_model_snapshots.py::test_restore_model_snapshot_updates_panel_current_code backend\tests\test_model_snapshots.py::test_delete_session_cascades_model_snapshots -q
```

Expected: fail because detail and restore functions do not exist.

- [x] **Step 3: Add ownership and restore functions**

Add to `backend/app/storage/history.py` after `get_model_snapshot()`:

```python
async def snapshot_belongs_to_user(snapshot_id: str, user_id: str | None) -> bool:
    if not user_id:
        return True
    db = await get_db()
    cursor = await db.execute(
        """
        SELECT s.user_id
        FROM model_snapshots ms
        JOIN panels p ON ms.panel_id = p.id
        JOIN sessions s ON p.session_id = s.id
        WHERE ms.id = ?
        """,
        (snapshot_id,),
    )
    row = await cursor.fetchone()
    return bool(row and row["user_id"] == user_id)


async def restore_model_snapshot(snapshot_id: str, user_id: str | None = None) -> dict | None:
    if not await snapshot_belongs_to_user(snapshot_id, user_id):
        return None
    snapshot = await get_model_snapshot(snapshot_id)
    if snapshot is None:
        return None
    await update_panel_code(snapshot["panel_id"], snapshot["code"], snapshot.get("params"))
    return snapshot
```

- [x] **Step 4: Run tests and verify green**

Run the command from Step 2. Expected: pass.

---

### Task 4: Snapshot History API

**Files:**
- Modify: `backend/app/api/history.py`
- Test: `backend/tests/test_model_snapshots.py`

- [x] **Step 1: Add failing API test**

Append to `backend/tests/test_model_snapshots.py`:

```python
def test_snapshot_api_list_detail_and_restore(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "history_db_path", str(tmp_path / "history.db"))
    asyncio.run(history.close_db())
    asyncio.run(history.create_session("session-1", user_id=None))
    asyncio.run(history.create_panel("session-1", "panel-1", user_id=None))
    snapshot = asyncio.run(history.create_model_snapshot(
        "panel-1",
        {
            "success": True,
            "request_id": "req-1",
            "code": "result = api",
            "files": {"stl": "/api/files/req-1/result.stl"},
            "inspect_report": {"verdict": "pass", "available_exports": ["stl"]},
        },
        source="generation",
        prompt="make api model",
    ))
    client = TestClient(app)
    listed = client.get("/api/history/panels/panel-1/snapshots")
    detail = client.get(f"/api/history/snapshots/{snapshot['id']}")
    restored = client.post(f"/api/history/snapshots/{snapshot['id']}/restore")
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == snapshot["id"]
    assert listed.json()[0]["available_exports"] == ["stl"]
    assert detail.status_code == 200
    assert detail.json()["result"]["code"] == "result = api"
    assert restored.status_code == 200
    assert restored.json()["code"] == "result = api"
```

- [x] **Step 2: Run API test and verify red**

```powershell
python -m pytest backend\tests\test_model_snapshots.py::test_snapshot_api_list_detail_and_restore -q
```

Expected: fail with 404 because routes do not exist.

- [x] **Step 3: Add imports and routes**

In `backend/app/api/history.py`, import:

```python
list_model_snapshots,
get_model_snapshot,
restore_model_snapshot,
snapshot_belongs_to_user,
```

Add after `api_get_messages()`:

```python
@router.get("/panels/{panel_id}/snapshots")
async def api_list_model_snapshots(panel_id: str, user=Depends(get_optional_user)):
    if not await panel_belongs_to_user(panel_id, _uid(user)):
        raise HTTPException(status_code=404, detail="Panel not found")
    return await list_model_snapshots(panel_id)


@router.get("/snapshots/{snapshot_id}")
async def api_get_model_snapshot(snapshot_id: str, user=Depends(get_optional_user)):
    if not await snapshot_belongs_to_user(snapshot_id, _uid(user)):
        raise HTTPException(status_code=404, detail="Snapshot not found")
    snapshot = await get_model_snapshot(snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return snapshot


@router.post("/snapshots/{snapshot_id}/restore")
async def api_restore_model_snapshot(snapshot_id: str, user=Depends(get_optional_user)):
    snapshot = await restore_model_snapshot(snapshot_id, _uid(user))
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return snapshot
```

- [x] **Step 4: Run API test and verify green**

Run the command from Step 2. Expected: pass.

---

### Task 5: WebSocket Snapshot Creation

**Files:**
- Modify: `backend/app/api/websocket.py`
- Test: `backend/tests/test_deploy_websocket.py`

- [x] **Step 1: Add failing WebSocket assertions**

In `backend/tests/test_deploy_websocket.py`, extend `test_execute_code_returns_generation_result` after inspect assertions:

```python
    assert msg["data"]["snapshot_id"] is not None
    assert msg["data"]["version"] == 1
```

- [x] **Step 2: Run WebSocket test and verify red**

```powershell
python -m pytest backend\tests\test_deploy_websocket.py::test_execute_code_returns_generation_result -q
```

Expected: fail because WebSocket does not create snapshots.

- [x] **Step 3: Create snapshots in WebSocket branches**

In `backend/app/api/websocket.py`, after each `result_data` dictionary is built and before saving/sending it, add the matching block.

For `user_message`:

```python
                if result.success and result.code:
                    snapshot = await history.create_model_snapshot(
                        panel_id,
                        result_data,
                        source="generation",
                        prompt=text,
                    )
                    result_data["snapshot_id"] = snapshot["id"]
                    result_data["version"] = snapshot["version"]
```

For `modify_part`:

```python
                if result.success and result.code:
                    snapshot = await history.create_model_snapshot(
                        panel_id,
                        result_data,
                        source="modify_part",
                        prompt=f"modify part {part_name}: {instruction}",
                    )
                    result_data["snapshot_id"] = snapshot["id"]
                    result_data["version"] = snapshot["version"]
```

For `execute_code`:

```python
                if response.success and response.code:
                    snapshot = await history.create_model_snapshot(
                        panel_id,
                        result_data,
                        source="execute_code",
                        prompt="manual code execution",
                    )
                    result_data["snapshot_id"] = snapshot["id"]
                    result_data["version"] = snapshot["version"]
```

The `user_message` and `modify_part` blocks must run before `history.save_message(..., result=result_data)` so stored assistant messages include the snapshot id and version.

- [x] **Step 4: Run WebSocket tests and verify green**

```powershell
python -m pytest backend\tests\test_deploy_websocket.py -q
```

Expected: pass.

---

### Task 6: Frontend Types And Store Restore Action

**Files:**
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/stores/sessionStore.ts`

- [x] **Step 1: Add snapshot types and result fields**

In `frontend/src/types/index.ts`, add `snapshot_id?: string;` and `version?: number;` to `GenerationResult`. Add after `GenerationResult`:

```ts
export interface ModelSnapshotSummary {
  id: string;
  panel_id: string;
  parent_snapshot_id?: string | null;
  version: number;
  source: "generation" | "execute_code" | "parameter_edit" | "modify_part" | string;
  prompt: string;
  status: "pass" | "warn" | "fail" | "unknown";
  created_at: string;
  inspect_verdict?: "pass" | "warn" | "fail" | null;
  available_exports?: string[];
}

export interface ModelSnapshotDetail extends ModelSnapshotSummary {
  code: string;
  result: GenerationResult;
}
```

- [x] **Step 2: Add store action type and implementation**

In `frontend/src/stores/sessionStore.ts`, add the interface action:

```ts
restorePanelResult: (panelId: string, result: GenerationResult, code: string) => void;
```

Add to the Zustand store object:

```ts
  restorePanelResult: (panelId, result, code) =>
    set((state) => ({
      panels: updatePanel(state.panels, panelId, (panel) => {
        const restoredResult = { ...result, code };
        const restoreMessage: ChatMessage = {
          role: "assistant",
          content: `Restored version ${result.version ?? ""}`.trim(),
          result: restoredResult,
        };
        return {
          result: restoredResult,
          isGenerating: false,
          currentStep: null,
          multiStepProgress: null,
          stepHistory: [],
          generationStartTime: null,
          messages: [...panel.messages, restoreMessage],
        };
      }),
    })),
```

- [x] **Step 3: Run frontend build**

```powershell
cd frontend
npm.cmd run build
```

Expected: pass.

---

### Task 7: Frontend Version History Panel

**Files:**
- Create: `frontend/src/components/VersionHistoryPanel.tsx`
- Modify: `frontend/src/App.tsx`

- [x] **Step 1: Create `VersionHistoryPanel.tsx`**

Create `frontend/src/components/VersionHistoryPanel.tsx`:

```tsx
import { useCallback, useEffect, useState } from "react";
import { API_BASE } from "../config";
import { authFetch } from "../lib/authFetch";
import type { ModelSnapshotDetail, ModelSnapshotSummary } from "../types";

interface VersionHistoryPanelProps {
  panelId: string;
  activeSnapshotId?: string | null;
  refreshKey?: string | number | null;
  onRestore: (snapshot: ModelSnapshotDetail) => void;
}

const STATUS_CLASS: Record<string, string> = {
  pass: "border-emerald-200 bg-emerald-50 text-emerald-700",
  warn: "border-amber-200 bg-amber-50 text-amber-700",
  fail: "border-red-200 bg-red-50 text-red-700",
  unknown: "border-gray-200 bg-gray-50 text-gray-600",
};

function formatTime(iso: string) {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export default function VersionHistoryPanel({ panelId, activeSnapshotId, refreshKey, onRestore }: VersionHistoryPanelProps) {
  const [snapshots, setSnapshots] = useState<ModelSnapshotSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [restoringId, setRestoringId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadSnapshots = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await authFetch(`${API_BASE}/api/history/panels/${panelId}/snapshots`);
      if (!response.ok) throw new Error("Failed to load versions");
      setSnapshots(await response.json());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load versions");
    } finally {
      setLoading(false);
    }
  }, [panelId]);

  useEffect(() => {
    loadSnapshots();
  }, [loadSnapshots, refreshKey]);

  const restoreSnapshot = async (snapshotId: string) => {
    setRestoringId(snapshotId);
    setError(null);
    try {
      const response = await authFetch(`${API_BASE}/api/history/snapshots/${snapshotId}/restore`, { method: "POST" });
      if (!response.ok) throw new Error("Failed to restore version");
      onRestore(await response.json());
      await loadSnapshots();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to restore version");
    } finally {
      setRestoringId(null);
    }
  };

  return (
    <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold text-gray-900">Version History</h3>
          <p className="text-xs text-gray-500">Restore a CAD checkpoint without another LLM call.</p>
        </div>
        <button className="text-xs text-indigo-600 hover:text-indigo-700" onClick={loadSnapshots} disabled={loading}>Refresh</button>
      </div>
      {error && <div className="mb-2 rounded bg-red-50 px-2 py-1 text-xs text-red-700">{error}</div>}
      {loading && <div className="text-xs text-gray-400">Loading versions...</div>}
      {!loading && snapshots.length === 0 && <div className="text-xs text-gray-400">No versions saved yet.</div>}
      <div className="space-y-2">
        {snapshots.map((snapshot) => {
          const isActive = snapshot.id === activeSnapshotId;
          return (
            <div key={snapshot.id} className={`rounded-lg border p-3 ${isActive ? "border-indigo-200 bg-indigo-50" : "border-gray-100 bg-gray-50"}`}>
              <div className="flex items-center justify-between gap-2">
                <div className="min-w-0">
                  <div className="text-xs font-semibold text-gray-800">v{snapshot.version} · {snapshot.source}</div>
                  <div className="truncate text-[11px] text-gray-500">{snapshot.prompt || "No prompt recorded"}</div>
                </div>
                <span className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${STATUS_CLASS[snapshot.status] || STATUS_CLASS.unknown}`}>{snapshot.status}</span>
              </div>
              <div className="mt-2 flex items-center justify-between gap-2 text-[11px] text-gray-500">
                <span>{formatTime(snapshot.created_at)}</span>
                <button className="rounded bg-white px-2 py-1 text-indigo-600 shadow-sm hover:text-indigo-700 disabled:text-gray-400" disabled={restoringId === snapshot.id || isActive} onClick={() => restoreSnapshot(snapshot.id)}>
                  {isActive ? "Current" : restoringId === snapshot.id ? "Restoring..." : "Restore"}
                </button>
              </div>
              {snapshot.available_exports && snapshot.available_exports.length > 0 && <div className="mt-2 text-[11px] text-blue-700">Exports: {snapshot.available_exports.join(", ")}</div>}
            </div>
          );
        })}
      </div>
    </div>
  );
}
```

- [x] **Step 2: Wire panel into `App.tsx`**

In `frontend/src/App.tsx`, import:

```ts
import VersionHistoryPanel from "./components/VersionHistoryPanel";
import type { Annotation3D, ModelSnapshotDetail } from "./types";
```

Add store selector in `MainApp`:

```ts
const restorePanelResult = useSessionStore((s) => s.restorePanelResult);
```

Add callback in `MainApp`:

```ts
const handleRestoreSnapshot = (snapshot: ModelSnapshotDetail) => {
  restorePanelResult(activePanelId, snapshot.result, snapshot.code);
  restoreContext(activePanelId, snapshot.code);
};
```

Render after `InspectReportPanel` in the analysis tab:

```tsx
<VersionHistoryPanel
  panelId={activePanelId}
  activeSnapshotId={result?.snapshot_id || null}
  refreshKey={result?.snapshot_id || result?.request_id || null}
  onRestore={handleRestoreSnapshot}
/>
```

- [x] **Step 3: Run frontend build**

```powershell
cd frontend
npm.cmd run build
```

Expected: pass. Existing Vite bundle-size warnings are acceptable.

---

### Task 8: Report And Final Verification

**Files:**
- Modify: `docs/case-study-adaptations/CAD-Agent-adaptation-report.md`
- Modify: `docs/superpowers/plans/2026-07-19-cadam-version-snapshots.md`

- [x] **Step 1: Append Adaptation 5 report section**

Append to `docs/case-study-adaptations/CAD-Agent-adaptation-report.md`:

```markdown
## Adaptation 5: CADAM-Style Version Snapshots

### Source
- Project: CADAM
- Borrowed idea: durable conversation/message state makes prior creative outputs recoverable instead of overwriting the latest model state.
- Local reference files studied:
  - `../CADAM/src/contexts/ConversationContext.tsx`
  - `../CADAM/src/services/messageService.ts`
  - `../CADAM/src/services/conversationService.ts`
  - `../CADAM/src/components/Sidebar.tsx`
  - `../CADAM/src/components/history/ConversationCard.tsx`

### CAD-Agent Implementation
- Added panel-level `model_snapshots` storage with monotonically increasing versions.
- Saved successful model-producing WebSocket operations as snapshots.
- Added history endpoints for listing, reading, and restoring snapshots.
- Added a frontend version history panel with status badges and one-click restore.
- Preserved a `parent_snapshot_id` field for future branching while keeping the current UI linear.

### Verification
- `python -m pytest backend\tests\test_model_snapshots.py backend\tests\test_deploy_websocket.py backend\tests\test_deploy_persistence.py -q` passed.
- `cd frontend && npm.cmd run build` passed.

### Decision
- Implemented recommended version B: structured model snapshots and restore, without a full branch tree or geometry diff UI.
```

- [x] **Step 2: Run backend verification**

```powershell
python -m pytest backend\tests\test_model_snapshots.py backend\tests\test_deploy_websocket.py backend\tests\test_deploy_persistence.py -q
```

Expected: pass.

- [x] **Step 3: Run frontend verification**

```powershell
cd frontend
npm.cmd run build
```

Expected: pass. Bundle-size warnings are acceptable when the command exits with code 0.

- [x] **Step 4: Mark plan complete**

After all verification passes, replace every task checkbox in `docs/superpowers/plans/2026-07-19-cadam-version-snapshots.md` from `[ ]` to `[x]`.

---

## Execution Notes

- Do not run `git commit` unless the user explicitly requests it.
- Keep the UI linear in this phase; do not build a branch tree.
- Store snapshots only for successful responses with non-empty code.
- Treat restore as state recovery, not regeneration; it must not call the LLM.
- Keep existing sessions, panels, messages, `current_code`, `validation`, `inspect_report`, and download behavior backward compatible.
- If `backend/tests/test_codegen.py::TestLazyClientInit::test_client_raises_without_api_key` fails during a broad run, treat it as unrelated to this adaptation because it checks an older provider-specific key message.
