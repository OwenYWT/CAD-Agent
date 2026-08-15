"""Tenant-scoped PostgreSQL storage for DFM rules and engineering knowledge."""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db import tenant_transaction
from app.dfm.models import DFMRule, DFMRuleSet
from app.principal_context import current_principal
from app.repositories.identity import ensure_principal


ROOT = Path(__file__).parent


def _rule(row) -> DFMRule:
    return DFMRule(
        id=row["id"],
        process=row["process"],
        category=row["category"],
        check_type=row["check_type"],
        threshold_min=row["threshold_min"],
        threshold_max=row["threshold_max"],
        unit=row["unit"],
        severity=row["severity"],
        description=row["description"],
        suggestion_template=row["suggestion_template"],
        enabled=bool(row["enabled"]),
    )


async def _seed_rules(connection, context) -> None:
    count = await connection.scalar(
        text("SELECT count(*) FROM dfm_rule_sets WHERE tenant_id=:tenant"),
        {"tenant": context.tenant_id},
    )
    if int(count or 0):
        return
    rules = json.loads(
        (ROOT / "default_rules.json").read_text(encoding="utf-8")
    )
    grouped: dict[str, list[dict]] = {}
    for rule in rules:
        grouped.setdefault(rule["process"], []).append(rule)
    for process, items in grouped.items():
        set_id = f"default_{process.lower()}"
        await connection.execute(
            text(
                """
                INSERT INTO dfm_rule_sets (
                    tenant_id, id, name, process, is_builtin
                ) VALUES (
                    :tenant, :id, :name, :process, true
                ) ON CONFLICT DO NOTHING
                """
            ),
            {
                "tenant": context.tenant_id,
                "id": set_id,
                "name": f"{process} 默认规则",
                "process": process,
            },
        )
        for rule in items:
            await connection.execute(
                text(
                    """
                    INSERT INTO dfm_rules (
                        tenant_id, id, rule_set_id, process, category,
                        check_type, threshold_min, threshold_max, unit,
                        severity, description, suggestion_template, enabled
                    ) VALUES (
                        :tenant, :id, :set_id, :process, :category,
                        :check_type, :minimum, :maximum, :unit,
                        :severity, :description, :suggestion, true
                    ) ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "tenant": context.tenant_id,
                    "id": rule["id"],
                    "set_id": set_id,
                    "process": rule["process"],
                    "category": rule["category"],
                    "check_type": rule.get("check_type", "geometric"),
                    "minimum": rule.get("threshold_min"),
                    "maximum": rule.get("threshold_max"),
                    "unit": rule.get("unit", "mm"),
                    "severity": rule.get("severity", "warning"),
                    "description": rule.get("description", ""),
                    "suggestion": rule.get("suggestion_template", ""),
                },
            )


async def _rules_transaction():
    context = current_principal()
    transaction = tenant_transaction(
        context.tenant_id,
        context.principal_id,
    )
    return context, transaction


async def list_rule_sets() -> list[DFMRuleSet]:
    context, transaction = await _rules_transaction()
    async with transaction as connection:
        await ensure_principal(connection, context)
        await _seed_rules(connection, context)
        sets = (
            await connection.execute(
                text(
                    """
                    SELECT id, name, process FROM dfm_rule_sets
                    WHERE tenant_id=:tenant ORDER BY process
                    """
                ),
                {"tenant": context.tenant_id},
            )
        ).mappings().all()
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT * FROM dfm_rules
                    WHERE tenant_id=:tenant ORDER BY category, severity
                    """
                ),
                {"tenant": context.tenant_id},
            )
        ).mappings().all()
    by_set: dict[str, list[DFMRule]] = {}
    for row in rows:
        by_set.setdefault(row["rule_set_id"], []).append(_rule(row))
    return [
        DFMRuleSet(
            id=row["id"],
            name=row["name"],
            process=row["process"],
            rules=by_set.get(row["id"], []),
        )
        for row in sets
    ]


async def _query_rules(where: str, params: dict) -> list[DFMRule]:
    context, transaction = await _rules_transaction()
    async with transaction as connection:
        await ensure_principal(connection, context)
        await _seed_rules(connection, context)
        rows = (
            await connection.execute(
                text(
                    "SELECT * FROM dfm_rules "
                    "WHERE tenant_id=:tenant AND "
                    + where
                    + " ORDER BY category, severity"
                ),
                {"tenant": context.tenant_id, **params},
            )
        ).mappings().all()
    return [_rule(row) for row in rows]


