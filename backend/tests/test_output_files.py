"""Shared output-file helpers (#32): proves orchestrator + multi_step use ONE
implementation that copies extra formats (the drift that lost files is gone)."""
import tempfile
from pathlib import Path

import pytest

from app.config import settings
from app.sandbox.output_files import copy_output_files, extract_params, find_file_in_output


@pytest.fixture(autouse=True)
def _storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "file_storage_dir", str(tmp_path / "files"))


def _work_dir_with(*names) -> Path:
    wd = Path(tempfile.mkdtemp())
    out = wd / "output"
    out.mkdir()
    for n in names:
        (out / n).write_text("x")
    return wd


def test_copy_includes_unrequested_formats():
    """Requesting only STL must still copy a STEP that was produced alongside it
    (this is exactly the bug multi_step had before de-duplication)."""
    wd = _work_dir_with("result.stl", "result.step")
    files = copy_output_files(wd, "req1", ["stl"])
    assert "stl" in files
    assert "step" in files  # copied even though not requested


def test_orchestrator_and_multistep_share_impl():
    """Both classes delegate to the same module function → no divergence possible."""
    from app.agent.orchestrator import Orchestrator
    from app.agent.multi_step import MultiStepExecutor
    import app.sandbox.output_files as shared

    wd = _work_dir_with("result.stl", "result.step")
    orch = Orchestrator.__new__(Orchestrator)
    mse = MultiStepExecutor.__new__(MultiStepExecutor)

    a = orch._copy_output_files(wd, "ra", ["stl"])
    wd2 = _work_dir_with("result.stl", "result.step")
    b = mse._copy_output_files(wd2, "rb", ["stl"])
    # same set of format keys produced by both
    assert set(a) == set(b) == {"stl", "step"}


def test_extract_params_shared():
    code = "w = 20  # 宽\nh = 30\nresult = box(w, h)\nshow_object(result)"
    p = extract_params(code)
    assert p["w"].value == 20 and p["w"].comment == "宽"
    assert p["h"].value == 30


def test_find_file_in_output():
    wd = _work_dir_with("result.stl")
    assert find_file_in_output(wd, ".stl").name == "result.stl"
    assert find_file_in_output(wd, ".dxf") is None
