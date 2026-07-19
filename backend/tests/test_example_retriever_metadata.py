import json

import pytest

from app.examples.retriever import ExampleRetriever


def write_example(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.mark.asyncio
async def test_metadata_criteria_boosts_matching_printable_example(tmp_path):
    write_example(
        tmp_path / "generic_hook.json",
        {
            "id": "generic_hook",
            "category": "hook",
            "part_type": "custom",
            "tags": ["wall", "mount"],
            "description": "wall mounted object",
            "description_en": "generic wall mounted object",
            "features_used": ["fillet"],
            "code": "generic_code",
        },
    )
    write_example(
        tmp_path / "printable_bracket.json",
        {
            "id": "printable_bracket",
            "category": "bracket",
            "part_type": "bracket",
            "tags": ["wall", "mount"],
            "description": "wall mounted object",
            "description_en": "generic wall mounted object",
            "features_used": ["mounting holes", "ribs", "fillet"],
            "modeling_hints": ["extrude_cut"],
            "manufacturing_notes": ["Keep wall thickness above 2 mm"],
            "failure_modes": ["Fillets fail if rib roots are too small"],
            "print_profile": {"process": "FDM", "min_wall_mm": 1.2},
            "code": "bracket_code",
        },
    )

    retriever = ExampleRetriever(str(tmp_path))
    results = await retriever.find_similar(
        "wall mounted object",
        top_k=2,
        part_type="bracket",
        features=["mounting holes", "ribs"],
        modeling_hint="extrude_cut",
    )

    assert results[0]["code"] == "bracket_code"
    assert results[0]["part_type"] == "bracket"
    assert results[0]["features_used"] == ["mounting holes", "ribs", "fillet"]
    assert results[0]["manufacturing_notes"] == ["Keep wall thickness above 2 mm"]
    assert results[0]["failure_modes"] == ["Fillets fail if rib roots are too small"]
    assert results[0]["print_profile"] == {"process": "FDM", "min_wall_mm": 1.2}
    assert results[0]["modeling_hints"] == ["extrude_cut"]
    assert results[0]["score"] > results[1]["score"]


@pytest.mark.asyncio
async def test_metadata_is_included_in_indexed_text(tmp_path):
    write_example(
        tmp_path / "snap_fit.json",
        {
            "id": "snap_fit",
            "category": "clip",
            "part_type": "custom",
            "tags": [],
            "description": "small clip",
            "description_en": "small clip",
            "features_used": ["snap fit", "living hinge relief"],
            "manufacturing_notes": ["Add root radius for FDM snap tabs"],
            "failure_modes": ["Snap tabs break when printed across layer lines"],
            "code": "snap_code",
        },
    )

    retriever = ExampleRetriever(str(tmp_path))
    results = await retriever.find_similar("FDM snap tabs with root radius", top_k=1)

    assert results[0]["code"] == "snap_code"

from app.agent.code_gen import CodeGenerator


def test_code_generator_formats_printable_example_metadata():
    formatted = CodeGenerator()._format_examples([
        {
            "description": "Printable bracket",
            "category": "bracket",
            "part_type": "bracket",
            "features_used": ["mounting holes", "ribs"],
            "modeling_hints": ["extrude_cut"],
            "manufacturing_notes": ["Keep wall thickness above 2 mm"],
            "failure_modes": ["Fillets fail if too large"],
            "print_profile": {"process": "FDM"},
            "code": "result = None",
        }
    ])

    assert "mounting holes" in formatted
    assert "Keep wall thickness above 2 mm" in formatted
    assert "Fillets fail if too large" in formatted
    assert "result = None" in formatted
