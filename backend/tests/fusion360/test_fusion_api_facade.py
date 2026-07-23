import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from fusion_addin.CADAgentFusionConnector.config import ConnectorConfig
from fusion_addin.CADAgentFusionConnector.dispatcher import CancelToken, DispatchError
from fusion_addin.CADAgentFusionConnector.fusion_api import FusionApiFacade


class FakeParameter:
    entityToken = "parameter-token"
    expression = "2 mm"
    value = 0.2
    unit = "cm"


class FakeDesign:
    def __init__(self, parameter):
        self.parameter = parameter

    def findEntityByToken(self, token):
        return [self.parameter] if token == "parameter-token" else []


class NarrowFacade(FusionApiFacade):
    def __init__(self, tmp_path):
        config = ConnectorConfig(
            runtime_url="http://127.0.0.1:8765",
            connector_secret="x" * 32,
            connector_instance_id="2144d1f8-bfe1-43da-b3c7-8b444bd77e66",
            artifact_root=tmp_path,
            journal_path=tmp_path / "journal.json",
        )
        super().__init__(SimpleNamespace(), SimpleNamespace(), config)
        self.parameter = FakeParameter()
        self.design = FakeDesign(self.parameter)
        self.document = SimpleNamespace(isSaved=True, isModified=False, dataFile=None, creationId="doc")

    def _active(self, expected_document_id=None):
        assert expected_document_id in {None, "doc"}
        return self.document, self.design

    def _assert_writable(self, document):
        return None

    def _feature_errors(self, design):
        return []

    def _verify_after_mutation(self, design, action, ids, before_errors):
        return {
            "passed": True,
            "checks": [{"check": "parameter_equals", "passed": True, "target_id": ids[0]}],
            "compute_completed": True,
            "new_feature_errors": [],
        }


def _action(mode="execute"):
    return {
        "request_id": "2144d1f8-bfe1-43da-b3c7-8b444bd77e66",
        "action": "cad.update_parameter",
        "execution_mode": mode,
        "target": {"document_id": "doc", "component_id": "component", "parameter_id": "parameter-token"},
        "value": {"amount": 3, "unit": "mm"},
    }


def test_no_active_document_and_non_design_fail_with_stable_errors(tmp_path):
    config = NarrowFacade(tmp_path).config
    no_document = FusionApiFacade(
        SimpleNamespace(activeDocument=None, activeProduct=None), SimpleNamespace(), config
    )
    with pytest.raises(DispatchError) as raised:
        no_document._active()
    assert raised.value.code == "NO_ACTIVE_DOCUMENT"

    non_design = FusionApiFacade(
        SimpleNamespace(activeDocument=SimpleNamespace(), activeProduct=None),
        SimpleNamespace(),
        config,
    )
    with pytest.raises(DispatchError) as raised:
        non_design._active()
    assert raised.value.code == "NO_ACTIVE_DESIGN"


def test_read_only_document_and_missing_parameter_fail_closed(tmp_path):
    facade = NarrowFacade(tmp_path)
    read_only = SimpleNamespace(
        isSaved=True, dataFile=SimpleNamespace(isReadOnly=True)
    )
    with pytest.raises(DispatchError) as raised:
        FusionApiFacade._assert_writable(facade, read_only)
    assert raised.value.code == "DOCUMENT_READ_ONLY"

    facade.design.findEntityByToken = lambda _token: []
    with pytest.raises(DispatchError) as raised:
        facade.snapshot(_action())
    assert raised.value.code == "PARAMETER_NOT_FOUND"


def test_unsaved_document_requires_save_as(tmp_path):
    facade = NarrowFacade(tmp_path)
    facade.document.isSaved = False
    facade.design.computeAll = lambda: True
    action = {
        "request_id": str(uuid.uuid4()),
        "action": "cad.save_document",
        "target": {"document_id": "doc"},
        "version_description": "test",
    }

    with pytest.raises(DispatchError) as raised:
        facade.execute(action, {}, CancelToken())
    assert raised.value.code == "DOCUMENT_UNSAVED"


