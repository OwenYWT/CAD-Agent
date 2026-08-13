from app.agent.assembly_manifest import enrich_assembly_parts, changed_part_ids


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
