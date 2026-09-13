"""Keep each real E2E deployment's fixtures and evidence in its own directory."""
import os
from pathlib import Path


def evidence_path(filename: str) -> Path:
    directory = Path(os.getenv("CAD_NATIVE_E2E_EVIDENCE_DIR", "/tmp"))
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory / filename
