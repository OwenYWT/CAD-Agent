#!/usr/bin/env python3
"""Validate the repository-shared CAD-Agent feature/test map."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
MAP_PATH = ROOT / ".agents" / "skills" / "cad-agent-review" / "references" / "feature-test-map.json"
LOGIC_PATH = ROOT / ".agents" / "skills" / "cad-agent-review" / "references" / "feature-logic.md"
EXPECTED_IDS = [f"{index:02d}" for index in range(1, 25)]


def main() -> int:
    errors: list[str] = []
    try:
        data = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"feature map load failed: {exc}")
        return 2
    if not (ROOT / data.get("inventory", "")).exists():
        errors.append(f"missing inventory: {data.get('inventory')}")
    if not LOGIC_PATH.exists():
        errors.append(f"missing feature logic baseline: {LOGIC_PATH.relative_to(ROOT)}")
    domains = data.get("domains")
    if not isinstance(domains, list):
        errors.append("domains must be a list")
        domains = []
    ids = [item.get("id") for item in domains if isinstance(item, dict)]
    if ids != EXPECTED_IDS:
        errors.append(f"domain IDs must be exactly {EXPECTED_IDS}; got {ids}")
    for item in domains:
        if not isinstance(item, dict):
            errors.append("domain entry is not an object")
            continue
        domain_id = item.get("id", "<missing>")
        for key in ("name", "feature_ids", "logic", "implementation", "tests"):
            if key not in item:
                errors.append(f"domain {domain_id}: missing {key}")
        paths = list(item.get("implementation", [])) + list(item.get("tests", []))
        for path in paths:
            if not isinstance(path, str) or not (ROOT / path).exists():
                errors.append(f"domain {domain_id}: missing path {path}")
    if errors:
        print("Feature map validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    total_paths = sum(len(item["implementation"]) + len(item["tests"]) for item in domains)
    print(f"Feature map valid: {len(domains)} domains, {total_paths} referenced paths.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
