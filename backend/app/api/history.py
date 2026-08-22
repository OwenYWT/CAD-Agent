from fastapi import APIRouter, Depends, HTTPException

from app.api.auth import get_optional_user
from app.storage.history import (
    list_sessions,
    list_panels,
    get_messages,
    delete_session,
    panel_belongs_to_user,
    session_belongs_to_user,
    list_model_snapshots,
    get_model_snapshot,
    diff_model_snapshots,
    snapshot_belongs_to_user,
    restore_model_snapshot,
)

router = APIRouter(prefix="/api/history", tags=["history"])


def _uid(user) -> str | None:
    """user is the logged-in user dict, or None in local-dev auth-off mode."""
    return user["id"] if user else None


@router.get("/sessions")
async def api_list_sessions(user=Depends(get_optional_user)):
    return await list_sessions(_uid(user))


@router.get("/sessions/{session_id}/panels")
async def api_list_panels(session_id: str, user=Depends(get_optional_user)):
    if not await session_belongs_to_user(session_id, _uid(user)):
        raise HTTPException(status_code=404, detail="Session not found")
    return await list_panels(session_id)


@router.get("/panels/{panel_id}/messages")
async def api_get_messages(panel_id: str, user=Depends(get_optional_user)):
    if not await panel_belongs_to_user(panel_id, _uid(user)):
        raise HTTPException(status_code=404, detail="Panel not found")
    return await get_messages(panel_id)




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
    if not await snapshot_belongs_to_user(snapshot_id, _uid(user)):
        raise HTTPException(status_code=404, detail="Snapshot not found")
    snapshot = await restore_model_snapshot(snapshot_id, _uid(user))
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return snapshot


@router.get("/snapshots/{from_snapshot_id}/diff/{to_snapshot_id}")
async def api_diff_model_snapshots(from_snapshot_id: str, to_snapshot_id: str, user=Depends(get_optional_user)):
    diff = await diff_model_snapshots(from_snapshot_id, to_snapshot_id, _uid(user))
    if diff is None:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return diff


@router.delete("/sessions/{session_id}")
async def api_delete_session(session_id: str, user=Depends(get_optional_user)):
    await delete_session(session_id, _uid(user))
    return {"ok": True}
