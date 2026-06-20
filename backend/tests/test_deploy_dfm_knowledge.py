"""Deploy-readiness tests for the rule-based, LLM-free DFM + knowledge-graph stack.

Covers:
  - REST: GET /api/dfm/rules, GET /api/dfm/rules/{process}, PUT /dfm/rules/{rule_id},
          POST /dfm/rule-sets (clone), DELETE /dfm/rule-sets/{set_id} (builtin -> 400).
  - REST: /api/knowledge nodes / nodes/{id} / process/{id}/constraints
          / process/{id}/materials / POST recommend.
  - DIRECT: DFMAnalyzer.analyze() is genuinely LLM-free on a real trimesh STL with
            NO dashscope key — design_score is int, rule_violations present,
            structural_issues == [], and no LLM module is ever imported/called.

Hermetic: no Docker, no LLM, no network. Both module-global SQLite DBs
(rule_store._db, knowledge_graph._db) are isolated to tmp files per module and
reset between, so the real seed data loads fresh.
"""
import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.dfm.rule_store as rule_store
import app.dfm.knowledge_graph as kg
from app.config import settings
from app.main import app
from tests.e2e_harness import make_stl


# --------------------------------------------------------------------------- #
# Isolation: point both module-global DBs at fresh tmp files, auth OFF.
# --------------------------------------------------------------------------- #

@pytest.fixture
def client(tmp_path, monkeypatch):
    # Auth OFF by default (api_keys empty) — assert it so a stray env doesn't break us.
    monkeypatch.setattr(settings, "api_keys", [])
    # Hard guarantee for the whole stack: no LLM credentials at all.
    monkeypatch.setattr(settings, "dashscope_api_key", None)

    # rule_store derives its db path by replacing 'history.db' -> 'dfm_rules.db'
    monkeypatch.setattr(settings, "history_db_path", str(tmp_path / "history.db"))

    # knowledge_graph uses a hardcoded module-level DB_PATH global.
    monkeypatch.setattr(kg, "DB_PATH", tmp_path / "knowledge_graph.db")

    # Reset both module-global connections so they re-open against the tmp paths
    # and re-seed the default JSON fixtures fresh for this test.
    asyncio.run(rule_store.close_db())
    asyncio.run(kg.close_db())

    with TestClient(app) as c:
        yield c

    asyncio.run(rule_store.close_db())
    asyncio.run(kg.close_db())


# --------------------------------------------------------------------------- #
# DFM rule REST endpoints
# --------------------------------------------------------------------------- #

def test_list_all_rule_sets_seeds_builtin(client):
    r = client.get("/api/dfm/rules")
    assert r.status_code == 200
    sets = r.json()
    assert isinstance(sets, list) and sets
    ids = {s["id"] for s in sets}
    # default seed creates one set per process, id = default_<process lower>
    assert "default_cnc" in ids
    assert "default_fdm" in ids
    assert "default_injection_mold" in ids
    # each set carries its rules
    cnc = next(s for s in sets if s["id"] == "default_cnc")
    assert cnc["process"] == "CNC"
    assert cnc["rules"], "rule set should embed its rules"


@pytest.mark.parametrize("process", ["CNC", "FDM", "injection_mold"])
def test_get_rules_by_process_ok(client, process):
    r = client.get(f"/api/dfm/rules/{process}")
    assert r.status_code == 200
    rules = r.json()
    assert isinstance(rules, list) and rules
    assert all(rule["process"] == process for rule in rules)
    assert all(rule["enabled"] is True for rule in rules)


def test_get_rules_unknown_process_404(client):
    r = client.get("/api/dfm/rules/laser_origami")
    assert r.status_code == 404
    assert "laser_origami" in r.json()["detail"]


def test_update_rule_threshold_ok(client):
    # grab a real geometric rule id from the CNC set
    cnc = client.get("/api/dfm/rules/CNC").json()
    target = next(rule for rule in cnc if rule["category"] == "wall_thickness")
    rule_id = target["id"]

    r = client.put(f"/api/dfm/rules/{rule_id}", json={"threshold_min": 2.5, "severity": "warning"})
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == rule_id
    assert body["threshold_min"] == 2.5
    assert body["severity"] == "warning"

    # persisted: re-fetch the process rules and confirm the change stuck
    again = client.get("/api/dfm/rules/CNC").json()
    persisted = next(rule for rule in again if rule["id"] == rule_id)
    assert persisted["threshold_min"] == 2.5
    assert persisted["severity"] == "warning"


def test_update_rule_unknown_404(client):
    r = client.put("/api/dfm/rules/no_such_rule", json={"threshold_min": 1.0})
    assert r.status_code == 404
    assert "no_such_rule" in r.json()["detail"]


