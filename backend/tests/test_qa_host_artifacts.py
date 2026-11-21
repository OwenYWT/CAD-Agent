import json
import os
import time

import pytest

from app.config import settings
from app.main import _cleanup_old_files
from app.sandbox.executor import CadQueryExecutor


@pytest.mark.parametrize("runtime", ["docker", "podman"])
def test_result_protocol_requires_bounded_manifest_and_declared_regular_files(tmp_path, runtime):
    executor = CadQueryExecutor(runtime_name=runtime)
    output = tmp_path / "output"
    output.mkdir()
    manifest = output / "result.json"
    def read(payload):
        manifest.write_bytes(payload)
        return executor._read_result(output, tmp_path, time.time())

    declared = json.dumps({"status": "success", "files": {"step": "/sandbox/output/plate.step"}}).encode()
    assert read(declared).error_code == "sandbox_protocol_error"
    (output / "plate.step").write_bytes(b"test artifact")
    assert read(declared).files == {"step": output / "plate.step"}
    for payload in (b"{", b" " * (4 * 1024 * 1024 + 1), b'{"status":"error","error":{}}'):
        assert read(payload).error_code == "sandbox_protocol_error"


def test_collection_expires_only_completed_idle_request_files(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "file_storage_dir", str(tmp_path))
    monkeypatch.setattr(settings, "file_ttl_hours", 1)
    past = time.time() - 7200
    for name in ("expired", "active", "partial", "capability_users"):
        directory = tmp_path / name
        directory.mkdir()
        leaf = directory / "plate.step"
        leaf.write_bytes(b"part")
        if name in ("expired", "active"):
            marker = directory / ".materialized"
            marker.touch()
            os.utime(marker, (past, past))
        if name != "active":
            os.utime(leaf, (past, past))
        os.utime(directory, (past, past))
    _cleanup_old_files()
    assert {p.name for p in tmp_path.iterdir()} == {"active", "partial", "capability_users"}
    assert all((p / "plate.step").is_file() for p in tmp_path.iterdir())
