from fastapi import APIRouter, Depends, HTTPException

from app.api.auth import verify_api_key
from app.parts_library.data import lookup

router = APIRouter()


@router.get("/api/parts/{designation}")
async def get_part(
    designation: str,
    api_key: str | None = Depends(verify_api_key),
):
    """查询标准件参数"""
    result = lookup(designation)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Standard part '{designation}' not found")
    return {"designation": designation.upper(), "params": result}
