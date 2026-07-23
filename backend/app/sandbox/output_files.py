"""Shared output-file + param-extraction helpers.

Previously duplicated (and drifted) between Orchestrator and MultiStepExecutor —
the multi-step copy lost output formats because the "also copy other formats" fix
(commit 2cb1bd9) only landed in the orchestrator. Single source of truth now.
"""
import shutil
from pathlib import Path

from app.config import settings
from app.models.schemas import ParamConfig
from app.parameters import extract_parameters

_FORMAT_EXTENSIONS = {
    "step": ".step",
    "stl": ".stl",
    "dxf": ".dxf",
    "svg": ".svg",
}


def find_file_in_output(work_dir: Path, ext: str) -> Path | None:
    output_dir = work_dir / "output"
    for f in output_dir.glob(f"*{ext}"):
        return f
    return None


def find_stl_in_output(work_dir: Path) -> Path | None:
    return find_file_in_output(work_dir, ".stl")


def copy_output_files(
    work_dir: Path, request_id: str, output_formats: list[str]
) -> dict[str, str]:
    """Copy requested formats AND any other available output files into storage.
    The 'also copy other formats' pass ensures e.g. a STEP produced alongside a
    requested STL is still served (this is what the multi-step copy used to miss)."""
    dest_dir = Path(settings.file_storage_dir) / request_id
    dest_dir.mkdir(parents=True, exist_ok=True)

    files: dict[str, str] = {}
    output_dir = work_dir / "output"

    # Requested formats first
    for fmt in output_formats:
        ext = _FORMAT_EXTENSIONS.get(fmt, f".{fmt}")
        for src_file in output_dir.glob(f"*{ext}"):
            dest_file = dest_dir / src_file.name
            shutil.copy2(src_file, dest_file)
            files[fmt] = f"/api/files/{request_id}/{src_file.name}"
            break

    # Also copy any other available output files not in requested formats
    for ext_name, ext in _FORMAT_EXTENSIONS.items():
        if ext_name in files:
            continue
        for src_file in output_dir.glob(f"*{ext}"):
            dest_file = dest_dir / src_file.name
            shutil.copy2(src_file, dest_file)
            files[ext_name] = f"/api/files/{request_id}/{src_file.name}"
            break

    return files


def extract_params(code: str) -> dict[str, ParamConfig]:
    params: dict[str, ParamConfig] = {}
    for parameter in extract_parameters(code):
        params[parameter.name] = ParamConfig(
            value=parameter.value,
            comment=parameter.comment or parameter.display_name,
        )
    return params
