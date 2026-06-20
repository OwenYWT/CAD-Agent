"""Tester feedback endpoint — captures the ONE signal the system cannot infer:
did the part actually print? success=True only means 'code executed + geometry printable',
not 'the user printed it successfully'. This is the wedge's ground-truth metric."""
from fastapi import APIRouter, Depends

from app.api.auth import verify_api_key
from app.models.schemas import FeedbackRequest
from app.storage.history import save_feedback, get_feedback

router = APIRouter(prefix="/api/feedback", tags=["feedback"])


@router.post("")
async def api_save_feedback(req: FeedbackRequest, _key=Depends(verify_api_key)):
    saved = await save_feedback(
        request_id=req.request_id,
        rating=req.rating,
        printed=req.printed,
        note=req.note,
    )
    return {"ok": True, "feedback": saved}


@router.get("/{request_id}")
async def api_get_feedback(request_id: str, _key=Depends(verify_api_key)):
    return await get_feedback(request_id)
