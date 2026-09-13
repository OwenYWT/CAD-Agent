"""Small, evidence-based browser/Agent projection of verified kernel state."""
from __future__ import annotations

from typing import Any
from uuid import UUID, uuid5
import hashlib
import json
import math


def measured_topology_bindings(obj: dict, revision_id: UUID | str | None) -> list[dict]:
    """Only publish unique selectors supported by complete kernel measurements."""
    # Origin/datum objects expose visualization shapes (including effectively
    # infinite planes), not physical Face/Edge references on model geometry.
    if revision_id is None or obj.get("type_id") in {
        "App::Plane", "App::Line", "App::Point", "App::Origin",
        "PartDesign::Plane", "PartDesign::Line", "PartDesign::Point", "PartDesign::CoordinateSystem",
    }:
        return []
    topology = (obj.get("inspection") or {}).get("topology") or {}
    items = topology.get("items") or []
    # Older projections did not separately record face/edge counts. Completeness
    # of their combined inventory is still verifiable from the recorded total.
    complete = topology.get("total", -1) == len(items)
    faces = [f for f in items if f.get("kind") == "face"]
    if not complete and topology.get("total_faces", -1) != len(faces):
        return []
    planes = [f for f in faces if f.get("geometry_type") == "Part::GeomPlane"
              or (not f.get("geometry_type") and f.get("geometry") == "Plane")]
    if any(f.get("status") == "unavailable" for f in faces):
        planes = []
    base = {"schema_version": "topology-selector.v1", "backend": "freecad",
            "revision_id": str(revision_id), "object_name": obj["name"], "tolerance_mm": 1e-5}
    result = []
    for index, axis in enumerate("xyz"):
        candidates = [f for f in planes if len(f.get("normal", [])) == 3 and len(f.get("center", [])) == 3
                      and abs(abs(f["normal"][index]) - 1) <= 1e-5]
        for extreme in ("min", "max"):
            if not candidates:
                continue
            position = (min if extreme == "min" else max)(f["center"][index] for f in candidates)
            if sum(abs(f["center"][index] - position) <= 1e-5 for f in candidates) == 1:
                result.append({**base, "subelement_kind": "face", "geometry": "planar", "axis": axis, "extreme": extreme})
    edges = [e for e in items if e.get("kind") == "edge"]
    if (complete or topology.get("total_edges", -1) == len(edges)) and not any(e.get("status") == "unavailable" for e in edges):
        circles = [e for e in edges if e.get("geometry_type") == "Part::GeomCircle"
                   and len(e.get("curve_center", [])) == 3 and len(e.get("curve_axis", [])) == 3
                   and isinstance(e.get("radius_mm"), (int, float))]
        for edge in circles:
            axis = next((i for i in range(3) if abs(abs(edge["curve_axis"][i]) - 1) <= 1e-5), None)
            if axis is None:
                continue
            matching = [other for other in circles if abs(other["radius_mm"] - edge["radius_mm"]) <= 1e-5
                        and abs(abs(other["curve_axis"][axis]) - 1) <= 1e-5
                        and math.dist(other["curve_center"], edge["curve_center"]) <= 1e-5]
            if len(matching) == 1:
                result.append({**base, "subelement_kind": "edge", "geometry": "circular", "axis": "xyz"[axis],
                               "extreme": "max", "radius_mm": edge["radius_mm"],
                               "center": dict(zip("xyz", edge["curve_center"]))})
    return result[:32]


def feature_id(document_id: UUID, kernel_name: str) -> str:
    # The allowlisted kernel protocol cannot rename/reuse object names.
    return str(uuid5(document_id, f"freecad-object:{kernel_name}"))


def project_semantic_state(document_id: UUID, state: dict[str, Any], *,
                           revision_id: UUID | str | None = None, previous: dict | None = None) -> dict[str, Any]:
    objects = state.get("objects", [])
    names = {obj["name"] for obj in objects}
    hierarchy_known = bool(objects) and all(obj.get("structure", {}).get("status") == "measured" for obj in objects)
    parents = {name: [] for name in names}
    if hierarchy_known:
        for obj in objects:
            for member in obj["structure"].get("members", []):
                if member in parents:
                    parents[member].append(obj["name"])
                else:
                    hierarchy_known = False
    parameters: dict[str, list] = {}
    for parameter in state.get("parameters", []):
        parameters.setdefault(parameter["object_name"], []).append(parameter)
    features = []
    prior = {f["id"]: f for f in (previous or {}).get("features", [])}
    for obj in objects:
        name = obj["name"]
        feature = {
            "id": feature_id(document_id, name), "kernel_name": name,
            "label": obj.get("label", name), "type": obj.get("type_id"),
            # Intent/role cannot be inferred reliably from shape or filename.
            "intent": None, "role": None,
            "dependencies": [feature_id(document_id, n) for n in obj.get("out", []) if n in names],
            "parameters": parameters.get(name, []),
            "is_valid": obj.get("is_valid"), "shape": obj.get("shape"),
            "geometry_sha256": obj.get("geometry_sha256"),
            "geometry_fingerprint_kind": obj.get("geometry_fingerprint_kind"),
            "global_placement": obj.get("global_placement"),
            "sketch_constraints_sha256": obj.get("sketch_constraints_sha256"),
            "instance": obj.get("instance"),
            "sketch": obj.get("sketch"),
        }
        content = dict(feature)
        if feature.get("geometry_sha256"):
            # New inspector measurements must not manufacture a feature edit.
            content.pop("shape")
        digest = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        old = prior.get(feature["id"])
        feature.update(content_sha256=digest,
            revision_created=old.get("revision_created") if old else str(revision_id) if revision_id and previous is not None else None,
            last_modified=old.get("last_modified") if old and old.get("content_sha256") == digest else str(revision_id) if revision_id else None,
            topology_bindings=measured_topology_bindings(obj, revision_id))
        # Presentation metadata is outside the established feature fingerprint:
        # a projector upgrade cannot manufacture edits to historical geometry.
        structure = obj.get("structure", {})
        feature["structure"] = {"category": structure.get("category"),
            "container_ids": [feature_id(document_id, n) for n in parents[name]],
            "member_ids": [feature_id(document_id, n) for n in structure.get("members", []) if n in names],
            "body_tip_id": feature_id(document_id, structure["body_tip"]) if structure.get("body_tip") in names else None} if hierarchy_known else None
        features.append(feature)
    return {"schema_version": "cad-semantic-state.v1", "features": features,
            "hierarchy_status": "measured" if hierarchy_known else "unavailable",
            "roots": [feature_id(document_id, n) for n in state.get("root_objects", []) if n in names]}


