"""Process knowledge graph — SQLite-backed store for process/material/constraint relationships."""

import asyncio
import json
import logging
from pathlib import Path

import aiosqlite
from pydantic import BaseModel

logger = logging.getLogger(__name__)

DB_PATH = Path("./data/knowledge_graph.db")
_db: aiosqlite.Connection | None = None
_init_lock = asyncio.Lock()

DEFAULT_KNOWLEDGE_PATH = Path(__file__).parent / "default_knowledge.json"


# --- Models ---


class KGNode(BaseModel):
    id: str
    type: str  # "process" | "material" | "constraint" | "supplier"
    name: str
    properties: dict = {}
    customer_id: str = "default"


class KGEdge(BaseModel):
    id: int | None = None
    source_id: str
    target_id: str
    relationship: str  # "supports_material" | "has_constraint" | "has_capability"
    properties: dict = {}
    customer_id: str = "default"


class ProcessRecommendation(BaseModel):
    process_id: str
    process_name: str
    material_id: str | None = None
    material_name: str | None = None
    constraints: dict = {}
    score: float = 0.0  # 0-1 compatibility score
    notes: list[str] = []


# --- DB Helpers ---


async def _get_db() -> aiosqlite.Connection:
    global _db
    if _db is not None:
        return _db

    async with _init_lock:
        if _db is not None:
            return _db

        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _db = await aiosqlite.connect(str(DB_PATH))
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")

        await _db.executescript("""
            CREATE TABLE IF NOT EXISTS kg_nodes (
                id TEXT NOT NULL,
                type TEXT NOT NULL,
                name TEXT NOT NULL,
                properties TEXT DEFAULT '{}',
                customer_id TEXT DEFAULT 'default',
                PRIMARY KEY (id, customer_id)
            );
            CREATE TABLE IF NOT EXISTS kg_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                relationship TEXT NOT NULL,
                properties TEXT DEFAULT '{}',
                customer_id TEXT DEFAULT 'default'
            );
            CREATE INDEX IF NOT EXISTS idx_edges_source ON kg_edges(source_id, customer_id);
            CREATE INDEX IF NOT EXISTS idx_edges_target ON kg_edges(target_id, customer_id);
            CREATE INDEX IF NOT EXISTS idx_edges_rel ON kg_edges(relationship, customer_id);
            CREATE INDEX IF NOT EXISTS idx_nodes_type ON kg_nodes(type, customer_id);
        """)
        await _db.commit()

        # Seed defaults if empty
        row = await _db.execute_fetchall(
            "SELECT COUNT(*) as cnt FROM kg_nodes WHERE customer_id='default'"
        )
        if row[0][0] == 0:
            await _seed_defaults()

    return _db


async def _seed_defaults():
    """Load default knowledge graph from JSON."""
    if not DEFAULT_KNOWLEDGE_PATH.exists():
        logger.warning("Default knowledge file not found")
        return

    data = json.loads(DEFAULT_KNOWLEDGE_PATH.read_text())
    db = _db
    assert db is not None

    for node in data.get("nodes", []):
        await db.execute(
            "INSERT OR IGNORE INTO kg_nodes (id, type, name, properties, customer_id) VALUES (?,?,?,?,?)",
            (node["id"], node["type"], node["name"], json.dumps(node.get("properties", {})), "default"),
        )

    for edge in data.get("edges", []):
        await db.execute(
            "INSERT INTO kg_edges (source_id, target_id, relationship, properties, customer_id) VALUES (?,?,?,?,?)",
            (edge["source"], edge["target"], edge["relationship"], json.dumps(edge.get("properties", {})), "default"),
        )

    await db.commit()
    logger.info("Seeded default knowledge graph")


# --- Query Functions ---


async def list_nodes(
    node_type: str | None = None,
    customer_id: str = "default",
) -> list[KGNode]:
    db = await _get_db()
    if node_type:
        rows = await db.execute_fetchall(
            "SELECT * FROM kg_nodes WHERE type=? AND customer_id=?",
            (node_type, customer_id),
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT * FROM kg_nodes WHERE customer_id=?",
            (customer_id,),
        )
    return [
        KGNode(
            id=r["id"], type=r["type"], name=r["name"],
            properties=json.loads(r["properties"]),
            customer_id=r["customer_id"],
        )
        for r in rows
    ]


async def get_node(node_id: str, customer_id: str = "default") -> KGNode | None:
    db = await _get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM kg_nodes WHERE id=? AND customer_id=?",
        (node_id, customer_id),
    )
    if not rows:
        return None
    r = rows[0]
    return KGNode(
        id=r["id"], type=r["type"], name=r["name"],
        properties=json.loads(r["properties"]),
        customer_id=r["customer_id"],
    )


async def get_edges(
    source_id: str | None = None,
    target_id: str | None = None,
    relationship: str | None = None,
    customer_id: str = "default",
) -> list[KGEdge]:
    db = await _get_db()
    conditions = ["customer_id=?"]
    params: list = [customer_id]
    if source_id:
        conditions.append("source_id=?")
        params.append(source_id)
    if target_id:
        conditions.append("target_id=?")
        params.append(target_id)
    if relationship:
        conditions.append("relationship=?")
        params.append(relationship)

    where = " AND ".join(conditions)
    rows = await db.execute_fetchall(
        f"SELECT * FROM kg_edges WHERE {where}", params,
    )
    return [
        KGEdge(
            id=r["id"], source_id=r["source_id"], target_id=r["target_id"],
            relationship=r["relationship"],
            properties=json.loads(r["properties"]),
            customer_id=r["customer_id"],
        )
        for r in rows
    ]


