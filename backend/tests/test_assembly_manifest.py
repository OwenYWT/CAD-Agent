from app.agent.assembly_manifest import changed_part_ids, code_hash, enrich_assembly_parts


def test_enrich_assembly_parts_adds_stable_ids_and_hashes():
    parts = [
        {"name": "Base Plate", "code": "result = base"},
        {"name": "Lid", "code": "result = lid"},
    ]

    enriched = enrich_assembly_parts(parts)

    assert enriched[0]["part_id"] == "base-plate"
    assert enriched[0]["code_hash"]
    assert enriched[1]["part_id"] == "lid"
    assert enriched[1]["code_hash"]
    assert enrich_assembly_parts(enriched)[0]["part_id"] == "base-plate"
    assert enrich_assembly_parts(enriched)[0]["code_hash"] == enriched[0]["code_hash"]


def test_changed_part_ids_uses_code_hash_not_position():
    before = enrich_assembly_parts([
        {"name": "base", "code": "result = base"},
        {"name": "lid", "code": "result = lid"},
    ])
    after = enrich_assembly_parts([
        {"name": "lid", "code": "result = lid modified"},
        {"name": "base", "code": "result = base"},
    ])

    changes = changed_part_ids(before, after)

    assert changes["changed"] == ["lid"]
    assert changes["unchanged"] == ["base"]
    assert changes["added"] == []
    assert changes["removed"] == []


def test_enrich_assembly_parts_normalizes_duplicate_explicit_ids():
    enriched = enrich_assembly_parts([
        {"part_id": "Base Plate", "name": "left", "code": "result = left"},
        {"part_id": "Base Plate", "name": "right", "code": "result = right"},
    ])

    assert [part["part_id"] for part in enriched] == ["base-plate", "base-plate-2"]


def test_enrich_assembly_parts_recomputes_stale_hash_when_code_is_present():
    enriched = enrich_assembly_parts([
        {
            "part_id": "lid",
            "name": "lid",
            "code": "result = updated_lid",
            "code_hash": "stale-hash",
        },
    ])

    assert enriched[0]["code_hash"] == code_hash("result = updated_lid")