def test_preview_reads_snapshot_and_returns_plan_without_mutating(tmp_path):
    facade = NarrowFacade(tmp_path)
    result = facade.preview(_action("preview"), CancelToken())
    assert facade.parameter.expression == "2 mm"
    assert result["verification"] is None
    assert result["data"]["planned_changes"][0] == {
        "target_id": "parameter-token",
        "path": "parameter.expression",
        "kind": "updated",
        "before": "2 mm",
        "after": "3 mm",
    }


def test_parameter_execution_returns_observed_diff_and_verification(tmp_path):
    facade = NarrowFacade(tmp_path)
    result = facade.execute(_action(), {}, CancelToken())
    assert facade.parameter.expression == "3 mm"
    assert result["status"] == "success"
    assert result["changes"][0]["before"] == "2 mm"
    assert result["changes"][0]["after"] == "3 mm"
    assert result["verification"]["passed"] is True
    assert result["data"]["snapshot_id"] == result["_snapshot"]["snapshot_id"]
    assert result["_snapshot"]["expression"] == "2 mm"


def test_parameter_compensation_restores_semantic_snapshot(tmp_path):
    facade = NarrowFacade(tmp_path)
    action = _action()
    snapshot = facade.snapshot(action)
    facade.parameter.expression = "99 mm"
    facade.design.computeAll = lambda: True
    facade.compensate(action, snapshot)
    assert facade.parameter.expression == "2 mm"


@pytest.mark.parametrize(
    ("kind", "content"),
    [
        ("step", b"ISO-10303-21;\nHEADER;\nENDSEC;\n"),
        ("png", b"\x89PNG\r\n\x1a\nimage"),
        ("dxf", b"0\nSECTION\n2\nENTITIES\n"),
        ("f3d", b"PK\x03\x04archive"),
    ],
)
def test_export_artifact_signature_and_hash(kind, content, tmp_path):
    path = tmp_path / f"part.{kind}"
    path.write_bytes(content)
    size, digest = FusionApiFacade._artifact_metadata(path, kind)
    assert size == len(content)
    assert len(digest) == 64


def test_export_artifact_missing_or_wrong_signature_fails_closed(tmp_path):
    path = tmp_path / "part.step"
    path.write_bytes(b"not a STEP file")
    with pytest.raises(Exception) as raised:
        FusionApiFacade._artifact_metadata(path, "step")
    assert getattr(raised.value, "code", None) == "ARTIFACT_INVALID"


def test_binary_stl_signature_uses_declared_triangle_count(tmp_path):
    valid = tmp_path / "valid.stl"
    valid.write_bytes(b"binary stl".ljust(80, b"\0") + (1).to_bytes(4, "little") + b"\0" * 50)
    assert FusionApiFacade._artifact_metadata(valid, "stl")[0] == 134

    invalid = tmp_path / "invalid.stl"
    invalid.write_bytes(b"not an stl".ljust(84, b"\0"))
    with pytest.raises(Exception) as raised:
        FusionApiFacade._artifact_metadata(invalid, "stl")
    assert getattr(raised.value, "code", None) == "ARTIFACT_INVALID"


def test_text_parameter_summary_preserves_expression_without_numeric_value():
    parameter = SimpleNamespace(
        objectType="adsk::fusion::UserParameter", entityToken="text-token",
        name="label", expression='"demo"', value="demo", unit="",
        modelParameters=None, createdBy=None,
    )
    summary = FusionApiFacade._parameter_summary(parameter, "root-component")
    assert summary["expression"] == '"demo"'
    assert summary["value"] is None
    assert summary["unit"] is None
    assert summary["component_id"] == "root-component"


