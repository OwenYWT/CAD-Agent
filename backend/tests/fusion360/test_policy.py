import hashlib
from pathlib import Path

import pytest

from app.fusion360.artifacts import ArtifactStore, safe_basename
from app.fusion360.contract import CAD_ACTION_ADAPTER
from app.fusion360.errors import FusionConnectorError
from app.fusion360.policy import (
    action_intent_hash,
    canonical_json,
    classify_risk,
    idempotency_payload_hash,
)


def _action(mode="preview", amount=3):
    return CAD_ACTION_ADAPTER.validate_python({
        "action": "cad.update_parameter",
        "execution_mode": mode,
        "target": {"document_id": "d", "component_id": "c", "parameter_id": "p"},
        "value": {"amount": amount, "unit": "mm"},
    })


def test_cad_c14n_1_golden_vector():
    value = {"b": 1.0, "a": "e\u0301", "z": -0.0}
    encoded = canonical_json(value)
    assert encoded == '{"a":"é","b":1,"z":0}'.encode()
    assert hashlib.sha256(encoded).hexdigest() == "9371c9350b32de3c25260fe5ab264bcad966564d2bb5b73f2777bea0d799c7f6"


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_canonicalizer_rejects_nonfinite_numbers(number):
    with pytest.raises(ValueError):
        canonical_json(number)


def test_intent_hash_excludes_execution_metadata_but_includes_value():
    assert action_intent_hash(_action("preview")) == action_intent_hash(_action("execute"))
    assert action_intent_hash(_action(amount=3)) != action_intent_hash(_action(amount=4))


def test_idempotency_hash_is_owner_and_phase_scoped():
    intent = action_intent_hash(_action())
    assert idempotency_payload_hash("a", "preview", intent) != idempotency_payload_hash("a", "execute", intent)
    assert idempotency_payload_hash("a", "execute", intent) != idempotency_payload_hash("b", "execute", intent)


def test_all_action_risks_are_explicit():
    assert classify_risk("cad.export") == "low"
    assert classify_risk("cad.save_as") == "high"
    with pytest.raises(FusionConnectorError) as raised:
        classify_risk("cad.unknown")
    assert raised.value.code == "UNSUPPORTED_ACTION"


def test_artifact_paths_are_confined(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    request_id = "2144d1f8-bfe1-43da-b3c7-8b444bd77e66"
    path = store.staging_path("owner", request_id, "part.step")
    assert path.is_relative_to(store.root)
    with pytest.raises(FusionConnectorError):
        store.resolve_local("owner", request_id, "../../outside")
    with pytest.raises(FusionConnectorError):
        safe_basename("../secret")


def test_step_artifact_signature_hash_and_empty_rejection(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    request_id = "2144d1f8-bfe1-43da-b3c7-8b444bd77e66"
    valid = store.staging_path("owner", request_id, "part.step")
    valid.write_bytes(b"ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;\n")
    size, digest = store.validate_file(valid, "step")
    assert size == valid.stat().st_size
    assert digest == hashlib.sha256(valid.read_bytes()).hexdigest()
    empty = store.staging_path("owner", request_id, "empty.step")
    empty.touch()
    with pytest.raises(FusionConnectorError) as raised:
        store.validate_file(empty, "step")
    assert raised.value.code == "ARTIFACT_INVALID"


def test_binary_stl_requires_triangle_count_to_match_file_size(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    valid = store.root / "valid.stl"
    valid.parent.mkdir(parents=True)
    valid.write_bytes(b"binary stl".ljust(80, b"\0") + (1).to_bytes(4, "little") + b"\0" * 50)
    assert store.validate_file(valid, "stl")[0] == 134

    solid_header = store.root / "solid-header-binary.stl"
    solid_header.write_bytes(b"solid binary header".ljust(80, b"\0") + (1).to_bytes(4, "little") + b"\0" * 50)
    assert store.validate_file(solid_header, "stl")[0] == 134

    invalid = store.root / "invalid.stl"
    invalid.write_bytes(b"not an stl".ljust(84, b"\0"))
    with pytest.raises(FusionConnectorError) as raised:
        store.validate_file(invalid, "stl")
    assert raised.value.code == "ARTIFACT_INVALID"
