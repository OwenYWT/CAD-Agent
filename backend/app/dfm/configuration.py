"""Immutable, content-addressed rule inputs for a durable engineering check."""
from __future__ import annotations

from uuid import UUID

from app.dfm.models import DFMRule, rule_process, validate_rule_thresholds
from app.execution.canonical import canonical_sha256


async def capture_configuration(connection, *, tenant_id: UUID, principal_id: UUID,
                                process: str | None, origin: str = "admission") -> dict:
    from app.dfm.postgres_store import configured_rules
    return freeze_rules(await configured_rules(connection, tenant_id=tenant_id, process=process),
        tenant_id=tenant_id, principal_id=principal_id, process=process, origin=origin)


def freeze_rules(rules: list[DFMRule], *, tenant_id: UUID, principal_id: UUID,
                 process: str | None, origin: str = "admission") -> dict:
    if origin not in {"admission", "legacy_activity"}:
        raise ValueError("Unknown DFM configuration origin")
    configured = sorted(rules, key=lambda rule: rule.id)
    if len({rule.id for rule in configured}) != len(configured):
        raise ValueError("Duplicate DFM rule identities")
    for rule in configured:
        validate_rule_thresholds(rule.model_dump())
        if process and rule_process(rule.process) != rule_process(process):
            raise ValueError("DFM rule process does not match the check")
    content = {
        "schema_version": "dfm-rule-configuration.v1",
        "tenant_id": str(tenant_id), "principal_id": str(principal_id),
        "process": rule_process(process), "origin": origin,
        "rules": [rule.model_dump(mode="json") for rule in configured],
    }
    return {**content, "sha256": canonical_sha256(content)}


def configuration_rules(configuration: dict, *, process: str | None,
                        tenant_id: UUID | None = None,
                        principal_id: UUID | None = None) -> list[DFMRule]:
    rules = [DFMRule.model_validate(rule) for rule in configuration["rules"]]
    expected = freeze_rules(rules, tenant_id=UUID(configuration["tenant_id"]),
        principal_id=UUID(configuration["principal_id"]), process=process,
        origin=configuration["origin"])
    if expected != configuration:
        raise ValueError("DFM configuration integrity or process mismatch")
    if tenant_id is not None and configuration["tenant_id"] != str(tenant_id):
        raise ValueError("DFM configuration tenant mismatch")
    if principal_id is not None and configuration["principal_id"] != str(principal_id):
        raise ValueError("DFM configuration principal mismatch")
    return rules
