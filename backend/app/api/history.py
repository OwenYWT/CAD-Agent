from fastapi import APIRouter, Depends

from app.api.auth import verify_api_key
from app.storage.history import (
    list_sessions,
    list_panels,
    get_messages,
    delete_session,
)

router = APIRouter(prefix="/api/history", tags=["history"])


@router.get("/sessions")
async def api_list_sessions(_key=Depends(verify_api_key)):
    return await list_sessions()


@router.get("/sessions/{session_id}/panels")
async def api_list_panels(session_id: str, _key=Depends(verify_api_key)):
    return await list_panels(session_id)


@router.get("/panels/{panel_id}/messages")
async def api_get_messages(panel_id: str, _key=Depends(verify_api_key)):
    return await get_messages(panel_id)


@router.delete("/sessions/{session_id}")
async def api_delete_session(session_id: str, _key=Depends(verify_api_key)):
    await delete_session(session_id)
    return {"ok": True}