async def get_rules_for_set(rule_set_id: str) -> list[DFMRule]:
    return await _query_rules("rule_set_id=:set_id", {"set_id": rule_set_id})


async def get_rules_by_process(process: str) -> list[DFMRule]:
    return await _query_rules(
        "process=:process AND enabled=true",
        {"process": process},
    )


async def get_all_enabled_rules() -> list[DFMRule]:
    return await _query_rules("enabled=true", {})


async def update_rule(rule_id: str, updates: dict) -> DFMRule | None:
    allowed = {
        "threshold_min",
        "threshold_max",
        "severity",
        "enabled",
        "description",
        "suggestion_template",
    }
    filtered = {key: value for key, value in updates.items() if key in allowed}
    if not filtered:
        return None
    context, transaction = await _rules_transaction()
    async with transaction as connection:
        await _seed_rules(connection, context)
        assignments = ", ".join(f"{key}=:{key}" for key in filtered)
        row = (
            await connection.execute(
                text(
                    f"""
                    UPDATE dfm_rules SET {assignments}
                    WHERE tenant_id=:tenant AND id=:id
                    RETURNING *
                    """
                ),
                {
                    "tenant": context.tenant_id,
                    "id": rule_id,
                    **filtered,
                },
            )
        ).mappings().one_or_none()
    return _rule(row) if row else None


async def clone_rule_set(
    source_id: str,
    new_id: str,
    new_name: str,
) -> DFMRuleSet | None:
    context, transaction = await _rules_transaction()
    async with transaction as connection:
        await _seed_rules(connection, context)
        source = (
            await connection.execute(
                text(
                    """
                    SELECT * FROM dfm_rule_sets
                    WHERE tenant_id=:tenant AND id=:id
                    """
                ),
                {"tenant": context.tenant_id, "id": source_id},
            )
        ).mappings().one_or_none()
        if source is None:
            return None
        try:
            await connection.execute(
                text(
                    """
                    INSERT INTO dfm_rule_sets (
                        tenant_id, id, name, process, is_builtin
                    ) VALUES (:tenant, :id, :name, :process, false)
                    """
                ),
                {
                    "tenant": context.tenant_id,
                    "id": new_id,
                    "name": new_name,
                    "process": source["process"],
                },
            )
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT * FROM dfm_rules
                        WHERE tenant_id=:tenant AND rule_set_id=:source
                        """
                    ),
                    {"tenant": context.tenant_id, "source": source_id},
                )
            ).mappings().all()
            for row in rows:
                rule_id = (
                    row["id"].replace(source_id, new_id)
                    if source_id in row["id"]
                    else f"{new_id}_{row['id']}"
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO dfm_rules (
                            tenant_id, id, rule_set_id, process, category,
                            check_type, threshold_min, threshold_max, unit,
                            severity, description, suggestion_template, enabled
                        ) VALUES (
                            :tenant, :id, :set_id, :process, :category,
                            :check_type, :minimum, :maximum, :unit,
                            :severity, :description, :suggestion, :enabled
                        )
                        """
                    ),
                    {
                        "tenant": context.tenant_id,
                        "id": rule_id,
                        "set_id": new_id,
                        "process": row["process"],
                        "category": row["category"],
                        "check_type": row["check_type"],
                        "minimum": row["threshold_min"],
                        "maximum": row["threshold_max"],
                        "unit": row["unit"],
                        "severity": row["severity"],
                        "description": row["description"],
                        "suggestion": row["suggestion_template"],
                        "enabled": row["enabled"],
                    },
                )
        except IntegrityError as exc:
            raise ValueError("规则集 ID 已存在") from exc
    rules = await get_rules_for_set(new_id)
    return DFMRuleSet(
        id=new_id,
        name=new_name,
        process=source["process"],
        rules=rules,
    )


async def delete_rule_set(set_id: str) -> bool:
    if set_id.startswith("default_"):
        return False
    context, transaction = await _rules_transaction()
    async with transaction as connection:
        deleted = await connection.execute(
            text(
                """
                DELETE FROM dfm_rule_sets
                WHERE tenant_id=:tenant AND id=:id AND is_builtin=false
                """
            ),
            {"tenant": context.tenant_id, "id": set_id},
        )
    return deleted.rowcount == 1


