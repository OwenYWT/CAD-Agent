from fastapi import APIRouter, Depends, HTTPException

from app.api.auth import get_optional_user
from app.storage.history import (
    list_sessions,
    list_panels,
    get_messages,
    delete_session,
    panel_belongs_to_user,
    session_belongs_to_user,
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


@router.delete("/sessions/{session_id}")
async def api_delete_session(session_id: str, user=Depends(get_optional_user)):
    await delete_session(session_id, _uid(user))
    return {"ok": True}
