"""SQLite-backed DFM rule storage with default seed data."""

import asyncio
import json
import logging
from pathlib import Path

import aiosqlite

from app.config import settings
from app.dfm.models import DFMRule, DFMRuleSet

logger = logging.getLogger(__name__)

_db: aiosqlite.Connection | None = None
_lock = asyncio.Lock()

_DEFAULT_RULES_PATH = Path(__file__).parent / "default_rules.json"


async def _get_db() -> aiosqlite.Connection:
    global _db
    if _db is not None:
        return _db

    async with _lock:
        if _db is not None:
            return _db

        db_path = settings.history_db_path.replace("history.db", "dfm_rules.db")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        _db = await aiosqlite.connect(db_path)
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")

        await _db.executescript("""
            CREATE TABLE IF NOT EXISTS rule_sets (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                process TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS rules (
                id TEXT PRIMARY KEY,
                rule_set_id TEXT NOT NULL REFERENCES rule_sets(id),
                process TEXT NOT NULL,
                category TEXT NOT NULL,
                check_type TEXT NOT NULL DEFAULT 'geometric',
                threshold_min REAL,
                threshold_max REAL,
                unit TEXT DEFAULT 'mm',
                severity TEXT DEFAULT 'warning',
                description TEXT DEFAULT '',
                suggestion_template TEXT DEFAULT '',
                enabled INTEGER DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_rules_set ON rules(rule_set_id);
            CREATE INDEX IF NOT EXISTS idx_rules_process ON rules(process);
        """)
        await _db.commit()

        # Seed defaults if empty
        cursor = await _db.execute("SELECT COUNT(*) FROM rule_sets")
        row = await cursor.fetchone()
        if row[0] == 0:
            await _seed_defaults()

        return _db


async def _seed_defaults():
    """Load default rules from JSON and insert into database."""
    db = _db
    if db is None:
        return

    rules_data = json.loads(_DEFAULT_RULES_PATH.read_text(encoding="utf-8"))

    # Group by process
    by_process: dict[str, list[dict]] = {}
    for r in rules_data:
        by_process.setdefault(r["process"], []).append(r)

    for process, rules in by_process.items():
        set_id = f"default_{process.lower()}"
        await db.execute(
            "INSERT INTO rule_sets (id, name, process) VALUES (?, ?, ?)",
            (set_id, f"{process} 默认规则", process),
        )
        for r in rules:
            await db.execute(
                """INSERT INTO rules
                   (id, rule_set_id, process, category, check_type,
                    threshold_min, threshold_max, unit, severity,
                    description, suggestion_template, enabled)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (
                    r["id"], set_id, r["process"], r["category"],
                    r.get("check_type", "geometric"),
                    r.get("threshold_min"), r.get("threshold_max"),
                    r.get("unit", "mm"), r.get("severity", "warning"),
                    r.get("description", ""), r.get("suggestion_template", ""),
                ),
            )
    await db.commit()
    logger.info(f"Seeded {len(rules_data)} default DFM rules across {len(by_process)} processes")


async def list_rule_sets() -> list[DFMRuleSet]:
    db = await _get_db()
    cursor = await db.execute("SELECT id, name, process FROM rule_sets ORDER BY process")
    rows = await cursor.fetchall()
    result = []
    for row in rows:
        rules = await get_rules_for_set(row["id"])
        result.append(DFMRuleSet(id=row["id"], name=row["name"], process=row["process"], rules=rules))
    return result


async def get_rules_for_set(rule_set_id: str) -> list[DFMRule]:
    db = await _get_db()
    cursor = await db.execute(
        "SELECT * FROM rules WHERE rule_set_id = ? ORDER BY category, severity",
        (rule_set_id,),
    )
    rows = await cursor.fetchall()
    return [_row_to_rule(r) for r in rows]


async def get_rules_by_process(process: str) -> list[DFMRule]:
    """Get all enabled rules for a process (across all rule sets)."""
    db = await _get_db()
    cursor = await db.execute(
        "SELECT * FROM rules WHERE process = ? AND enabled = 1 ORDER BY category, severity",
        (process,),
    )
    rows = await cursor.fetchall()
    return [_row_to_rule(r) for r in rows]


async def get_all_enabled_rules() -> list[DFMRule]:
    """Get all enabled rules across all processes."""
    db = await _get_db()
    cursor = await db.execute(
        "SELECT * FROM rules WHERE enabled = 1 ORDER BY process, category, severity"
    )
    rows = await cursor.fetchall()
    return [_row_to_rule(r) for r in rows]


async def update_rule(rule_id: str, updates: dict) -> DFMRule | None:
    db = await _get_db()
    allowed = {"threshold_min", "threshold_max", "severity", "enabled", "description", "suggestion_template"}
    filtered = {k: v for k, v in updates.items() if k in allowed}
    if not filtered:
        return None

    sets = ", ".join(f"{k} = ?" for k in filtered)
    values = list(filtered.values()) + [rule_id]
    await db.execute(f"UPDATE rules SET {sets} WHERE id = ?", values)
    await db.commit()

    cursor = await db.execute("SELECT * FROM rules WHERE id = ?", (rule_id,))
    row = await cursor.fetchone()
    return _row_to_rule(row) if row else None


async def clone_rule_set(source_id: str, new_id: str, new_name: str) -> DFMRuleSet | None:
    db = await _get_db()
    cursor = await db.execute("SELECT * FROM rule_sets WHERE id = ?", (source_id,))
    src = await cursor.fetchone()
    if not src:
        return None

    await db.execute(
        "INSERT INTO rule_sets (id, name, process) VALUES (?, ?, ?)",
        (new_id, new_name, src["process"]),
    )

    cursor = await db.execute("SELECT * FROM rules WHERE rule_set_id = ?", (source_id,))
    rows = await cursor.fetchall()
    for r in rows:
        new_rule_id = r["id"].replace(source_id, new_id) if source_id in r["id"] else f"{new_id}_{r['id']}"
        await db.execute(
            """INSERT INTO rules
               (id, rule_set_id, process, category, check_type,
                threshold_min, threshold_max, unit, severity,
                description, suggestion_template, enabled)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_rule_id, new_id, r["process"], r["category"],
                r["check_type"], r["threshold_min"], r["threshold_max"],
                r["unit"], r["severity"], r["description"],
                r["suggestion_template"], r["enabled"],
            ),
        )
    await db.commit()
    rules = await get_rules_for_set(new_id)
    return DFMRuleSet(id=new_id, name=new_name, process=src["process"], rules=rules)


async def delete_rule_set(set_id: str) -> bool:
    if set_id.startswith("default_"):
        return False  # Cannot delete built-in sets
    db = await _get_db()
    await db.execute("DELETE FROM rules WHERE rule_set_id = ?", (set_id,))
    await db.execute("DELETE FROM rule_sets WHERE id = ?", (set_id,))
    await db.commit()
    return True


async def close_db():
    global _db
    if _db:
        await _db.close()
        _db = None


def _row_to_rule(row) -> DFMRule:
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