async def get_process_constraints(
    process_id: str,
    material_id: str | None = None,
    customer_id: str = "default",
) -> dict:
    """Get constraints for a process, optionally specific to a material."""
    # Base process constraints
    edges = await get_edges(
        source_id=process_id, relationship="has_constraint",
        customer_id=customer_id,
    )
    constraints: dict = {}
    for e in edges:
        constraints.update(e.properties)

    # Material-specific overrides
    if material_id:
        mat_edges = await get_edges(
            source_id=process_id, target_id=material_id,
            relationship="supports_material",
            customer_id=customer_id,
        )
        for e in mat_edges:
            constraints.update(e.properties)

    return constraints


async def get_supported_materials(
    process_id: str,
    customer_id: str = "default",
) -> list[dict]:
    """Get all materials supported by a process."""
    edges = await get_edges(
        source_id=process_id, relationship="supports_material",
        customer_id=customer_id,
    )
    results = []
    for e in edges:
        mat = await get_node(e.target_id, customer_id)
        if mat:
            results.append({
                "material": mat.model_dump(),
                "constraints": e.properties,
            })
    return results


async def recommend_processes(
    max_dimension: float | None = None,
    material_preference: str | None = None,
    customer_id: str = "default",
) -> list[ProcessRecommendation]:
    """Recommend processes based on part characteristics."""
    processes = await list_nodes("process", customer_id)
    recommendations = []

    for proc in processes:
        constraints = await get_process_constraints(proc.id, customer_id=customer_id)
        notes = []
        score = 1.0

        # Check size constraint
        proc_max_size = constraints.get("max_size")
        if max_dimension and proc_max_size:
            if max_dimension > proc_max_size:
                score *= 0.1
                notes.append(f"尺寸 {max_dimension:.0f}mm 超过工艺限制 {proc_max_size}mm")
            elif max_dimension > proc_max_size * 0.8:
                score *= 0.7
                notes.append(f"接近尺寸上限 ({max_dimension:.0f}/{proc_max_size}mm)")

        # Check material compatibility
        mat_node = None
        mat_constraints = {}
        if material_preference:
            mat_edges = await get_edges(
                source_id=proc.id, relationship="supports_material",
                customer_id=customer_id,
            )
            for e in mat_edges:
                mat = await get_node(e.target_id, customer_id)
                if mat and material_preference.lower() in mat.name.lower():
                    mat_node = mat
                    mat_constraints = e.properties
                    break

            if not mat_node:
                score *= 0.2
                notes.append(f"不支持材料: {material_preference}")

        recommendations.append(ProcessRecommendation(
            process_id=proc.id,
            process_name=proc.name,
            material_id=mat_node.id if mat_node else None,
            material_name=mat_node.name if mat_node else None,
            constraints={**constraints, **mat_constraints},
            score=round(score, 2),
            notes=notes,
        ))

    recommendations.sort(key=lambda r: r.score, reverse=True)
    return recommendations


# --- Mutation Functions ---


async def upsert_node(node: KGNode) -> KGNode:
    db = await _get_db()
    await db.execute(
        """INSERT INTO kg_nodes (id, type, name, properties, customer_id)
           VALUES (?,?,?,?,?)
           ON CONFLICT(id, customer_id)
           DO UPDATE SET name=excluded.name, type=excluded.type, properties=excluded.properties""",
        (node.id, node.type, node.name, json.dumps(node.properties), node.customer_id),
    )
    await db.commit()
    return node


async def add_edge(edge: KGEdge) -> KGEdge:
    db = await _get_db()
    cursor = await db.execute(
        "INSERT INTO kg_edges (source_id, target_id, relationship, properties, customer_id) VALUES (?,?,?,?,?)",
        (edge.source_id, edge.target_id, edge.relationship, json.dumps(edge.properties), edge.customer_id),
    )
    await db.commit()
    edge.id = cursor.lastrowid
    return edge


async def delete_node(node_id: str, customer_id: str = "default") -> bool:
    db = await _get_db()
    await db.execute(
        "DELETE FROM kg_nodes WHERE id=? AND customer_id=?",
        (node_id, customer_id),
    )
    await db.execute(
        "DELETE FROM kg_edges WHERE (source_id=? OR target_id=?) AND customer_id=?",
        (node_id, node_id, customer_id),
    )
    await db.commit()
    return True


async def delete_edge(edge_id: int) -> bool:
    db = await _get_db()
    await db.execute("DELETE FROM kg_edges WHERE id=?", (edge_id,))
    await db.commit()
    return True


async def clone_for_customer(customer_id: str) -> int:
    """Clone default knowledge graph for a customer."""
    db = await _get_db()
    # Check if customer already has nodes
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM kg_nodes WHERE customer_id=?", (customer_id,)
    )
    if rows[0][0] > 0:
        return 0

    # Clone nodes
    await db.execute(
        """INSERT INTO kg_nodes (id, type, name, properties, customer_id)
           SELECT id, type, name, properties, ? FROM kg_nodes WHERE customer_id='default'""",
        (customer_id,),
    )

    # Clone edges
    await db.execute(
        """INSERT INTO kg_edges (source_id, target_id, relationship, properties, customer_id)
           SELECT source_id, target_id, relationship, properties, ? FROM kg_edges WHERE customer_id='default'""",
        (customer_id,),
    )

    await db.commit()
    count = (await db.execute_fetchall(
        "SELECT COUNT(*) FROM kg_nodes WHERE customer_id=?", (customer_id,)
    ))[0][0]
    logger.info(f"Cloned knowledge graph for customer {customer_id}: {count} nodes")
    return count


async def close_db():
    global _db
    if _db:
        await _db.close()
        _db = None
