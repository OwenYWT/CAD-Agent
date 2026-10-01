import importlib.util
from pathlib import Path
import pytest


def test_unprotected_main_never_passes_release_preflight():
    path=Path(__file__).resolve().parents[2]/'scripts/release/verify_merge_protection.py'
    spec=importlib.util.spec_from_file_location('gate',path)
    gate=importlib.util.module_from_spec(spec);spec.loader.exec_module(gate)
    with pytest.raises(ValueError,match='not mandatory'):
        gate.validate({})
