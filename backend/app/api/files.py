import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.api.auth import verify_api_key
from app.config import settings
from app.storage.file_ownership import request_belongs_to

router = APIRouter()

MEDIA_TYPES = {
    ".step": "application/step",
    ".stp": "application/step",
    ".stl": "application/sla",
    ".dxf": "application/dxf",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".py": "text/x-python",
    ".bas": "text/plain",
}

# Allowed file extensions for download
_ALLOWED_EXTENSIONS = frozenset({
    ".step", ".stp", ".stl", ".dxf", ".svg", ".png", ".py", ".bas",
})

# request_id must be a UUID-like string (hex + hyphens)
_SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
# filename must be simple: alphanum, dots, hyphens, underscores
_SAFE_FILENAME_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")


@router.get("/files/{request_id}/{filename}")
async def download_file(
    request_id: str,
    filename: str,
    _credential: str | None = Depends(verify_api_key),
):
    """下载生成的 CAD 文件"""
    # Validate request_id format
    if not _SAFE_ID_PATTERN.match(request_id):
        raise HTTPException(status_code=400, detail="Invalid request ID format")
    if not request_belongs_to(request_id, _credential):
        raise HTTPException(status_code=404, detail="File not found")

    # Reject path traversal and special characters in filename
    if not _SAFE_FILENAME_PATTERN.match(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")

    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    # Check extension allowlist
    suffix = Path(filename).suffix.lower()
    if suffix not in _ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"File type '{suffix}' not allowed")

    # Resolve and verify the path stays within storage dir
    storage_dir = Path(settings.file_storage_dir).resolve()
    request_dir = (storage_dir / request_id).resolve()
    file_path = (request_dir / filename).resolve()

    if request_dir.parent != storage_dir or file_path.parent != request_dir:
        raise HTTPException(status_code=400, detail="Invalid file path")

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    media_type = MEDIA_TYPES.get(suffix, "application/octet-stream")

    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        filename=filename,
    )