def test_model_parameter_summary_uses_created_feature_component():
    component = SimpleNamespace(entityToken="component-token")
    feature = SimpleNamespace(entityToken="feature-token", parentComponent=component)
    parameter = SimpleNamespace(
        objectType="adsk::fusion::ModelParameter", entityToken="parameter-token",
        name="distance", expression="2 mm", value=0.2, unit="cm",
        component=None, createdBy=feature,
    )

    summary = FusionApiFacade._parameter_summary(parameter, "root-component")

    assert summary["component_id"] == "component-token"
    assert summary["created_by_id"] == "feature-token"


@pytest.mark.parametrize(
    ("object_type", "expected_kind", "owner_path"),
    [
        ("adsk::fusion::Profile", "profile", "sketch"),
        ("adsk::fusion::SketchPoint", "sketch_point", "sketch"),
        ("adsk::fusion::BRepEdge", "edge", "body"),
        ("adsk::fusion::BRepFace", "face", "body"),
        ("adsk::fusion::ConstructionPlane", "construction_plane", "direct"),
    ],
)
def test_selection_summary_exposes_semantic_kind_and_owning_component(
    object_type, expected_kind, owner_path
):
    component = SimpleNamespace(entityToken="component-token")
    entity = SimpleNamespace(
        objectType=object_type, entityToken="selected-token", name="Selected",
        parentComponent=component if owner_path == "direct" else None,
        parentSketch=SimpleNamespace(parentComponent=component) if owner_path == "sketch" else None,
        parentBody=None,
        body=SimpleNamespace(parentComponent=component) if owner_path == "body" else None,
        isSuppressed=None, healthState=None,
    )

    kind = FusionApiFacade._selection_kind(entity)
    summary = FusionApiFacade._entity_summary(entity, kind)

    assert kind == expected_kind
    assert summary["component_id"] == "component-token"
    assert summary["vendor_extensions"]["autodesk:object_type"] == object_type


def test_artifact_verify_requires_runtime_file_evidence(tmp_path):
    facade = NarrowFacade(tmp_path)
    compute_calls = []
    facade.design.computeAll = lambda: compute_calls.append("computeAll") or True
    request = {
        "request_id": str(uuid.uuid4()),
        "specification": {
            "document_id": "doc", "source_request_id": str(uuid.uuid4()),
            "checks": [{"check": "artifact_valid", "artifact_id": "artifact"}],
        },
        "_references": {
            "source_result": {"artifacts": [{"artifact_id": "artifact"}]},
            "artifact_evidence": {"artifact": {"valid": True, "size_bytes": 10, "sha256": "a" * 64}},
        },
    }
    result = facade.verify(request, CancelToken())
    assert result["status"] == "success"
    assert result["verification"]["compute_completed"] is True
    assert compute_calls == ["computeAll"]
    request["_references"]["artifact_evidence"]["artifact"] = {"valid": False}
    failed = facade.verify(request, CancelToken())
    assert failed["status"] == "failed"


def test_standalone_verify_reports_failed_compute_instead_of_static_success(tmp_path):
    facade = NarrowFacade(tmp_path)
    facade.design.computeAll = lambda: False
    request = {
        "request_id": str(uuid.uuid4()),
        "specification": {
            "document_id": "doc",
            "checks": [{"check": "entity_resolves", "target_id": "parameter-token"}],
        },
    }

    result = facade.verify(request, CancelToken())

    assert result["status"] == "failed"
    assert result["verification"]["compute_completed"] is False
    assert result["verification"]["checks"][0] == {
        "check": "computeAll", "passed": False, "actual": False,
    }
    assert result["error"]["code"] == "COMPUTE_FAILED"


def test_no_new_feature_errors_uses_the_requested_snapshot_baseline(tmp_path):
    facade = NarrowFacade(tmp_path)
    facade.design.computeAll = lambda: True
    facade._feature_errors = lambda design: ["existing-feature-error"]
    snapshot_id = str(uuid.uuid4())
    request = {
        "request_id": str(uuid.uuid4()),
        "specification": {
            "document_id": "doc",
            "checks": [{"check": "no_new_feature_errors", "snapshot_id": snapshot_id}],
        },
        "_references": {
            "snapshot_feature_errors": {snapshot_id: ["existing-feature-error"]},
        },
    }

    result = facade.verify(request, CancelToken())

    assert result["status"] == "success"
    check = next(item for item in result["verification"]["checks"] if item["check"] == "no_new_feature_errors")
    assert check["actual"] == []


