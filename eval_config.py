from __future__ import annotations

import os
from pathlib import Path


EVAL_ROOT = Path(__file__).resolve().parent


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser().resolve() if value else None


def default_data_root() -> Path:
    return _env_path("EVAL_ALGORITHMS_DATA_ROOT") or EVAL_ROOT / "data"


def default_cadprompt_dir() -> Path:
    return _env_path("CADPROMPT_DIR") or default_data_root() / "CADPrompt"


def default_cadcoder_file() -> Path:
    return _env_path("CADCODER_FILE") or default_data_root() / "CAD-coder" / "cad_data_train_high.json"


def default_results_root() -> Path:
    return _env_path("EVAL_ALGORITHMS_RESULTS_ROOT") or EVAL_ROOT / "results"


def discover_cad_agent_root() -> Path:
    configured = _env_path("CAD_AGENT_ROOT")
    candidates = [
        configured,
        EVAL_ROOT.parent / "CAD-Agent",
        EVAL_ROOT / "CAD-Agent",
    ]
    for candidate in candidates:
        if candidate and (candidate / "backend" / "app").is_dir():
            return candidate.resolve()
    raise FileNotFoundError(
        "Could not find CAD-Agent root. Set CAD_AGENT_ROOT to the repository path."
    )


def default_backend_dir() -> Path:
    configured = _env_path("CAD_AGENT_BACKEND_DIR")
    if configured:
        return configured
    return discover_cad_agent_root() / "backend"


def default_storage_root() -> Path:
    configured = _env_path("CAD_AGENT_STORAGE_ROOT")
    if configured:
        return configured
    return default_backend_dir() / "data" / "files"


def resolve_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()
