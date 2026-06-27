from fastapi import APIRouter, Depends, HTTPException

from app.api.auth import get_current_user
from app.storage.history import (
    list_sessions,
    list_panels,
    get_messages,
    delete_session,
    panel_belongs_to_user,
    session_belongs_to_user,
)

router = APIRouter(prefix="/api/history", tags=["history"])


@router.get("/sessions")
async def api_list_sessions(user=Depends(get_current_user)):
    return await list_sessions(user["id"])


@router.get("/sessions/{session_id}/panels")
async def api_list_panels(session_id: str, user=Depends(get_current_user)):
    if not await session_belongs_to_user(session_id, user["id"]):
        raise HTTPException(status_code=404, detail="Session not found")
    return await list_panels(session_id)


@router.get("/panels/{panel_id}/messages")
async def api_get_messages(panel_id: str, user=Depends(get_current_user)):
    if not await panel_belongs_to_user(panel_id, user["id"]):
        raise HTTPException(status_code=404, detail="Panel not found")
    return await get_messages(panel_id)


@router.delete("/sessions/{session_id}")
async def api_delete_session(session_id: str, user=Depends(get_current_user)):
    await delete_session(session_id, user["id"])
    return {"ok": True}
