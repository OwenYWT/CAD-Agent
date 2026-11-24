from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

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
)

router = APIRouter(prefix="/api/history", tags=["history"])


def _uid(user) -> str | None:
    """user is the logged-in user dict, or None in local-dev auth-off mode."""
    return user["id"] if user else None


@router.get("/sessions")
async def api_list_sessions(
    user=Depends(get_optional_user), limit: int = Query(50, ge=1, le=100),
    before_updated_at: str | None = Query(None, max_length=40),
    before_id: str | None = Query(None, min_length=1, max_length=500),
):
    if (before_updated_at is None) != (before_id is None):
        raise HTTPException(422, "请同时提供上一页的更新时间和项目 ID")
    if before_updated_at is not None:
        try:
            datetime.fromisoformat(before_updated_at)
        except ValueError as exc:
            raise HTTPException(422, "分页时间无效") from exc
    cursor = (before_updated_at, before_id) if before_updated_at is not None and before_id is not None else None
    return await list_sessions(_uid(user), limit=limit, before=cursor)


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
    raise HTTPException(
        status_code=410,
        detail=(
            "旧版本恢复写接口已停用。请读取该版本并通过持久化 "
            "MCAD 执行链路创建可审查的 Change Set。"
        ),
    )


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
