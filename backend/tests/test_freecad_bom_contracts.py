from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.freecad.bom_contracts import (
    FreeCADBOMComponentV1,
    FreeCADBOMDocumentV1,
    FreeCADBOMRequestV1,
)


def _request(**overrides) -> FreeCADBOMRequestV1:
    values = {
        "candidate_build_id": uuid4(),
        "base_revision_id": uuid4(),
        "plan_hash": "a" * 64,
        "runtime_image_digest": f"sha256:{'b' * 64}",
        "combine_step_key": "combine",
        "components": (
            FreeCADBOMComponentV1(
                step_key="housing",
                label="Housing",
                position_mm=(0, 0, 0),
                artifact_id="component:housing",
            ),
        ),
        "property_columns": (),
    }
    values.update(overrides)
    return FreeCADBOMRequestV1(**values)


def test_bom_request_is_strict_and_rejects_duplicate_components():
    request = _request()
    assert request.components[0].quantity == 1
    with pytest.raises(ValidationError, match="must be unique"):
        _request(components=(request.components[0], request.components[0]))
    with pytest.raises(ValidationError):
        FreeCADBOMRequestV1(**{
            **request.model_dump(),
            "unexpected": True,
        })


@pytest.mark.parametrize('version', ['1.1.3', '1.1.4'])
def test_bom_document_reads_current_and_saved_native_versions(version):
    source = {
        "candidate_build_id": str(uuid4()),
        "base_revision_id": str(uuid4()),
        "plan_hash": "c" * 64,
    }
    generator = {
        "freecad_version": version,
        "native_type": "Assembly::BomObject",
    }
    document = FreeCADBOMDocumentV1(
        source=source,
        generator=generator,
        columns=("Index", "Name", "Quantity", "File"),
        rows=({
            "index": "1",
            "name": "Housing",
            "quantity": 1,
            "file_name": "housing.step",
            "properties": {},
        },),
    )
    assert document.rows[0].name == "Housing"
    assert document.generator['freecad_version'] == version
    for unsupported in ('0.20.2', '1.1.5', None):
        with pytest.raises(ValidationError, match='version mismatch'):
            FreeCADBOMDocumentV1(source=source,
                generator={**generator, 'freecad_version': unsupported},
                columns=document.columns, rows=document.rows)
    with pytest.raises(ValidationError, match="native Assembly"):
        FreeCADBOMDocumentV1(
            source=source,
            generator={**generator, "native_type": "Spreadsheet::Sheet"},
            columns=document.columns,
            rows=document.rows,
        )
    with pytest.raises(ValidationError):
        FreeCADBOMDocumentV1(
            source=source,
            generator=generator,
            columns=document.columns,
            rows=(),
        )


def test_bom_selects_the_delivered_manifest_for_each_logical_step():
    from app.workflows.handlers.bom import _select_bom_inputs
    from temporalio.exceptions import ApplicationError

    def row(key):
        return {'id': uuid4(), 'manifest': {'plan_step_key': key}}

    old, repaired, optional, part, new_part = (row(key) for key in ('combine', 'combine', 'combine', 'leg', 'leg'))
    rows = [old, part, repaired, new_part, optional]
    chosen = {'combine': str(repaired['id']), 'leg': str(new_part['id'])}
    assert _select_bom_inputs(rows, ['combine', 'leg'], chosen) == {'combine': repaired, 'leg': new_part}
    for wrong in ({**chosen, 'leg': str(old['id'])}, {**chosen, 'combine': str(uuid4())}):
        with pytest.raises(ApplicationError, match='missing'):
            _select_bom_inputs(rows, ['combine', 'leg'], wrong)
    with pytest.raises(ApplicationError, match='ambiguous'):
        _select_bom_inputs(rows, ['combine', 'leg'], {'combine': str(repaired['id'])})


@pytest.mark.asyncio
async def test_assembly_bom_never_waives_integrity_or_a_second_geometry_mismatch():
    from app.workflows.agent_v2 import McadAgentWorkflowV2
    from temporalio.exceptions import ApplicationError

    original = {'executed': {'staging_manifest_id': 'original'}}
    optimized = {'executed': {'staging_manifest_id': 'optional'}, 'previous_dfm_assembly': (original, 'dfm-01')}
    for code, expected in [('bom_input_integrity_failed', ['optional']),
                           ('bom_source_geometry_mismatch', ['optional', 'original'])]:
        flow = McadAgentWorkflowV2()
        seen = []
        async def fail(name, payload, **kwargs):
            seen.append(payload['staging_manifest_id'])
            raise ApplicationError(code, type=code)
        flow._activity = fail
        with pytest.raises(ApplicationError, match=code):
            await flow._assembly_bom({}, optimized, {})
        assert seen == expected
