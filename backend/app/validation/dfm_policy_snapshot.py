"""Freeze tenant DFM rules and process knowledge before isolated execution."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_serializer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


class FrozenDFMRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    process: str
    category: str
    check_type: Literal["geometric", "heuristic"]
    threshold_min: float | None = None
    threshold_max: float | None = None
    unit: str = ""
    severity: Literal["critical", "warning", "info"]
    description: str = ""
    suggestion_template: str = ""


class DFMPolicySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["dfm-policy-snapshot.v1"]
    tenant_id: UUID
    process: str
    material: str
    rule_set_versions: dict[str, str]
    rules: tuple[FrozenDFMRule, ...]
    knowledge_constraints: dict[str, Any]
    source: Literal["tenant-postgres", "builtin-default"]
    threshold_precedence: Literal["configured_rules"] | None = None

    @model_serializer(mode="wrap")
    def historical_identity(self, handler):
        payload = handler(self)
        if self.threshold_precedence is None:
            payload.pop("threshold_precedence", None)
        return payload

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @property
    def policy_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


_PROCESS_MAP = {
    "fdm": "FDM",
    "sla": "SLA",
    "cnc": "CNC",
    "laser_cut": "sheet_metal",
    "sheet_metal": "sheet_metal",
    "injection_mold": "injection_mold",
    "die_casting": "die_casting",
    "generic": "FDM",
}
_PROCESS_NODE_MAP = {
    "FDM": "proc_fdm",
    "SLA": "proc_sla",
    "CNC": "proc_cnc_3axis",
    "sheet_metal": "proc_sheet",
    "injection_mold": "proc_injection",
    "die_casting": "proc_die_casting",
}


def _severity(value: object) -> str:
    normalized = str(value or "warning").strip().lower()
    return {
        "error": "critical",
        "warn": "warning",
    }.get(normalized, normalized)


def _builtin_rules(process: str) -> list[dict[str, Any]]:
    path = Path(__file__).parents[1] / "dfm" / "default_rules.json"
    return [
        item
        for item in json.loads(path.read_text(encoding="utf-8"))
        if item["process"] == process
    ]


async def resolve_dfm_policy_snapshot(
    connection: AsyncConnection,
    *,
    tenant_id: UUID,
    manufacturing_profile: dict[str, Any] | None,
) -> DFMPolicySnapshot:
    profile = dict(manufacturing_profile or {})
    process_key = str(profile.get("process") or "generic").strip().lower()
    if process_key not in _PROCESS_MAP:
        raise ValueError(f"unsupported manufacturing process: {process_key}")
    process = _PROCESS_MAP[process_key]
    material = str(profile.get("material") or "unspecified")
    rows = (
        await connection.execute(
            text(
                """
                SELECT r.*, s.version AS rule_set_version
                FROM dfm_rules r
                JOIN dfm_rule_sets s
                  ON s.tenant_id=r.tenant_id AND s.id=r.rule_set_id
                WHERE r.tenant_id=:tenant_id AND r.process=:process
                ORDER BY r.id
                """
            ),
            {"tenant_id": tenant_id, "process": process},
        )
    ).mappings().all()
    source = "tenant-postgres"
    if rows:
        raw_rules = [dict(row) for row in rows if row["enabled"]]
        versions = {
            str(row["rule_set_id"]): str(row["rule_set_version"])
            for row in rows
        }
    else:
        raw_rules = _builtin_rules(process)
        versions = {f"builtin_{process.lower()}": "1"}
        source = "builtin-default"
    rules = tuple(
        FrozenDFMRule.model_validate(
            {
                "id": str(row["id"]),
                "process": str(row.get("process") or process),
                "category": str(row["category"]),
                "check_type": str(row.get("check_type") or "geometric"),
                "threshold_min": row.get("threshold_min"),
                "threshold_max": row.get("threshold_max"),
                "unit": str(row.get("unit") or ""),
                "severity": _severity(row.get("severity")),
                "description": str(row.get("description") or ""),
                "suggestion_template": str(
                    row.get("suggestion_template") or ""
                ),
            }
        )
        for row in raw_rules
    )
    process_node = _PROCESS_NODE_MAP.get(process)
    constraints: dict[str, Any] = {}
    if process_node:
        process_knowledge = (
            await connection.execute(
                text(
                    """
                    SELECT e.properties
                    FROM knowledge_edges e
                    WHERE e.tenant_id=:tenant_id
                      AND e.customer_id='default'
                      AND e.source_id=:process_node
                      AND e.target_id=:process_node
                      AND e.relationship='has_constraint'
                    ORDER BY e.id LIMIT 1
                    """
                ),
                {"tenant_id": tenant_id, "process_node": process_node},
            )
        ).mappings().one_or_none()
        if process_knowledge is not None:
            constraints.update(dict(process_knowledge["properties"] or {}))
        material_knowledge = (
            await connection.execute(
                text(
                    """
                    SELECT e.properties
                    FROM knowledge_edges e
                    JOIN knowledge_nodes n
                      ON n.tenant_id=e.tenant_id
                     AND n.customer_id=e.customer_id
                     AND n.id=e.target_id
                    WHERE e.tenant_id=:tenant_id
                      AND e.customer_id='default'
                      AND e.source_id=:process_node
                      AND e.relationship='supports_material'
                      AND (
                        lower(n.name)=lower(:material)
                        OR lower(n.id)=lower(:material)
                      )
                    ORDER BY n.id LIMIT 1
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "process_node": process_node,
                    "material": material,
                },
            )
        ).mappings().one_or_none()
        if material_knowledge is not None:
            constraints.update(dict(material_knowledge["properties"] or {}))
    return DFMPolicySnapshot(
        schema_version="dfm-policy-snapshot.v1",
        tenant_id=tenant_id,
        process=process,
        material=material,
        rule_set_versions=versions,
        rules=rules,
        knowledge_constraints=constraints,
        source=source,
        threshold_precedence="configured_rules",
    )
