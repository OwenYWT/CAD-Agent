"""Deterministic identifiers shared by geometry planning and its evidence."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any


def bounded_identifier(value: str) -> str:
    if len(value) <= 120:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{value[:103]}-{digest}"


def slug_identifier(value: str, *, fallback: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return bounded_identifier(cleaned or fallback)


def unique_identifier(base: str, seen: set[str]) -> str:
    identifier = bounded_identifier(base)
    suffix = 2
    while identifier in seen:
        identifier = bounded_identifier(f"{base}-{suffix:02d}")
        suffix += 1
    seen.add(identifier)
    return identifier


def feature_candidates(plan_data: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Use durable step identity, or derive one for legacy feature descriptions."""
    names = [str(item).strip() for item in plan_data.get("features") or () if str(item).strip()]
    sources: list[tuple[str | None, str]] = [(None, name) for name in names]
    if not sources:
        for step in plan_data.get("steps") or ():
            data = step if isinstance(step, Mapping) else step.model_dump(mode="python")
            key = str(data.get("step_key") or "").strip()
            description = str(data.get("description") or key).strip()
            if description:
                sources.append((key or None, description))
    if not sources:
        description = str(plan_data.get("objective") or plan_data.get("description") or "").strip()
        sources.append((None, description or "main body"))
    seen: set[str] = set()
    return [
        (unique_identifier(key or slug_identifier(name, fallback=f"feature-{index:02d}"), seen), name)
        for index, (key, name) in enumerate(sources, start=1)
    ]
