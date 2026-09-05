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


def test_bom_document_requires_native_freecad_113_and_nonempty_rows():
    source = {
        "candidate_build_id": str(uuid4()),
        "base_revision_id": str(uuid4()),
        "plan_hash": "c" * 64,
    }
    generator = {
        "freecad_version": "1.1.3",
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
