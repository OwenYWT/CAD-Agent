from contextlib import asynccontextmanager
from tempfile import TemporaryDirectory
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError
from fastapi.responses import FileResponse, Response
from app.object_store import ObjectIntegrityError, download_verified_object

from app.api.auth import verify_api_key
from app.config import settings
from app.storage.file_ownership import get_project_file, request_belongs_to

router = APIRouter()

MEDIA_TYPES = {
    ".fcstd": "application/vnd.freecad.fcstd",
    ".step": "application/step",
    ".stp": "application/step",
    ".stl": "application/sla",
    ".dxf": "application/dxf",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".json": "application/json",
    ".csv": "text/csv; charset=utf-8",
    ".py": "text/x-python",
    ".bas": "text/plain",
}

# Allowed file extensions for download
_ALLOWED_EXTENSIONS = frozenset({
    ".fcstd",
    ".step",
    ".stp",
    ".stl",
    ".dxf",
    ".svg",
    ".png",
    ".json",
    ".csv",
    ".py",
    ".bas",
})

# request_id must be a UUID-like string (hex + hyphens)
_SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
# filename must be simple: alphanum, dots, hyphens, underscores
_SAFE_FILENAME_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")


class FileUploadForm(BaseModel):
    file: UploadFile

    @classmethod
    def openapi_body(cls):
        return {"requestBody": {"required": True, "content": {
            "multipart/form-data": {"schema": cls.model_json_schema()}}}}


@asynccontextmanager
async def read_upload_form(request: Request, model: type[FileUploadForm]):
    # Called by the endpoint after authentication. The context also closes all
    # spooled files on validation errors, cancellation and normal completion.
    async with request.form(max_files=1, max_fields=len(model.model_fields) - 1) as fields:
        try:
            upload = model.model_validate(dict(fields))
        except ValidationError as exc:
            raise RequestValidationError(exc.errors(include_input=False)) from exc
        yield upload


class VerifiedArtifactResponse(Response):
    """Keep the verified temporary file alive exactly for the ASGI response."""
    def __init__(self, metadata, *, filename: str | None = None, headers=None):
        self.metadata = metadata
        self.filename = filename
        super().__init__(media_type=metadata["content_type"], headers={
            "ETag": f'"{metadata["sha256"]}"', **(headers or {})})

    async def __call__(self, scope, receive, send):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "verified"
            try:
                await download_verified_object(self.metadata, path)
            except ObjectIntegrityError as exc:
                raise HTTPException(503, detail="Stored artifact failed checksum verification") from exc
            await FileResponse(path, filename=self.filename, media_type=self.media_type,
                               headers={k: v for k, v in self.headers.items()
                                        if k != "content-length"})(scope, receive, send)


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
    if not await request_belongs_to(request_id, _credential):
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

    if settings.durable_control_plane_enabled:
        metadata = await get_project_file(request_id, filename)
        if metadata is None:
            raise HTTPException(status_code=404, detail="File not found")
        return VerifiedArtifactResponse(metadata, filename=filename)

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