def test_export_recomputes_before_reading_export_manager(tmp_path, monkeypatch):
    facade = NarrowFacade(tmp_path)
    events = []

    class Manager:
        def createSTEPExportOptions(self, path):
            return path

        def execute(self, path):
            events.append("execute")
            Path(path).write_bytes(b"ISO-10303-21;\nHEADER;\nENDSEC;\n")
            return True

    class Design:
        def computeAll(self):
            events.append("computeAll")
            return True

        @property
        def exportManager(self):
            events.append("exportManager")
            return Manager()

    facade.design = Design()
    monkeypatch.setattr(facade, "_require_adsk", lambda: None)
    request_id = str(uuid.uuid4())
    result = facade._export(
        {
            "request_id": request_id, "action": "cad.export",
            "target": {"document_id": "doc"}, "format": "step", "filename": "part.step",
        },
        {
            "artifact_dir": str(tmp_path),
            "artifact_root_fingerprint": facade.config.artifact_root_fingerprint,
        },
        CancelToken(),
    )

    assert events == ["computeAll", "exportManager", "execute"]
    assert result["verification"]["compute_completed"] is True
    assert next(
        check for check in result["verification"]["checks"]
        if check["check"] == "no_new_feature_errors"
    )["actual"] == []


def test_export_fails_if_compute_surfaces_a_new_feature_error(tmp_path, monkeypatch):
    facade = NarrowFacade(tmp_path)
    facade.design.computeAll = lambda: True
    observed = iter([[], ["feature-token:failed after compute"]])
    facade._feature_errors = lambda _design: next(observed)
    monkeypatch.setattr(facade, "_require_adsk", lambda: None)

    with pytest.raises(DispatchError) as raised:
        facade._export(
            {
                "request_id": str(uuid.uuid4()), "action": "cad.export",
                "target": {"document_id": "doc"}, "format": "step",
                "filename": "part.step",
            },
            {
                "artifact_dir": str(tmp_path),
                "artifact_root_fingerprint": facade.config.artifact_root_fingerprint,
            },
            CancelToken(),
        )

    assert raised.value.code == "VERIFICATION_FAILED"
    assert raised.value.details["new_feature_errors"] == ["feature-token:failed after compute"]


def test_feature_summary_covers_all_feature_types_and_normalizes_health(tmp_path):
    class Collection:
        def __init__(self, values):
            self.values = values
            self.count = len(values)

        def item(self, index):
            return self.values[index]

    feature = SimpleNamespace(
        entityToken="feature-token", name="Revolve1", objectType="adsk::fusion::RevolveFeature",
        parentComponent=None, isSuppressed=False, healthState=2,
        errorOrWarningMessage="profile is invalid",
    )
    component = SimpleNamespace(features=Collection([feature]))
    facade = NarrowFacade(tmp_path)
    summaries = facade._feature_summaries([component])
    assert summaries[0]["vendor_extensions"]["autodesk:object_type"].endswith("RevolveFeature")
    assert summaries[0]["health_state"] == "error"
    design = SimpleNamespace(allComponents=Collection([component]))
    assert FusionApiFacade._feature_errors(facade, design) == ["feature-token:profile is invalid"]