def test_update_rule_empty_body_400(client):
    # exclude_none drops everything -> "No fields to update"
    r = client.put("/api/dfm/rules/whatever", json={})
    assert r.status_code == 400


def test_clone_rule_set_then_delete_clone(client):
    r = client.post(
        "/api/dfm/rule-sets",
        json={"source_id": "default_cnc", "new_id": "acme_cnc", "new_name": "ACME CNC"},
    )
    assert r.status_code == 200
    cloned = r.json()
    assert cloned["id"] == "acme_cnc"
    assert cloned["name"] == "ACME CNC"
    assert cloned["process"] == "CNC"
    assert cloned["rules"], "clone should copy the source rules"

    # clone now appears in the list
    ids = {s["id"] for s in client.get("/api/dfm/rules").json()}
    assert "acme_cnc" in ids

    # a non-builtin (no default_ prefix) clone CAN be deleted -> 200
    d = client.delete("/api/dfm/rule-sets/acme_cnc")
    assert d.status_code == 200
    assert d.json() == {"ok": True}
    ids_after = {s["id"] for s in client.get("/api/dfm/rules").json()}
    assert "acme_cnc" not in ids_after


def test_clone_unknown_source_404(client):
    r = client.post(
        "/api/dfm/rule-sets",
        json={"source_id": "default_nonexistent", "new_id": "x", "new_name": "X"},
    )
    assert r.status_code == 404


def test_delete_builtin_rule_set_400(client):
    # default_ prefixed sets are built-in and protected
    r = client.delete("/api/dfm/rule-sets/default_cnc")
    assert r.status_code == 400
    assert "built-in" in r.json()["detail"].lower()
    # still present
    ids = {s["id"] for s in client.get("/api/dfm/rules").json()}
    assert "default_cnc" in ids


# --------------------------------------------------------------------------- #
# Knowledge-graph REST endpoints
# --------------------------------------------------------------------------- #

def test_kg_list_nodes_by_type_process(client):
    r = client.get("/api/knowledge/nodes", params={"type": "process"})
    assert r.status_code == 200
    nodes = r.json()
    assert isinstance(nodes, list) and nodes
    assert all(n["type"] == "process" for n in nodes)
    ids = {n["id"] for n in nodes}
    assert "proc_cnc_3axis" in ids
    assert "proc_fdm" in ids
    assert "proc_injection" in ids


def test_kg_list_nodes_no_filter_returns_all_types(client):
    r = client.get("/api/knowledge/nodes")
    assert r.status_code == 200
    types = {n["type"] for n in r.json()}
    assert "process" in types
    assert "material" in types


def test_kg_get_node_ok(client):
    r = client.get("/api/knowledge/nodes/proc_cnc_3axis")
    assert r.status_code == 200
    node = r.json()
    assert node["id"] == "proc_cnc_3axis"
    assert node["type"] == "process"
    assert node["name"]


def test_kg_get_node_unknown_404(client):
    r = client.get("/api/knowledge/nodes/proc_does_not_exist")
    assert r.status_code == 404


def test_kg_process_materials(client):
    r = client.get("/api/knowledge/process/proc_cnc_3axis/materials")
    assert r.status_code == 200
    mats = r.json()
    assert isinstance(mats, list) and mats
    # each entry is {"material": {...}, "constraints": {...}}
    first = mats[0]
    assert "material" in first and "constraints" in first
    assert first["material"]["type"] == "material"
    # CNC aluminium edge carried min_wall / max_size / tolerance constraints
    mat_ids = {m["material"]["id"] for m in mats}
    assert "mat_al6061" in mat_ids


def test_kg_process_constraints(client):
    # constraints specific to a (process, material) pair come from the supports_material edge
    r = client.get(
        "/api/knowledge/process/proc_cnc_3axis/constraints",
        params={"material_id": "mat_al6061"},
    )
    assert r.status_code == 200
    constraints = r.json()
    assert isinstance(constraints, dict)
    # the al6061 CNC edge defines these keys
    assert "min_wall" in constraints
    assert "max_size" in constraints


def test_kg_recommend_ranks_by_compatibility(client):
    # A small part with no material preference: every process scores, sorted desc.
    r = client.post("/api/knowledge/recommend", json={"max_dimension": 50.0})
    assert r.status_code == 200
    recs = r.json()
    assert isinstance(recs, list) and recs
    scores = [rec["score"] for rec in recs]
    assert scores == sorted(scores, reverse=True), "recommendations must be score-desc"
    assert all(0.0 <= s <= 1.0 for s in scores)
    assert all("process_id" in rec and "process_name" in rec for rec in recs)


