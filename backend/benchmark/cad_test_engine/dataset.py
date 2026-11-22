"""Load and validate the CADTest dataset without importing external code."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def resolve_dataset_root(root: Path) -> Path:
    configured = Path(root).expanduser().resolve()
    candidates = (
        configured,
        configured / "CAD_test",
    )
    for candidate in candidates:
        if (candidate / "data" / "dataset-v1.json").is_file():
            return candidate
    raise FileNotFoundError(
        "CADTest dataset manifest not found; expected <root>/data/dataset-v1.json"
    )


@dataclass(frozen=True)
class CadTestDataset:
    root: Path
    manifest: dict[str, Any]

    @property
    def dataset_hash(self) -> str:
        return str(self.manifest["dataset_hash"])

    @property
    def cases(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.manifest.get("cases") or ())

    @property
    def case_ids(self) -> frozenset[str]:
        return frozenset(str(case["id"]) for case in self.cases)

    def case(self, case_id: str) -> dict[str, Any]:
        for case in self.cases:
            if str(case.get("id")) == case_id:
                result = dict(case)
                case_root = self.root / "data" / "cases" / case_id
                result["root"] = str(case_root)
                result["prompt"] = (case_root / "prompt.txt").read_text(encoding="utf-8")
                return result
        raise KeyError(f"unknown CADTest case: {case_id}")

    def case_root(self, case_id: str) -> Path:
        case_root = self.root / "data" / "cases" / case_id
        if not case_root.is_dir():
            raise KeyError(f"unknown CADTest case: {case_id}")
        return case_root


def load_dataset(root: Path, *, verify_files: bool = True) -> CadTestDataset:
    dataset_root = resolve_dataset_root(root)
    manifest = _read_json(dataset_root / "data" / "dataset-v1.json")
    if manifest.get("status") != "validated":
        raise ValueError("CADTest dataset controls have not passed")
    expected_manifest_hash = manifest.get("dataset_hash")
    actual_manifest_hash = _canonical(
        {key: value for key, value in manifest.items() if key != "dataset_hash"}
    )
    if expected_manifest_hash != actual_manifest_hash:
        raise ValueError("CADTest dataset manifest hash mismatch")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("CADTest dataset contains no cases")
    ids = [str(case.get("id")) for case in cases if isinstance(case, dict)]
    if len(ids) != len(cases) or len(set(ids)) != len(ids):
        raise ValueError("CADTest dataset case IDs must be unique")

    if verify_files:
        controls_path = dataset_root / "reports" / "controls-validation.json"
        controls_hash = manifest.get("controls_sha256")
        if not controls_path.is_file() or _digest(controls_path) != controls_hash:
            raise ValueError("CADTest reference control evidence hash mismatch")
        for case in cases:
            case_id = str(case["id"])
            case_root = dataset_root / "data" / "cases" / case_id
            case_json = case_root / "case.json"
            if not case_json.is_file() or _digest(case_json) != case.get("case_json_sha256"):
                raise ValueError(f"CADTest case metadata integrity mismatch: {case_id}")
            for relative, expected_hash in (case.get("file_hashes") or {}).items():
                path = dataset_root / relative
                if not path.is_file() or _digest(path) != expected_hash:
                    raise ValueError(f"CADTest dataset integrity mismatch: {relative}")

    return CadTestDataset(root=dataset_root, manifest=manifest)