def test_property_verification_reads_observed_values(tmp_path):
    facade = NarrowFacade(tmp_path)
    entity = SimpleNamespace(
        entityToken="entity", name="Bracket", partNumber="P-42", description="verified",
        material=SimpleNamespace(id="material-id"),
    )
    facade.design = SimpleNamespace(
        findEntityByToken=lambda token: [entity] if token == "entity" else [],
        computeAll=lambda: True,
    )
    action = {
        "action": "cad.update_entity_properties",
        "target": {"document_id": "doc", "entity_id": "entity"},
        "properties": {
            "name": "Bracket", "part_number": "P-42", "description": "verified",
            "material": {"library_id": "library-id", "material_id": "material-id"},
        },
    }
    verification = FusionApiFacade._verify_after_mutation(
        facade, facade.design, action, ["entity"], []
    )
    assert verification["passed"] is True
    assert {check["check"] for check in verification["checks"]} >= {
        "property_equals", "material_equals", "entity_resolves", "no_new_feature_errors",
    }


def test_property_update_rejects_entity_kind_mismatch(tmp_path):
    facade = NarrowFacade(tmp_path)
    entity = SimpleNamespace(
        entityToken="entity", objectType="adsk::fusion::BRepBody", name="Body",
    )
    design = SimpleNamespace(
        findEntityByToken=lambda token: [entity] if token == "entity" else [],
    )
    action = {
        "action": "cad.update_entity_properties",
        "target": {
            "document_id": "doc", "entity_id": "entity", "entity_kind": "component",
        },
        "properties": {"name": "Renamed"},
    }

    with pytest.raises(DispatchError) as raised:
        facade._update_properties(design, action)

    assert raised.value.code == "INVALID_ACTION"
    assert entity.name == "Body"


def test_save_recomputes_and_reports_local_vs_cloud_state(tmp_path):
    facade = NarrowFacade(tmp_path)
    data_file = SimpleNamespace(versionId="v2", isComplete=False)
    document = SimpleNamespace(
        isSaved=True, isModified=False, dataFile=data_file, creationId="doc",
        save=lambda description: description == "checkpoint",
    )
    design = SimpleNamespace(computeAll=lambda: True)
    result = facade._save(
        document, design,
        {
            "request_id": str(uuid.uuid4()), "action": "cad.save_document",
            "version_description": "checkpoint",
        },
        [],
        save_as=False,
    )
    assert result["status"] == "success"
    assert result["data"]["local_save_accepted"] is True
    assert result["data"]["cloud_version_processing"] == "pending"
    assert result["verification"]["compute_completed"] is True


def test_created_feature_verification_checks_observed_object_type(tmp_path):
    facade = NarrowFacade(tmp_path)
    feature = SimpleNamespace(entityToken="created", objectType="adsk::fusion::ExtrudeFeature")
    design = SimpleNamespace(
        computeAll=lambda: True,
        findEntityByToken=lambda token: [feature] if token == "created" else [],
    )
    verification = FusionApiFacade._verify_after_mutation(
        facade,
        design,
        {"action": "cad.create_extrude"},
        ["created"],
        [],
    )
    type_check = next(item for item in verification["checks"] if item["check"] == "feature_type_equals")
    assert type_check["passed"] is True


def test_timeline_summary_reads_real_timeline_object_and_parent_group(tmp_path):
    parent = SimpleNamespace(index=2, name="Mounting features")
    entity = SimpleNamespace(
        entityToken="extrude-token", objectType="adsk::fusion::ExtrudeFeature",
    )
    timeline_object = SimpleNamespace(
        index=3, name="Extrude1", entity=entity, healthState=1,
        errorOrWarningMessage="profile warning", isGroup=False,
        isRolledBack=False, isSuppressed=False, parentGroup=parent,
    )
    timeline = SimpleNamespace(count=1, item=lambda index: timeline_object)

    summaries, truncated = FusionApiFacade._timeline_summaries(timeline)

    assert truncated is False
    assert summaries == [{
        "index": 3,
        "name": "Extrude1",
        "kind": "adsk::fusion::ExtrudeFeature",
        "entity_id": "extrude-token",
        "health_state": "warning",
        "is_group": False,
        "is_rolled_back": False,
        "is_suppressed": False,
        "parent": {"index": 2, "name": "Mounting features"},
        "error_or_warning": "profile warning",
    }]