async def _seed_knowledge(connection, context) -> None:
    count = await connection.scalar(
        text(
            """
            SELECT count(*) FROM knowledge_nodes
            WHERE tenant_id=:tenant AND customer_id='default'
            """
        ),
        {"tenant": context.tenant_id},
    )
    if int(count or 0):
        return
    path = ROOT / "default_knowledge.json"
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    for node in data.get("nodes", []):
        await connection.execute(
            text(
                """
                INSERT INTO knowledge_nodes (
                    tenant_id, customer_id, id, type, name, properties,
                    source
                ) VALUES (
                    :tenant, 'default', :id, :type, :name,
                    CAST(:properties AS jsonb), 'builtin'
                ) ON CONFLICT DO NOTHING
                """
            ),
            {
                "tenant": context.tenant_id,
                "id": node["id"],
                "type": node["type"],
                "name": node["name"],
                "properties": json.dumps(node.get("properties", {})),
            },
        )
    for edge in data.get("edges", []):
        await connection.execute(
            text(
                """
                INSERT INTO knowledge_edges (
                    tenant_id, customer_id, source_id, target_id,
                    relationship, properties, source
                ) VALUES (
                    :tenant, 'default', :source_id, :target_id,
                    :relationship, CAST(:properties AS jsonb), 'builtin'
                )
                """
            ),
            {
                "tenant": context.tenant_id,
                "source_id": edge["source"],
                "target_id": edge["target"],
                "relationship": edge["relationship"],
                "properties": json.dumps(edge.get("properties", {})),
            },
        )


def _node(row):
    from app.dfm.knowledge_graph import KGNode

    return KGNode(
        id=row["id"],
        type=row["type"],
        name=row["name"],
        properties=row["properties"],
        customer_id=row["customer_id"],
    )


def _edge(row):
    from app.dfm.knowledge_graph import KGEdge

    return KGEdge(
        id=row["id"],
        source_id=row["source_id"],
        target_id=row["target_id"],
        relationship=row["relationship"],
        properties=row["properties"],
        customer_id=row["customer_id"],
    )


async def list_nodes(node_type: str | None, customer_id: str):
    context, transaction = await _rules_transaction()
    async with transaction as connection:
        await ensure_principal(connection, context)
        await _seed_knowledge(connection, context)
        clause = "AND type=:type" if node_type else ""
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT * FROM knowledge_nodes
                    WHERE tenant_id=:tenant AND customer_id=:customer
                    """
                    + clause
                    + " ORDER BY type, name"
                ),
                {
                    "tenant": context.tenant_id,
                    "customer": customer_id,
                    "type": node_type,
                },
            )
        ).mappings().all()
    return [_node(row) for row in rows]


async def get_node(node_id: str, customer_id: str):
    context, transaction = await _rules_transaction()
    async with transaction as connection:
        await ensure_principal(connection, context)
        await _seed_knowledge(connection, context)
        row = (
            await connection.execute(
                text(
                    """
                    SELECT * FROM knowledge_nodes
                    WHERE tenant_id=:tenant AND customer_id=:customer
                      AND id=:id
                    """
                ),
                {
                    "tenant": context.tenant_id,
                    "customer": customer_id,
                    "id": node_id,
                },
            )
        ).mappings().one_or_none()
    return _node(row) if row else None


async def get_edges(
    source_id: str | None,
    target_id: str | None,
    relationship: str | None,
    customer_id: str,
):
    context, transaction = await _rules_transaction()
    filters = []
    params = {
        "tenant": context.tenant_id,
        "customer": customer_id,
        "source_id": source_id,
        "target_id": target_id,
        "relationship": relationship,
    }
    if source_id:
        filters.append("source_id=:source_id")
    if target_id:
        filters.append("target_id=:target_id")
    if relationship:
        filters.append("relationship=:relationship")
    suffix = (" AND " + " AND ".join(filters)) if filters else ""
    async with transaction as connection:
        await ensure_principal(connection, context)
        await _seed_knowledge(connection, context)
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT * FROM knowledge_edges
                    WHERE tenant_id=:tenant AND customer_id=:customer
                    """
                    + suffix
                    + " ORDER BY id"
                ),
                params,
            )
        ).mappings().all()
    return [_edge(row) for row in rows]


