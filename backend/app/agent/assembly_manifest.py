from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from typing import Any


def stable_part_id(name: str, existing_ids: set[str] | None = None) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "part"
    existing_ids = existing_ids or set()
    candidate = base
    index = 2
    while candidate in existing_ids:
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def code_hash(code: str) -> str:
    normalized = code.strip().replace("\r\n", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def enrich_assembly_parts(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    existing_ids: set[str] = set()
    for index, raw_part in enumerate(parts):
        part = deepcopy(raw_part)
        name = str(part.get("name") or f"part-{index + 1}")
        part_id = stable_part_id(str(part.get("part_id") or name), existing_ids)
        existing_ids.add(part_id)
        part["part_id"] = part_id
        part.setdefault("name", name)
        part.setdefault("description", "")
        part.setdefault("status", "success")
        part.setdefault("position", [0, 0, 0])
        part.setdefault("color", "lightgray")
        part_code = str(part.get("code") or "")
        part["code"] = part_code
        if part_code:
            part["code_hash"] = code_hash(part_code)
        else:
            part.setdefault("code_hash", code_hash(part_code))
        enriched.append(part)
    return enriched


def changed_part_ids(before_parts: list[dict[str, Any]], after_parts: list[dict[str, Any]]) -> dict[str, list[str]]:
    before_by_id = {str(part.get("part_id") or part.get("name")): part for part in before_parts}
    after_by_id = {str(part.get("part_id") or part.get("name")): part for part in after_parts}
    before_ids = set(before_by_id)
    after_ids = set(after_by_id)
    shared_ids = sorted(before_ids & after_ids)
    return {
        "added": sorted(after_ids - before_ids),
        "removed": sorted(before_ids - after_ids),
        "changed": [
            part_id
            for part_id in shared_ids
            if before_by_id[part_id].get("code_hash") != after_by_id[part_id].get("code_hash")
            or before_by_id[part_id].get("code") != after_by_id[part_id].get("code")
            or before_by_id[part_id].get("status") != after_by_id[part_id].get("status")
        ],
        "unchanged": [
            part_id
            for part_id in shared_ids
            if before_by_id[part_id].get("code_hash") == after_by_id[part_id].get("code_hash")
            and before_by_id[part_id].get("code") == after_by_id[part_id].get("code")
            and before_by_id[part_id].get("status") == after_by_id[part_id].get("status")
        ],
    }