def semantic_delta(before: dict, after: dict) -> dict:
    old = {f["id"]: f for f in before.get("features", [])}
    new = {f["id"]: f for f in after.get("features", [])}
    return {"upserted": [f for key, f in new.items() if old.get(key) != f],
            "removed": sorted(old.keys() - new.keys()), "roots": after.get("roots", [])}


def bounded_agent_context(state: dict, *, target_names: list[str] | None = None) -> dict:
    """L0 inventory and L1 selected features; report omissions explicitly.

    No full property/shape dumps, topology or binary data go into the prompt.
    The trusted full state is retained for validation and deterministic edits.
    """
    objects = state.get("objects", [])
    by_name = {obj["name"]: obj for obj in objects}
    selection = state.get("selection_context")
    requested = list(dict.fromkeys(target_names or
        [f["kernel_name"] for f in (selection or {}).get("features", [])]))
    selected = list(requested)
    for name in requested:
        selected.extend(by_name.get(name, {}).get("out", []))
    if not selected:
        selected = [obj["name"] for obj in objects[-16:]]
    selected = list(dict.fromkeys(selected))[:32]
    params = state.get("parameters", [])
    annotations = {a["kernel_name"]: a for a in state.get("feature_annotations", [])}
    roots = [o for o in objects if o["name"] in state.get("root_objects", [])]
    bounds = [(o.get("shape") or {}).get("bounds_mm") for o in roots if o.get("shape")]
    dimensions = None
    if bounds and all(b and len(b.get("min", [])) == 3 and len(b.get("max", [])) == 3 for b in bounds):
        dimensions = {axis: max(b["max"][i] for b in bounds) - min(b["min"][i] for b in bounds) for i, axis in enumerate("xyz")}
    dfm = state.get("dfm_summary") or {"status": "unavailable", "reason": "no_verified_dfm_evidence_for_revision"}
    return {
        "schema_version": "cad-agent-context.v1",
        "summary": {"document": state.get("document"), "object_count": len(objects),
                    "inventory": [{"name": o["name"], "type_id": o.get("type_id"),
                                   "label": str(o.get("label", o["name"]))[:160]} for o in objects[:80]],
                    "omitted_inventory_count": max(0, len(objects)-80),
                    "revision_id": state.get("revision_id"), "main_dimensions_mm": dimensions,
                    "dimensions_status": "measured" if dimensions is not None else "unavailable",
                    "key_features": [{"name": o["name"], "type_id": o.get("type_id"),
                                      "parameters": [p for p in params if p["object_name"] == o["name"]][:8]}
                                     for o in objects if str(o.get("type_id", "")).startswith("PartDesign::")][-24:],
                    "dfm": {**{k: v for k, v in dfm.items() if k in {"status", "revision_id", "evidence_id", "reason", "policy_hash"}},
                            "issues": list(dfm.get("issues") or [])[:12], "violations": list(dfm.get("violations") or [])[:12]}},
        "features": [{"name": name, "type_id": by_name[name].get("type_id"),
                      "user_role": annotations.get(name, {}).get("role"),
                      "user_intent": str(annotations.get(name, {}).get("intent") or "")[:500] or None,
                      "annotation_version": annotations.get(name, {}).get("version", 0),
                      "dependencies": by_name[name].get("out", [])[:32],
                      "parameters": [p for p in params if p["object_name"] == name][:24],
                      "is_valid": by_name[name].get("is_valid")}
                     for name in selected if name in by_name],
        "omitted_detail_count": max(0, len(objects)-len(set(selected) & by_name.keys())),
        **({"selection_context": selection} if selection else {}),
        **({'engineering_evidence':state['engineering_evidence'][:4]} if state.get('engineering_evidence') else {}),
    }
