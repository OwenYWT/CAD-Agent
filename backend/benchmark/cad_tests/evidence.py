"""Normalize existing benchmark rows into authoritative evaluation evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .contracts import Evidence


@dataclass(frozen=True)
class EvidenceContext:
    case: Mapping[str, Any]
    row: Mapping[str, Any]
    observations: Mapping[str, Any]
    evidence: tuple[Evidence, ...] = ()
    reasons: Mapping[str, str] = field(default_factory=dict)


def _geometry_evidence(row: Mapping[str, Any]) -> tuple[Evidence, ...]:
    source = row.get("evidence") or {}
    authority = source.get("authority") or {}
    result: list[Evidence] = []
    if source.get("bbox_extents_mm") is not None:
        result.append(
            Evidence(
                evidence_id="geometry-bbox",
                source_kind="geometry_bbox",
                source_ref=str(row.get("request_id") or "benchmark-row"),
                authority=str(authority.get("bbox_extents_mm", "unavailable")),
                observed={"bbox_extents_mm": source["bbox_extents_mm"]},
            )
        )
    for name in ("watertight", "printable", "valid_solid", "solid_count"):
        if source.get(name) is None:
            continue
        result.append(
            Evidence(
                evidence_id=f"geometry-{name}",
                source_kind=f"geometry_{name}",
                source_ref=str(row.get("request_id") or "benchmark-row"),
                authority=str(authority.get(name, "unavailable")),
                observed={name: source[name]},
            )
        )
    return tuple(result)


def _artifact_evidence(row: Mapping[str, Any]) -> tuple[Evidence, ...]:
    gate = row.get("artifact_gate") or {}
    result: list[Evidence] = []
    for prefix in ("step", "stl", "dxf"):
        path = gate.get(f"{prefix}_path")
        digest = gate.get(f"{prefix}_sha256")
        readable = gate.get(f"{prefix}_readable")
        if path is None and digest is None and readable is None:
            continue
        result.append(
            Evidence(
                evidence_id=f"artifact-{prefix}",
                source_kind=f"artifact_{prefix}",
                source_ref=str(path) if path is not None else None,
                authority="artifact_reader" if readable is not None else "unavailable",
                artifact_sha256=digest,
                observed={f"{prefix}_readable": readable},
                details={"path": path},
            )
        )
    return tuple(result)


def normalize_benchmark_row(case: Mapping[str, Any], row: Mapping[str, Any]) -> EvidenceContext:
    """Build evidence without converting missing observations into measurements."""
    source = row.get("evidence") or {}
    observations = {
        "bbox_extents_mm": source.get("bbox_extents_mm"),
        "watertight": source.get("watertight"),
        "printable": source.get("printable"),
        "solid_count": source.get("solid_count"),
        "valid_solid": source.get("valid_solid"),
        "artifact_gate": row.get("artifact_gate") or {},
    }
    reasons: dict[str, str] = {}
    if observations["bbox_extents_mm"] is None:
        reasons["bbox_extents_mm"] = "authoritative bounding-box evidence unavailable"
    if observations["solid_count"] is None:
        reasons["solid_count"] = "authoritative solid-count evidence unavailable"
    if observations["valid_solid"] is None:
        reasons["valid_solid"] = "authoritative valid-solid evidence unavailable"
    return EvidenceContext(
        case=case,
        row=row,
        observations=observations,
        evidence=_geometry_evidence(row) + _artifact_evidence(row),
        reasons=reasons,
    )
