"""Real Autodesk Fusion API facade.

This is the only production business module that calls model APIs from
``adsk``.  Every method is invoked by ``Dispatcher`` from Fusion's CustomEvent
(main) thread; the lifecycle entry point only owns event registration.
"""

from __future__ import annotations

import hashlib
import struct
import uuid
from pathlib import Path
from typing import Any

try:
    import adsk.core
    import adsk.fusion
except ImportError:  # Allows protocol/unit tests outside Fusion Desktop.
    adsk = None

from .config import ConnectorConfig
from .dispatcher import CancelToken, DispatchError

MAX_CONTEXT_ENTITIES = 5_000


class FusionApiFacade:
    def __init__(self, app: Any, ui: Any, config: ConnectorConfig):
        self.app = app
        self.ui = ui
        self.config = config
        self._created_by_request: dict[str, Any] = {}

    @staticmethod
    def registration(app: Any, config: ConnectorConfig, addin_version: str) -> dict[str, Any]:
        user = getattr(app, "currentUser", None)
        return {
            "connector_instance_id": config.connector_instance_id,
            "protocol_versions": [1],
            "fusion_version": str(app.version),
            "addin_version": addin_version,
            "platform": config.platform_name,
            "user_name": getattr(user, "displayName", None),
            "artifact_root_fingerprint": config.artifact_root_fingerprint,
            "capabilities": {
                "adapter": "fusion360",
                "contract_version": "1.0.0",
                "protocol_versions": [1],
                "available": True,
                "runtime_online": True,
                "connector_online": True,
                "fusion_running": True,
                "fusion_version": str(app.version),
                "actions": [
                    "cad.update_parameter", "cad.update_feature_parameter", "cad.create_sketch",
                    "cad.create_extrude", "cad.create_hole", "cad.create_fillet",
                    "cad.create_chamfer", "cad.update_entity_properties", "cad.save_document",
                    "cad.save_as", "cad.export",
                ],
                "context_sections": [
                    "application", "document", "design", "components", "occurrences",
                    "selection", "parameters", "materials", "mass_properties", "sketches",
                    "features", "timeline", "bodies", "assembly", "cloud",
                ],
                "limitations": [
                    "Fusion API calls execute serially on Fusion's main thread",
                    "DXF uses the released legacy Sketch.saveAsDXF unless preview APIs are explicitly enabled",
                    "Cloud save completion can remain pending after local acceptance",
                ],
            },
        }

    def get_context(self, request: dict[str, Any], cancel: CancelToken) -> dict[str, Any]:
        document, design = self._active()
        query = request["query"]
        sections = set(query["sections"])
        max_depth = query.get("max_depth", 8)
        include_suppressed = query.get("include_suppressed", False)
        data: dict[str, Any] = {
            "components": [], "occurrences": [], "selection": [], "parameters": [],
            "materials": [], "mass_properties": [], "sketches": [], "features": [],
            "timeline": [], "bodies": [], "assembly": [], "truncated": False,
            "vendor_extensions": {},
        }
        warnings: list[str] = []
        if "application" in sections:
            user = getattr(self.app, "currentUser", None)
            data["application"] = {
                "name": "Autodesk Fusion", "version": str(self.app.version),
                "language": str(getattr(self.app, "language", "")) or None,
                "user_name": getattr(user, "displayName", None),
            }
        if "document" in sections:
            data_file = getattr(document, "dataFile", None) if document.isSaved else None
            data["document"] = {
                "document_id": self._document_id(document), "name": document.name,
                "document_type": str(document.objectType), "is_saved": bool(document.isSaved),
                "is_modified": bool(document.isModified),
                "is_read_only": bool(getattr(data_file, "isReadOnly", False)),
            }
        if "design" in sections:
            data["design"] = {
                "design_type": self._design_type(design),
                "root_component_id": design.rootComponent.entityToken,
                "units": design.unitsManager.defaultLengthUnits,
            }
        cancel.check()
        components = self._items(design.allComponents, MAX_CONTEXT_ENTITIES)
        if design.allComponents.count > MAX_CONTEXT_ENTITIES:
            data["truncated"] = True
            warnings.append("Component traversal was truncated at the configured limit")
        if "components" in sections:
            data["components"] = [self._entity_summary(c, "component") for c in components]
        if "occurrences" in sections or "assembly" in sections:
            occurrences = self._occurrence_summaries(design.rootComponent, max_depth, include_suppressed)
            if len(occurrences) >= MAX_CONTEXT_ENTITIES:
                data["truncated"] = True
                warnings.append("Occurrence traversal reached the configured limit")
            if "occurrences" in sections:
                data["occurrences"] = occurrences
            if "assembly" in sections:
                data["assembly"] = occurrences
        cancel.check()
        if "selection" in sections:
            selections = self.ui.activeSelections
            data["selection"] = [
                self._entity_summary(
                    selections.item(i).entity,
                    self._selection_kind(selections.item(i).entity),
                )
                for i in range(min(selections.count, MAX_CONTEXT_ENTITIES))
            ]
            if selections.count > MAX_CONTEXT_ENTITIES:
                data["truncated"] = True
                warnings.append("Selection traversal was truncated at the configured limit")
        if "parameters" in sections:
            parameters = self._items(design.allParameters, MAX_CONTEXT_ENTITIES)
            data["parameters"] = [
                self._parameter_summary(p, design.rootComponent.entityToken) for p in parameters
            ]
            if design.allParameters.count > MAX_CONTEXT_ENTITIES:
                data["truncated"] = True
                warnings.append("Parameter traversal was truncated at the configured limit")
        if "materials" in sections:
            data["materials"] = self._material_summaries(components)
        if "mass_properties" in sections:
            data["mass_properties"] = [self._mass_summary(c) for c in components]
        if "sketches" in sections:
            data["sketches"] = self._collection_summaries(components, "sketches", "sketch")
            if len(data["sketches"]) >= MAX_CONTEXT_ENTITIES:
                data["truncated"] = True
                warnings.append("Sketch traversal reached the configured limit")
        if "bodies" in sections:
            data["bodies"] = self._collection_summaries(components, "bRepBodies", "body")
            if len(data["bodies"]) >= MAX_CONTEXT_ENTITIES:
                data["truncated"] = True
                warnings.append("Body traversal reached the configured limit")
        if "features" in sections:
            data["features"] = self._feature_summaries(components)
            if len(data["features"]) >= MAX_CONTEXT_ENTITIES:
                data["truncated"] = True
                warnings.append("Feature traversal reached the configured limit")
        if "timeline" in sections:
            timeline = getattr(design, "timeline", None)
            if timeline is not None:
                data["timeline"], timeline_truncated = self._timeline_summaries(timeline)
                if timeline_truncated:
                    data["truncated"] = True
                    warnings.append("Timeline traversal was truncated at the configured limit")
        if "cloud" in sections:
            data["cloud"] = self._cloud_summary(document)
        return self._result(request["request_id"], "cad.get_context", data, warnings=warnings)

    def preview(self, action: dict[str, Any], cancel: CancelToken) -> dict[str, Any]:
        document, design = self._active(action["target"]["document_id"])
        cancel.check()
        snapshot = self.snapshot(action)
        planned = self._planned_changes(action, snapshot)
        return self._result(
            action["request_id"], action["action"],
            {"kind": "preview", "planned_changes": planned, "verification_plan": self._verification_plan(action)},
            status="success",
            # A preview only observes current values and describes future checks.
            # It does not rebuild the model, so it must not publish compute evidence.
            verification=None,
        )

    def snapshot(self, action: dict[str, Any]) -> dict[str, Any]:
        document, design = self._active(action["target"]["document_id"])
        self._assert_writable(document)
        name = action["action"]
        snapshot: dict[str, Any] = {
            "snapshot_id": str(uuid.uuid4()),
            "document_id": self._document_id(document),
            "document_modified": bool(document.isModified),
            "feature_errors": self._feature_errors(design),
        }
        if name in {"cad.update_parameter", "cad.update_feature_parameter"}:
            parameter = self._resolve_one(design, action["target"]["parameter_id"], "PARAMETER_NOT_FOUND")
            snapshot.update({"target_id": parameter.entityToken, "expression": parameter.expression})
        elif name == "cad.update_entity_properties":
            entity = self._resolve_one(design, action["target"]["entity_id"], "TARGET_NOT_FOUND")
            self._validate_entity_kind(entity, action["target"])
            material = getattr(entity, "material", None)
            snapshot.update({
                "target_id": getattr(entity, "entityToken", action["target"]["entity_id"]),
                "name": getattr(entity, "name", None),
                "part_number": getattr(entity, "partNumber", None),
                "description": getattr(entity, "description", None),
                "material": {
                    "library_id": getattr(getattr(material, "parent", None), "id", None),
                    "material_id": getattr(material, "id", None),
                } if material else None,
            })
        elif name in {"cad.save_document", "cad.save_as"}:
            data_file = getattr(document, "dataFile", None) if document.isSaved else None
            snapshot.update({"version_id": getattr(data_file, "versionId", None), "is_saved": bool(document.isSaved)})
        return snapshot

    def execute(self, action: dict[str, Any], execution_context: dict[str, Any], cancel: CancelToken) -> dict[str, Any]:
        document, design = self._active(action["target"]["document_id"])
        name = action["action"]
        before_errors = self._feature_errors(design)
        if name == "cad.export":
            return self._export(action, execution_context, cancel)
        # Dispatcher supplies the journaled pre-mutation snapshot.  Keeping the
        # fallback makes direct facade/diagnostic calls truthful as well.
        pre_mutation_snapshot = action.get("_snapshot") or self.snapshot(action)
        self._assert_writable(document)
        cancel.check()
        if name in {"cad.update_parameter", "cad.update_feature_parameter"}:
            changes, ids = self._update_parameter(design, action, feature_owned=name == "cad.update_feature_parameter")
        elif name == "cad.create_sketch":
            changes, ids = self._create_sketch(design, action)
        elif name == "cad.create_extrude":
            changes, ids = self._create_extrude(design, action)
        elif name == "cad.create_hole":
            changes, ids = self._create_hole(design, action)
        elif name == "cad.create_fillet":
            changes, ids = self._create_fillet(design, action)
        elif name == "cad.create_chamfer":
            changes, ids = self._create_chamfer(design, action)
        elif name == "cad.update_entity_properties":
            changes, ids = self._update_properties(design, action)
        elif name == "cad.save_document":
            result = self._save(document, design, action, before_errors, save_as=False)
            result["_snapshot"] = pre_mutation_snapshot
            return result
        elif name == "cad.save_as":
            result = self._save(document, design, action, before_errors, save_as=True)
            result["_snapshot"] = pre_mutation_snapshot
            return result
        else:
            raise DispatchError("UNSUPPORTED_ACTION", f"Unsupported Fusion action: {name}")
        cancel.check()
        verification = self._verify_after_mutation(design, action, ids, before_errors)
        result = self._result(
            action["request_id"], name,
            {
                "kind": "mutation", "snapshot_id": pre_mutation_snapshot["snapshot_id"],
                "created_or_updated_entity_ids": ids,
                "compensation": "semantic_restore" if name.startswith("cad.update_") else "delete_created_feature",
            },
            changes=changes, verification=verification,
        )
        # Internal transport evidence. Runtime removes this before persisting the
        # public result and binds snapshot_id to this exact pre-mutation payload.
        result["_snapshot"] = pre_mutation_snapshot
        return result

    def verify(self, request: dict[str, Any], cancel: CancelToken) -> dict[str, Any]:
        specification = request["specification"]
        _, design = self._active(specification["document_id"])
        cancel.check()
        compute_completed = bool(design.computeAll())
        cancel.check()
        results = [{
            "check": "computeAll", "passed": compute_completed,
            "actual": compute_completed,
        }]
        new_feature_errors: list[str] = []
        references = request.get("_references", {})
        for check in specification["checks"]:
            cancel.check()
            kind = check["check"]
            passed = False
            actual: Any = None
            if kind == "parameter_equals":
                parameter = self._resolve_one(design, check["target_id"], "PARAMETER_NOT_FOUND")
                expected = self._dimension_expression(check["expected"])
                actual = parameter.expression
                passed = actual == expected or abs(float(parameter.value) - self._evaluate(design, expected, parameter.unit)) <= check.get("tolerance", 1e-6)
            elif kind == "entity_resolves":
                actual = len(design.findEntityByToken(check["target_id"]))
                passed = actual == 1
            elif kind in {"no_feature_errors", "no_new_feature_errors"}:
                errors = self._feature_errors(design)
                actual = errors
                if kind == "no_feature_errors":
                    passed = not errors
                else:
                    snapshot_id = check.get("snapshot_id")
                    if snapshot_id:
                        baseline = set(
                            references.get("snapshot_feature_errors", {}).get(snapshot_id, [])
                        )
                    else:
                        baseline = set(references.get("baseline_feature_errors", []))
                    discovered = sorted(set(errors).difference(baseline))
                    new_feature_errors.extend(discovered)
                    actual = discovered
                    passed = not discovered
            elif kind in {"local_save_accepted", "cloud_version_complete", "artifact_valid"}:
                source = references.get("source_result", {})
                if kind == "local_save_accepted":
                    actual = (source.get("data") or {}).get("local_save_accepted")
                    passed = actual is True
                elif kind == "cloud_version_complete":
                    actual = (source.get("data") or {}).get("cloud_version_processing")
                    passed = actual == "complete"
                else:
                    actual = references.get("artifact_evidence", {}).get(check["artifact_id"], {"valid": False})
                    passed = actual.get("valid") is True
            results.append({"check": kind, "passed": passed, "target_id": check.get("target_id"), "expected": check.get("expected"), "actual": actual})
        verification = {
            "passed": compute_completed and all(item["passed"] for item in results),
            "checks": results,
            "compute_completed": compute_completed,
            "new_feature_errors": sorted(set(new_feature_errors)),
        }
        status = "success" if verification["passed"] else "failed"
        error_code = "VERIFICATION_FAILED" if compute_completed else "COMPUTE_FAILED"
        error_message = (
            "One or more Fusion verification checks failed"
            if compute_completed else "Fusion computeAll did not complete during verification"
        )
        return self._result(
            request["request_id"], "cad.verify", verification, status=status, verification=verification,
            error=None if status == "success" else {
                "code": error_code, "message": error_message,
                "category": "verification" if compute_completed else "fusion",
                "retryable": False, "details": {},
            },
        )

    def compensate(self, action: dict[str, Any], snapshot: dict[str, Any]) -> None:
        _, design = self._active(action["target"]["document_id"])
        name = action["action"]
        if name in {"cad.update_parameter", "cad.update_feature_parameter"}:
            self._resolve_one(design, action["target"]["parameter_id"], "PARAMETER_NOT_FOUND").expression = snapshot["expression"]
        elif name == "cad.update_entity_properties":
            entity = self._resolve_one(design, action["target"]["entity_id"], "TARGET_NOT_FOUND")
            self._validate_entity_kind(entity, action["target"])
            for source, target in (("name", "name"), ("part_number", "partNumber"), ("description", "description")):
                if snapshot.get(source) is not None and hasattr(entity, target):
                    setattr(entity, target, snapshot[source])
            if action["properties"].get("material") is not None:
                material_ref = snapshot.get("material")
                if material_ref:
                    library = self.app.materialLibraries.itemById(material_ref["library_id"])
                    material = library.materials.itemById(material_ref["material_id"]) if library else None
                    if not material:
                        raise RuntimeError("snapshot material could not be resolved")
                    entity.material = material
                else:
                    entity.material = None
        elif name in {"cad.save_document", "cad.save_as"}:
            raise RuntimeError("save operations cannot be compensated")
        else:
            created = self._created_by_request.pop(str(action["request_id"]), None)
            if not created or not created.deleteMe():
                raise RuntimeError("created Fusion entity could not be deleted")
        if not design.computeAll():
            raise RuntimeError("Fusion compensation recompute failed")

    # ---- concrete action handlers -------------------------------------------------

    def _update_parameter(self, design: Any, action: dict[str, Any], *, feature_owned: bool) -> tuple[list[dict[str, Any]], list[str]]:
        parameter = self._resolve_one(design, action["target"]["parameter_id"], "PARAMETER_NOT_FOUND")
        if feature_owned:
            feature = self._resolve_one(design, action["target"]["feature_id"], "FEATURE_NOT_FOUND")
            if getattr(parameter, "createdBy", None) != feature:
                raise DispatchError("INVALID_ACTION", "Parameter is not owned by the targeted Feature")
        before = parameter.expression
        after = self._dimension_expression(action["value"])
        parameter.expression = after
        return [self._change(parameter.entityToken, "parameter.expression", before, parameter.expression)], [parameter.entityToken]

    def _create_sketch(self, design: Any, action: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        self._require_adsk()
        component = self._resolve_component(design, action["target"]["component_id"])
        plane = action["plane"]
        if plane["kind"] == "origin":
            target_plane = {"xy": component.xYConstructionPlane, "xz": component.xZConstructionPlane, "yz": component.yZConstructionPlane}[plane["plane"]]
        else:
            target_plane = self._resolve_one(design, plane["entity_id"], "TARGET_NOT_FOUND")
        sketch = component.sketches.add(target_plane)
        if action.get("name"):
            sketch.name = action["name"]
        curves = sketch.sketchCurves
        for primitive in action["primitives"]:
            if primitive["kind"] == "line":
                curves.sketchLines.addByTwoPoints(self._point(design, primitive["start"], action["unit"]), self._point(design, primitive["end"], action["unit"]))
            elif primitive["kind"] == "circle":
                curves.sketchCircles.addByCenterRadius(
                    self._point(design, primitive["center"], action["unit"]),
                    self._evaluate(design, f"{primitive['radius']} {action['unit']}", "cm"),
                )
            else:
                curves.sketchLines.addTwoPointRectangle(
                    self._point(design, primitive["corner1"], action["unit"]),
                    self._point(design, primitive["corner2"], action["unit"]),
                )
        self._created_by_request[str(action["request_id"])] = sketch
        return [self._created_change(sketch.entityToken, "sketch")], [sketch.entityToken]

    def _create_extrude(self, design: Any, action: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        self._require_adsk()
        component = self._resolve_component(design, action["target"]["component_id"])
        profile = self._resolve_one(design, action["profile_id"], "TARGET_NOT_FOUND")
        operation = {
            "new_body": adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
            "new_component": adsk.fusion.FeatureOperations.NewComponentFeatureOperation,
            "join": adsk.fusion.FeatureOperations.JoinFeatureOperation,
            "cut": adsk.fusion.FeatureOperations.CutFeatureOperation,
            "intersect": adsk.fusion.FeatureOperations.IntersectFeatureOperation,
        }[action["operation"]]
        features = component.features.extrudeFeatures
        feature_input = features.createInput(profile, operation)
        distance = self._value_input(action["distance"])
        if action["direction"] == "symmetric":
            if not feature_input.setSymmetricExtent(distance, True):
                raise DispatchError("FUSION_API_ERROR", "Fusion rejected the symmetric extrude extent")
        else:
            extent = adsk.fusion.DistanceExtentDefinition.create(distance)
            direction = adsk.fusion.ExtentDirections.PositiveExtentDirection if action["direction"] == "positive" else adsk.fusion.ExtentDirections.NegativeExtentDirection
            if not feature_input.setOneSideExtent(extent, direction):
                raise DispatchError("FUSION_API_ERROR", "Fusion rejected the extrude extent")
        if action.get("participant_body_ids"):
            feature_input.participantBodies = [self._resolve_one(design, token, "TARGET_NOT_FOUND") for token in action["participant_body_ids"]]
        feature = features.add(feature_input)
        return self._remember_created(action, feature, "extrude")

    def _create_hole(self, design: Any, action: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        self._require_adsk()
        component = self._resolve_component(design, action["target"]["component_id"])
        features = component.features.holeFeatures
        feature_input = features.createSimpleInput(self._value_input(action["diameter"]))
        points = adsk.core.ObjectCollection.create()
        for token in action["sketch_point_ids"]:
            points.add(self._resolve_one(design, token, "TARGET_NOT_FOUND"))
        if not feature_input.setPositionBySketchPoints(points):
            raise DispatchError("FUSION_API_ERROR", "Fusion rejected the hole sketch points")
        extent = action["extent"]
        if extent["kind"] == "distance":
            accepted = feature_input.setDistanceExtent(self._value_input(extent["distance"]))
        else:
            direction = adsk.fusion.ExtentDirections.PositiveExtentDirection if extent["direction"] == "positive" else adsk.fusion.ExtentDirections.NegativeExtentDirection
            accepted = feature_input.setAllExtent(direction)
        if not accepted:
            raise DispatchError("FUSION_API_ERROR", "Fusion rejected the hole extent")
        feature = features.add(feature_input)
        return self._remember_created(action, feature, "hole")

    def _create_fillet(self, design: Any, action: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        self._require_adsk()
        component = self._resolve_component(design, action["target"]["component_id"])
        edges = self._object_collection(design, action["edge_ids"])
        features = component.features.filletFeatures
        feature_input = features.createInput()
        feature_input.edgeSetInputs.addConstantRadiusEdgeSet(edges, self._value_input(action["radius"]), action["tangent_chain"])
        feature = features.add(feature_input)
        return self._remember_created(action, feature, "fillet")

    def _create_chamfer(self, design: Any, action: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        self._require_adsk()
        component = self._resolve_component(design, action["target"]["component_id"])
        edges = self._object_collection(design, action["edge_ids"])
        features = component.features.chamferFeatures
        feature_input = features.createInput2()
        feature_input.chamferEdgeSets.addEqualDistanceChamferEdgeSet(edges, self._value_input(action["distance"]), action["tangent_chain"])
        feature = features.add(feature_input)
        return self._remember_created(action, feature, "chamfer")

    def _update_properties(self, design: Any, action: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        entity = self._resolve_one(design, action["target"]["entity_id"], "TARGET_NOT_FOUND")
        self._validate_entity_kind(entity, action["target"])
        changes = []
        for source, target in (("name", "name"), ("part_number", "partNumber"), ("description", "description")):
            if source in action["properties"] and action["properties"][source] is not None:
                if not hasattr(entity, target):
                    raise DispatchError("INVALID_ACTION", f"{source} is not supported by this entity")
                before = getattr(entity, target)
                setattr(entity, target, action["properties"][source])
                changes.append(self._change(action["target"]["entity_id"], f"properties.{source}", before, getattr(entity, target)))
        material_ref = action["properties"].get("material")
        if material_ref:
            library = self.app.materialLibraries.itemById(material_ref["library_id"])
            material = library.materials.itemById(material_ref["material_id"]) if library else None
            if not material or not hasattr(entity, "material"):
                raise DispatchError("TARGET_NOT_FOUND", "Requested Fusion material was not found or cannot be assigned")
            before = getattr(getattr(entity, "material", None), "id", None)
            entity.material = material
            changes.append(self._change(action["target"]["entity_id"], "properties.material", before, material.id))
        return changes, [action["target"]["entity_id"]]

    def _save(
        self,
        document: Any,
        design: Any,
        action: dict[str, Any],
        before_errors: list[str],
        *,
        save_as: bool,
    ) -> dict[str, Any]:
        compute = bool(design.computeAll())
        if not compute:
            raise DispatchError("COMPUTE_FAILED", "Fusion computeAll did not complete before save")
        new_errors = sorted(set(self._feature_errors(design)).difference(before_errors))
        if new_errors:
            raise DispatchError(
                "VERIFICATION_FAILED",
                "Fusion reported new feature errors before save",
                details={"new_feature_errors": new_errors},
            )
        before_file = getattr(document, "dataFile", None) if document.isSaved else None
        before_version = getattr(before_file, "versionId", None)
        if save_as:
            folder = self.app.data.findFolderById(action["data_folder_id"])
            if not folder:
                raise DispatchError("TARGET_NOT_FOUND", "Fusion cloud folder was not found")
            accepted = document.saveAs(action["name"], folder, action.get("description", ""), action.get("tag", ""))
        else:
            if not document.isSaved:
                raise DispatchError("DOCUMENT_UNSAVED", "Unsaved Fusion documents require cad.save_as")
            accepted = document.save(action.get("version_description", ""))
        if not accepted:
            raise DispatchError("FUSION_API_ERROR", "Fusion did not accept the save request")
        after_file = getattr(document, "dataFile", None)
        cloud_state = "complete" if after_file and after_file.isComplete else "pending"
        local_accepted = bool(accepted and document.isSaved and not document.isModified)
        verification = {
            "passed": local_accepted,
            "checks": [
                {"check": "computeAll", "passed": compute, "actual": compute},
                {"check": "no_new_feature_errors", "passed": not new_errors, "actual": new_errors},
                {
                    "check": "local_save_accepted", "passed": local_accepted,
                    "expected": {"is_saved": True, "is_modified": False},
                    "actual": {"is_saved": bool(document.isSaved), "is_modified": bool(document.isModified)},
                },
            ],
            "compute_completed": compute, "new_feature_errors": new_errors,
        }
        return self._result(
            action["request_id"], action["action"],
            {
                "kind": "save", "local_save_accepted": local_accepted,
                "cloud_version_processing": cloud_state, "before_version": before_version,
                "after_version": getattr(after_file, "versionId", None),
            },
            changes=[self._change(self._document_id(document), "document.version", before_version, getattr(after_file, "versionId", None))],
            verification=verification,
        )

    def _export(self, action: dict[str, Any], execution_context: dict[str, Any], cancel: CancelToken) -> dict[str, Any]:
        self._require_adsk()
        document, design = self._active(action["target"]["document_id"])
        if execution_context.get("artifact_root_fingerprint") != self.config.artifact_root_fingerprint:
            raise DispatchError("PATH_NOT_ALLOWED", "Artifact root fingerprint does not match Add-in configuration")
        artifact_dir = Path(execution_context["artifact_dir"]).resolve()
        root = self.config.artifact_root.resolve()
        if not artifact_dir.is_relative_to(root):
            raise DispatchError("PATH_NOT_ALLOWED", "Runtime artifact directory escapes configured root")
        filename = action["filename"]
        if Path(filename).name != filename:
            raise DispatchError("PATH_NOT_ALLOWED", "Export filename must be a basename")
        path = (artifact_dir / filename).resolve()
        if not path.is_relative_to(artifact_dir):
            raise DispatchError("PATH_NOT_ALLOWED", "Export path escapes request directory")
        path.parent.mkdir(parents=True, exist_ok=True)
        cancel.check()
        before_errors = self._feature_errors(design)
        compute_completed = bool(design.computeAll())
        if not compute_completed:
            raise DispatchError("COMPUTE_FAILED", "Fusion computeAll did not complete before export")
        after_errors = self._feature_errors(design)
        new_feature_errors = sorted(set(after_errors).difference(before_errors))
        if new_feature_errors:
            raise DispatchError(
                "VERIFICATION_FAILED",
                "Fusion compute introduced feature errors before export",
                details={"new_feature_errors": new_feature_errors},
            )
        cancel.check()
        manager = design.exportManager
        fmt = action["format"]
        options = action.get("options", {})
        geometry = self._export_geometry(design, options)
        if fmt == "step":
            export_options = manager.createSTEPExportOptions(str(path), geometry) if geometry else manager.createSTEPExportOptions(str(path))
            accepted = manager.execute(export_options)
        elif fmt == "f3d":
            export_options = manager.createFusionArchiveExportOptions(str(path), geometry) if geometry else manager.createFusionArchiveExportOptions(str(path))
            accepted = manager.execute(export_options)
        elif fmt == "stl":
            geometry = geometry or design.rootComponent
            export_options = manager.createSTLExportOptions(geometry, str(path))
            refinements = {
                "high": adsk.fusion.MeshRefinementSettings.MeshRefinementHigh,
                "medium": adsk.fusion.MeshRefinementSettings.MeshRefinementMedium,
                "low": adsk.fusion.MeshRefinementSettings.MeshRefinementLow,
            }
            export_options.meshRefinement = refinements[options.get("mesh_refinement", "medium")]
            if options.get("binary") is not None:
                export_options.isBinaryFormat = options["binary"]
            accepted = manager.execute(export_options)
        elif fmt == "dxf":
            sketch = self._resolve_one(design, options["sketch_id"], "TARGET_NOT_FOUND")
            # Autodesk still labels createDXFSketchExportOptions as Preview and says
            # not to distribute it. Use the released legacy API unless an operator
            # explicitly opts into the preview in a future config revision.
            accepted = sketch.saveAsDXF(str(path))
        elif fmt == "png":
            accepted = self.app.activeViewport.saveAsImageFile(
                str(path), options.get("width") or 1024, options.get("height") or 768
            )
        else:
            raise DispatchError("UNSUPPORTED_ACTION", f"Unsupported export format: {fmt}")
        if not accepted:
            raise DispatchError("EXPORT_FAILED", f"Fusion failed to export {fmt.upper()}")
        cancel.check()
        size, digest = self._artifact_metadata(path, fmt)
        artifact_id = str(uuid.uuid4())
        local = {
            "artifact_id": artifact_id, "kind": fmt, "filename": filename,
            "media_type": {
                "step": "model/step", "stl": "model/stl", "dxf": "image/vnd.dxf",
                "f3d": "application/vnd.autodesk.fusion360", "png": "image/png",
            }[fmt],
            "size_bytes": size, "sha256": digest, "relative_path": filename,
        }
        result = self._result(
            action["request_id"], action["action"],
            {"kind": "export", "format": fmt, "artifact_ids": [artifact_id]},
            verification={
                "passed": True,
                "checks": [
                    {"check": "computeAll", "passed": compute_completed, "actual": compute_completed},
                    {"check": "no_new_feature_errors", "passed": True, "actual": new_feature_errors},
                    {"check": "artifact_valid", "passed": True, "actual": {"size_bytes": size, "sha256": digest}},
                ],
                "compute_completed": compute_completed, "new_feature_errors": new_feature_errors,
            },
        )
        result["_local_artifacts"] = [local]
        return result

    # ---- verification, traversal, and utility helpers ----------------------------

    def _active(self, expected_document_id: str | None = None) -> tuple[Any, Any]:
        document = self.app.activeDocument
        if not document:
            raise DispatchError("NO_ACTIVE_DOCUMENT", "Fusion has no active document")
        design = adsk.fusion.Design.cast(self.app.activeProduct) if adsk else getattr(self.app, "activeProduct", None)
        if not design:
            raise DispatchError("NO_ACTIVE_DESIGN", "The active Fusion document is not a Design")
        if expected_document_id and expected_document_id != self._document_id(document):
            raise DispatchError("DOCUMENT_MISMATCH", "The active Fusion document does not match the action target")
        return document, design

    def _assert_writable(self, document: Any) -> None:
        data_file = getattr(document, "dataFile", None) if document.isSaved else None
        if data_file and getattr(data_file, "isReadOnly", False):
            raise DispatchError("DOCUMENT_READ_ONLY", "The active Fusion document is read-only")

    @staticmethod
    def _document_id(document: Any) -> str:
        data_file = getattr(document, "dataFile", None) if document.isSaved else None
        return str(getattr(data_file, "id", None) or document.creationId)

    @staticmethod
    def _design_type(design: Any) -> str:
        if adsk:
            if design.designType == adsk.fusion.DesignTypes.ParametricDesignType:
                return "parametric"
            if design.designType == adsk.fusion.DesignTypes.DirectDesignType:
                return "direct"
        return "unknown"

    @staticmethod
    def _items(collection: Any, limit: int) -> list[Any]:
        return [collection.item(i) for i in range(min(collection.count, limit))]

    def _resolve_one(self, design: Any, token: str, missing_code: str) -> Any:
        matches = list(design.findEntityByToken(token))
        if not matches:
            raise DispatchError(missing_code, "Fusion target entity could not be resolved")
        if len(matches) > 1:
            raise DispatchError("TARGET_AMBIGUOUS", "Fusion entity token resolved to multiple entities")
        return matches[0]

    def _resolve_component(self, design: Any, token: str) -> Any:
        entity = self._resolve_one(design, token, "TARGET_NOT_FOUND")
        component = adsk.fusion.Component.cast(entity) if adsk else entity
        if not component:
            raise DispatchError("TARGET_NOT_FOUND", "Target is not a Fusion Component")
        return component

    @staticmethod
    def _selection_kind(entity: Any) -> str:
        """Return the semantic kind needed to authorize typed selection actions.

        Fusion's public ``objectType`` is included as vendor evidence, but a
        stable, small kind allowlist keeps Cloud Agent ownership checks
        independent from language-specific class names.
        """

        compact = str(getattr(entity, "objectType", "")).lower().replace("_", "")
        for marker, kind in (
            ("sketchpoint", "sketch_point"),
            ("constructionplane", "construction_plane"),
            ("brepedge", "edge"),
            ("brepface", "face"),
            ("brepbody", "body"),
            ("profile", "profile"),
            ("occurrence", "occurrence"),
            ("component", "component"),
            ("sketch", "sketch"),
            ("parameter", "parameter"),
            ("feature", "feature"),
        ):
            if marker in compact:
                return kind
        return "selection"

    @staticmethod
    def _owning_component_id(entity: Any) -> str | None:
        component = getattr(entity, "parentComponent", None)
        if component is None:
            component = getattr(getattr(entity, "parentSketch", None), "parentComponent", None)
        if component is None:
            component = getattr(getattr(entity, "parentBody", None), "parentComponent", None)
        if component is None:
            component = getattr(getattr(entity, "body", None), "parentComponent", None)
        return getattr(component, "entityToken", None)

    @staticmethod
    def _entity_summary(entity: Any, kind: str) -> dict[str, Any]:
        return {
            "id": str(getattr(entity, "entityToken", getattr(entity, "objectType", "unknown"))),
            "name": str(getattr(entity, "name", kind)), "kind": kind,
            "component_id": FusionApiFacade._owning_component_id(entity),
            "parent_id": None, "suppressed": getattr(entity, "isSuppressed", None),
            "health_state": FusionApiFacade._health_state_name(getattr(entity, "healthState", None)),
            "vendor_extensions": {"autodesk:object_type": str(getattr(entity, "objectType", ""))},
        }

    @staticmethod
    def _health_state_name(value: Any) -> str | None:
        if value is None:
            return None
        try:
            numeric = int(value)
        except (TypeError, ValueError, RuntimeError):
            text = str(value).lower()
            for name in ("healthy", "warning", "error", "suppressed", "rolled_back", "unknown"):
                if name.replace("_", "") in text.replace("_", ""):
                    return name
            return text or None
        return {
            0: "healthy", 1: "warning", 2: "error", 3: "suppressed",
            4: "rolled_back", 5: "unknown",
        }.get(numeric, f"unknown:{numeric}")

    def _occurrence_summaries(self, root: Any, max_depth: int, include_suppressed: bool) -> list[dict[str, Any]]:
        result = []
        stack = [(root.occurrences.item(i), 1, root.entityToken) for i in range(root.occurrences.count)]
        while stack and len(result) < MAX_CONTEXT_ENTITIES:
            occurrence, depth, parent_id = stack.pop()
            suppressed = bool(getattr(occurrence, "isSuppressed", False))
            if include_suppressed or not suppressed:
                summary = self._entity_summary(occurrence, "occurrence")
                summary["parent_id"] = parent_id
                summary["component_id"] = getattr(getattr(occurrence, "component", None), "entityToken", None)
                result.append(summary)
            if depth < max_depth:
                children = occurrence.childOccurrences
                stack.extend((children.item(i), depth + 1, occurrence.entityToken) for i in range(children.count))
        return result

    @staticmethod
    def _parameter_summary(parameter: Any, root_component_id: str | None = None) -> dict[str, Any]:
        object_type = str(parameter.objectType)
        try:
            numeric_value = float(parameter.value)
        except (TypeError, ValueError, RuntimeError):
            # Fusion also exposes text-valued parameters. Their expression is
            # still useful context, but a numeric value/unit would be false.
            numeric_value = None
        is_user_parameter = object_type.endswith("UserParameter")
        created_by = getattr(parameter, "createdBy", None)
        component = getattr(parameter, "component", None)
        if component is None:
            component = getattr(created_by, "parentComponent", None)
        component_id = getattr(component, "entityToken", None)
        if component_id is None and is_user_parameter:
            # User parameters are design-wide. The root component is their
            # explicit action scope so the typed target remains unambiguous.
            component_id = root_component_id
        return {
            "id": parameter.entityToken, "name": parameter.name, "expression": parameter.expression,
            "value": numeric_value, "unit": parameter.unit or None,
            "is_user_parameter": is_user_parameter,
            "component_id": component_id,
            "created_by_id": getattr(created_by, "entityToken", None),
        }

    def _material_summaries(self, components: list[Any]) -> list[dict[str, Any]]:
        result = []
        seen = set()
        for component in components:
            material = getattr(component, "material", None)
            if material and material.id not in seen:
                seen.add(material.id)
                result.append({
                    "id": material.id, "name": material.name,
                    "library_id": getattr(getattr(material, "parent", None), "id", None),
                    "component_id": component.entityToken,
                })
        return result

    @staticmethod
    def _mass_summary(component: Any) -> dict[str, Any]:
        properties = component.physicalProperties
        center = properties.centerOfMass
        return {
            "target_id": component.entityToken, "mass": properties.mass,
            "volume": properties.volume, "density": properties.density,
            "center_of_mass": [center.x, center.y, center.z],
        }

    def _collection_summaries(self, components: list[Any], attribute: str, kind: str) -> list[dict[str, Any]]:
        result = []
        for component in components:
            collection = getattr(component, attribute)
            for item in self._items(collection, MAX_CONTEXT_ENTITIES - len(result)):
                result.append(self._entity_summary(item, kind))
            if len(result) >= MAX_CONTEXT_ENTITIES:
                break
        return result

    def _feature_summaries(self, components: list[Any]) -> list[dict[str, Any]]:
        result = []
        for component in components:
            for feature in self._items(component.features, MAX_CONTEXT_ENTITIES - len(result)):
                result.append(self._entity_summary(feature, "feature"))
            if len(result) >= MAX_CONTEXT_ENTITIES:
                return result
        return result

    @staticmethod
    def _timeline_summaries(timeline: Any) -> tuple[list[dict[str, Any]], bool]:
        """Return the parametric Design.timeline, not Fusion's UI Browser tree."""
        count = int(timeline.count)
        result: list[dict[str, Any]] = []
        for position in range(min(count, MAX_CONTEXT_ENTITIES)):
            timeline_object = timeline.item(position)
            if timeline_object is None:
                continue
            entity = timeline_object.entity
            parent_group = timeline_object.parentGroup
            result.append({
                "index": int(timeline_object.index),
                "name": str(timeline_object.name),
                "kind": (
                    str(getattr(entity, "objectType", ""))
                    if entity is not None else "adsk::fusion::TimelineGroup"
                ),
                "entity_id": getattr(entity, "entityToken", None) if entity is not None else None,
                "health_state": FusionApiFacade._health_state_name(timeline_object.healthState),
                "is_group": bool(timeline_object.isGroup),
                "is_rolled_back": bool(timeline_object.isRolledBack),
                "is_suppressed": bool(timeline_object.isSuppressed),
                "parent": {
                    "index": int(parent_group.index),
                    "name": str(parent_group.name),
                } if parent_group is not None else None,
                "error_or_warning": str(timeline_object.errorOrWarningMessage) or None,
            })
        return result, count > MAX_CONTEXT_ENTITIES

    @staticmethod
    def _cloud_summary(document: Any) -> dict[str, Any] | None:
        if not document.isSaved or not document.dataFile:
            return None
        data_file = document.dataFile
        return {
            "data_file_id": data_file.id, "version_id": data_file.versionId,
            "version_number": data_file.versionNumber,
            "project_id": getattr(data_file.parentProject, "id", None),
            "folder_id": getattr(data_file.parentFolder, "id", None),
            "is_complete": data_file.isComplete, "is_read_only": data_file.isReadOnly,
        }

    def _feature_errors(self, design: Any) -> list[str]:
        errors = []
        for component in self._items(design.allComponents, MAX_CONTEXT_ENTITIES):
            for feature in self._items(component.features, MAX_CONTEXT_ENTITIES - len(errors)):
                if self._health_state_name(getattr(feature, "healthState", None)) == "error":
                    errors.append(f"{feature.entityToken}:{getattr(feature, 'errorOrWarningMessage', '')}")
            if len(errors) >= MAX_CONTEXT_ENTITIES:
                break
        return errors

    def _verify_after_mutation(self, design: Any, action: dict[str, Any], ids: list[str], before_errors: list[str]) -> dict[str, Any]:
        compute = bool(design.computeAll())
        if not compute:
            raise DispatchError("COMPUTE_FAILED", "Fusion computeAll did not complete")
        checks = []
        for token in ids:
            resolved = len(design.findEntityByToken(token)) == 1
            checks.append({"check": "entity_resolves", "passed": resolved, "target_id": token, "actual": resolved})
        expected_types = {
            "cad.create_sketch": "Sketch",
            "cad.create_extrude": "ExtrudeFeature",
            "cad.create_hole": "HoleFeature",
            "cad.create_fillet": "FilletFeature",
            "cad.create_chamfer": "ChamferFeature",
        }
        expected_type = expected_types.get(action["action"])
        if expected_type:
            for token in ids:
                entity = self._resolve_one(design, token, "TARGET_NOT_FOUND")
                actual_type = str(getattr(entity, "objectType", ""))
                checks.append({
                    "check": "feature_type_equals", "passed": actual_type.endswith(expected_type),
                    "target_id": token, "expected": expected_type, "actual": actual_type,
                })
        if action["action"] in {"cad.update_parameter", "cad.update_feature_parameter"}:
            parameter = self._resolve_one(design, action["target"]["parameter_id"], "PARAMETER_NOT_FOUND")
            expected = self._dimension_expression(action["value"])
            equal = parameter.expression == expected or abs(parameter.value - self._evaluate(design, expected, parameter.unit)) <= 1e-6
            checks.append({"check": "parameter_equals", "passed": equal, "target_id": parameter.entityToken, "expected": expected, "actual": parameter.expression})
        elif action["action"] == "cad.update_entity_properties":
            entity = self._resolve_one(design, action["target"]["entity_id"], "TARGET_NOT_FOUND")
            self._validate_entity_kind(entity, action["target"])
            for source, target in (("name", "name"), ("part_number", "partNumber"), ("description", "description")):
                expected = action["properties"].get(source)
                if expected is not None:
                    actual = getattr(entity, target, None)
                    checks.append({
                        "check": "property_equals", "passed": actual == expected,
                        "target_id": action["target"]["entity_id"], "expected": expected, "actual": actual,
                    })
            material_ref = action["properties"].get("material")
            if material_ref:
                actual = getattr(getattr(entity, "material", None), "id", None)
                expected = material_ref["material_id"]
                checks.append({
                    "check": "material_equals", "passed": actual == expected,
                    "target_id": action["target"]["entity_id"], "expected": expected, "actual": actual,
                })
        after_errors = self._feature_errors(design)
        new_errors = sorted(set(after_errors).difference(before_errors))
        checks.append({"check": "no_new_feature_errors", "passed": not new_errors, "actual": new_errors})
        passed = compute and all(item["passed"] for item in checks)
        if not passed:
            raise DispatchError("VERIFICATION_FAILED", "Fusion mutation verification failed", details={"new_feature_errors": new_errors})
        return {"passed": True, "checks": checks, "compute_completed": True, "new_feature_errors": new_errors}

    def _remember_created(self, action: dict[str, Any], feature: Any, kind: str) -> tuple[list[dict[str, Any]], list[str]]:
        if not feature:
            raise DispatchError("FUSION_API_ERROR", f"Fusion failed to create {kind}")
        self._created_by_request[str(action["request_id"])] = feature
        return [self._created_change(feature.entityToken, kind)], [feature.entityToken]

    def _object_collection(self, design: Any, tokens: list[str]) -> Any:
        collection = adsk.core.ObjectCollection.create()
        for token in tokens:
            collection.add(self._resolve_one(design, token, "TARGET_NOT_FOUND"))
        return collection

    @staticmethod
    def _validate_entity_kind(entity: Any, target: dict[str, Any]) -> None:
        expected_kind = target.get("entity_kind")
        if expected_kind is None:
            return
        expected_type = {
            "component": "adsk::fusion::Component",
            "occurrence": "adsk::fusion::Occurrence",
            "body": "adsk::fusion::BRepBody",
        }.get(expected_kind)
        if expected_type is None or getattr(entity, "objectType", None) != expected_type:
            raise DispatchError(
                "INVALID_ACTION",
                "Resolved Fusion entity does not match the declared entity kind",
            )

    def _export_geometry(self, design: Any, options: dict[str, Any]) -> Any:
        for field in ("body_id", "component_id"):
            if options.get(field):
                return self._resolve_one(design, options[field], "TARGET_NOT_FOUND")
        return None

    def _value_input(self, value: dict[str, Any]) -> Any:
        self._require_adsk()
        return adsk.core.ValueInput.createByString(self._dimension_expression(value))

    @staticmethod
    def _dimension_expression(value: dict[str, Any]) -> str:
        if value.get("expression") is not None:
            return value["expression"]
        return f"{value['amount']} {value['unit']}"

    @staticmethod
    def _evaluate(design: Any, expression: str, unit: str) -> float:
        return float(design.unitsManager.evaluateExpression(expression, unit))

    def _point(self, design: Any, point: dict[str, Any], unit: str) -> Any:
        return adsk.core.Point3D.create(
            self._evaluate(design, f"{point['x']} {unit}", "cm"),
            self._evaluate(design, f"{point['y']} {unit}", "cm"),
            0,
        )

    @staticmethod
    def _artifact_metadata(path: Path, kind: str) -> tuple[int, str]:
        if not path.is_file() or path.stat().st_size <= 0:
            raise DispatchError("ARTIFACT_INVALID", "Fusion export did not create a non-empty file")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            head = handle.read(512)
            digest.update(head)
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        size = path.stat().st_size
        is_ascii_stl = head.lower().lstrip().startswith(b"solid") and b"facet" in head.lower()
        binary_stl_count = struct.unpack("<I", head[80:84])[0] if len(head) >= 84 else None
        is_binary_stl = (
            binary_stl_count is not None
            and binary_stl_count > 0
            and size == 84 + binary_stl_count * 50
        )
        signatures = {
            "png": head.startswith(b"\x89PNG\r\n\x1a\n"),
            "step": b"ISO-10303-21" in head.upper(),
            "dxf": b"SECTION" in head.upper(),
            "stl": is_ascii_stl or is_binary_stl,
            "f3d": head.startswith((b"PK\x03\x04", b"Fusion 360")),
        }
        if not signatures.get(kind, False):
            raise DispatchError("ARTIFACT_INVALID", f"Fusion export has an invalid {kind.upper()} signature")
        return size, digest.hexdigest()

    @staticmethod
    def _planned_changes(action: dict[str, Any], snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        name = action["action"]
        if name in {"cad.update_parameter", "cad.update_feature_parameter"}:
            return [{
                "target_id": action["target"]["parameter_id"], "path": "parameter.expression",
                "kind": "updated", "before": snapshot.get("expression"),
                "after": FusionApiFacade._dimension_expression(action["value"]),
            }]
        if name == "cad.update_entity_properties":
            return [
                {"target_id": action["target"]["entity_id"], "path": f"properties.{key}", "kind": "updated", "before": snapshot.get(key), "after": value}
                for key, value in action["properties"].items()
            ]
        return [{"target_id": action["target"].get("component_id", action["target"]["document_id"]), "path": name.removeprefix("cad."), "kind": "created", "before": None, "after": "planned"}]

    @staticmethod
    def _verification_plan(action: dict[str, Any]) -> list[str]:
        checks = ["computeAll", "no_new_feature_errors", "entity_resolves"]
        if action["action"] in {"cad.update_parameter", "cad.update_feature_parameter"}:
            checks.append("parameter_equals")
        if action["action"] in {"cad.save_document", "cad.save_as"}:
            checks.extend(["local_save_accepted", "cloud_version_status"])
        return checks

    @staticmethod
    def _change(target_id: str, path: str, before: Any, after: Any) -> dict[str, Any]:
        return {"target_id": target_id, "path": path, "kind": "updated", "before": before, "after": after}

    @staticmethod
    def _created_change(target_id: str, path: str) -> dict[str, Any]:
        return {"target_id": target_id, "path": path, "kind": "created", "before": None, "after": target_id}

    @staticmethod
    def _result(
        request_id: str, action: str, data: Any, *, status: str = "success",
        changes: list[dict[str, Any]] | None = None, warnings: list[str] | None = None,
        verification: dict[str, Any] | None = None, error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "request_id": request_id, "status": status, "action": action, "data": data,
            "changes": changes or [], "warnings": warnings or [], "verification": verification,
            "artifacts": [], "approval": None, "error": error,
        }

    @staticmethod
    def _require_adsk() -> None:
        if adsk is None:
            raise RuntimeError("Autodesk Fusion API is unavailable outside Fusion Desktop")