async def upsert_node(node):
    context, transaction = await _rules_transaction()
    async with transaction as connection:
        await ensure_principal(connection, context)
        await _seed_knowledge(connection, context)
        await connection.execute(
            text(
                """
                INSERT INTO knowledge_nodes (
                    tenant_id, customer_id, id, type, name,
                    properties, source
                ) VALUES (
                    :tenant, :customer, :id, :type, :name,
                    CAST(:properties AS jsonb), 'user'
                )
                ON CONFLICT (tenant_id, customer_id, id)
                DO UPDATE SET type=EXCLUDED.type, name=EXCLUDED.name,
                    properties=EXCLUDED.properties, source='user'
                """
            ),
            {
                "tenant": context.tenant_id,
                "customer": node.customer_id,
                "id": node.id,
                "type": node.type,
                "name": node.name,
                "properties": json.dumps(node.properties),
            },
        )
    return node


async def add_edge(edge):
    context, transaction = await _rules_transaction()
    async with transaction as connection:
        await ensure_principal(connection, context)
        await _seed_knowledge(connection, context)
        edge.id = await connection.scalar(
            text(
                """
                INSERT INTO knowledge_edges (
                    tenant_id, customer_id, source_id, target_id,
                    relationship, properties, source
                ) VALUES (
                    :tenant, :customer, :source_id, :target_id,
                    :relationship, CAST(:properties AS jsonb), 'user'
                ) RETURNING id
                """
            ),
            {
                "tenant": context.tenant_id,
                "customer": edge.customer_id,
                "source_id": edge.source_id,
                "target_id": edge.target_id,
                "relationship": edge.relationship,
                "properties": json.dumps(edge.properties),
            },
        )
    return edge


async def delete_node(node_id: str, customer_id: str) -> bool:
    context, transaction = await _rules_transaction()
    async with transaction as connection:
        deleted = await connection.execute(
            text(
                """
                DELETE FROM knowledge_nodes
                WHERE tenant_id=:tenant AND customer_id=:customer
                  AND id=:id
                """
            ),
            {
                "tenant": context.tenant_id,
                "customer": customer_id,
                "id": node_id,
            },
        )
    return deleted.rowcount == 1


async def delete_edge(edge_id: int) -> bool:
    context, transaction = await _rules_transaction()
    async with transaction as connection:
        deleted = await connection.execute(
            text(
                "DELETE FROM knowledge_edges "
                "WHERE tenant_id=:tenant AND id=:id"
            ),
            {"tenant": context.tenant_id, "id": edge_id},
        )
    return deleted.rowcount == 1


async def clone_for_customer(customer_id: str) -> int:
    context, transaction = await _rules_transaction()
    async with transaction as connection:
        await ensure_principal(connection, context)
        await _seed_knowledge(connection, context)
        existing = await connection.scalar(
            text(
                """
                SELECT count(*) FROM knowledge_nodes
                WHERE tenant_id=:tenant AND customer_id=:customer
                """
            ),
            {"tenant": context.tenant_id, "customer": customer_id},
        )
        if int(existing or 0):
            return 0
        await connection.execute(
            text(
                """
                INSERT INTO knowledge_nodes (
                    tenant_id, customer_id, id, type, name,
                    properties, source
                )
                SELECT tenant_id, :customer, id, type, name,
                       properties, 'cloned'
                FROM knowledge_nodes
                WHERE tenant_id=:tenant AND customer_id='default'
                """
            ),
            {"tenant": context.tenant_id, "customer": customer_id},
        )
        await connection.execute(
            text(
                """
                INSERT INTO knowledge_edges (
                    tenant_id, customer_id, source_id, target_id,
                    relationship, properties, source
                )
                SELECT tenant_id, :customer, source_id, target_id,
                       relationship, properties, 'cloned'
                FROM knowledge_edges
                WHERE tenant_id=:tenant AND customer_id='default'
                """
            ),
            {"tenant": context.tenant_id, "customer": customer_id},
        )
        return int(
            await connection.scalar(
                text(
                    """
                    SELECT count(*) FROM knowledge_nodes
                    WHERE tenant_id=:tenant AND customer_id=:customer
                    """
                ),
                {"tenant": context.tenant_id, "customer": customer_id},
            )
            or 0
        )