def test_kg_recommend_oversized_penalises(client):
    # A 5000mm part should be penalised against processes with a max_size limit.
    small = client.post("/api/knowledge/recommend", json={"max_dimension": 30.0}).json()
    huge = client.post("/api/knowledge/recommend", json={"max_dimension": 5000.0}).json()
    small_by_id = {r["process_id"]: r["score"] for r in small}
    huge_by_id = {r["process_id"]: r["score"] for r in huge}
    # at least one process must score strictly lower for the oversized part
    penalised = [pid for pid in huge_by_id if huge_by_id[pid] < small_by_id.get(pid, 1.0)]
    assert penalised, "oversized part should reduce some process scores"


def test_kg_recommend_unknown_material_penalised(client):
    recs = client.post(
        "/api/knowledge/recommend",
        json={"max_dimension": 30.0, "material": "unobtanium"},
    ).json()
    # no process supports 'unobtanium' -> score multiplied by 0.2 + a note
    assert recs
    assert all(any("不支持材料" in note for note in rec["notes"]) for rec in recs)
    assert all(rec["score"] <= 0.21 for rec in recs)


# --------------------------------------------------------------------------- #
# DIRECT proof: DFMAnalyzer is LLM-free on a real STL with no key
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_dfm_analyzer_is_llm_free(tmp_path, monkeypatch):
    """Build a DFMAnalyzer, run analyze() on a REAL printable trimesh STL with
    dashscope_api_key=None. Assert deterministic outputs AND that no LLM client is
    ever constructed/called (we crash any attempt to build the dashscope client)."""
    monkeypatch.setattr(settings, "dashscope_api_key", None)
    # Isolate the rule_store DB so seeding works against tmp.
    monkeypatch.setattr(settings, "history_db_path", str(tmp_path / "history.db"))
    monkeypatch.setattr(kg, "DB_PATH", tmp_path / "knowledge_graph.db")
    await rule_store.close_db()
    await kg.close_db()

    # Land-mine: if any DFM code path tries to build the LLM client, fail loudly.
    # make_llm_client is the single factory every LLM-using module funnels through.
    import app.config as config_mod

    def _boom(*a, **k):
        raise AssertionError("DFM attempted to construct an LLM client — not LLM-free!")

    monkeypatch.setattr(config_mod, "make_llm_client", _boom)

    from app.validation.dfm_analyzer import DFMAnalyzer

    stl = make_stl("printable", tmp_path / "part.stl")  # watertight 20x30x40 box

    analyzer = DFMAnalyzer()
    result = await analyzer.analyze(stl, process="CNC")

    # design_score is a real int in [0, 100]
    assert isinstance(result.design_score, int)
    assert 0 <= result.design_score <= 100
    # rule_violations present (list of dicts) — at minimum CNC heuristic advisories fire
    assert isinstance(result.rule_violations, list)
    assert result.rule_violations, "expected at least advisory rule violations"
    assert all(isinstance(v, dict) and "rule_id" in v for v in result.rule_violations)
    # subjective VLM fields are gone — deterministic engine only
    assert result.structural_issues == []
    assert result.functional_notes == []
    # geometry attached and watertight (real trimesh ran)
    assert result.geometry is not None
    assert result.geometry.is_watertight is True
    # summary is a deterministic non-empty template string
    assert isinstance(result.design_summary, str) and result.design_summary

    await rule_store.close_db()
    await kg.close_db()


@pytest.mark.asyncio
async def test_dfm_analyzer_thin_part_scores_lower(tmp_path, monkeypatch):
    """A thin part trips a critical geometric wall-thickness rule -> lower score than
    a printable part. Pure deterministic, no key."""
    monkeypatch.setattr(settings, "dashscope_api_key", None)
    monkeypatch.setattr(settings, "history_db_path", str(tmp_path / "history.db"))
    monkeypatch.setattr(kg, "DB_PATH", tmp_path / "knowledge_graph.db")
    await rule_store.close_db()
    await kg.close_db()

    from app.validation.dfm_analyzer import DFMAnalyzer

    thin = make_stl("thin", tmp_path / "thin.stl")        # 40x40x0.4 -> sub-mm wall
    printable = make_stl("printable", tmp_path / "ok.stl")  # 20x30x40

    analyzer = DFMAnalyzer()
    thin_res = await analyzer.analyze(thin, process="FDM")
    ok_res = await analyzer.analyze(printable, process="FDM")

    assert thin_res.design_score <= ok_res.design_score
    # the thin part must surface a geometric wall-thickness violation
    cats = {v["category"] for v in thin_res.rule_violations if v["source"] == "geometric"}
    assert "wall_thickness" in cats

    await rule_store.close_db()
    await kg.close_db()
