from app.storage.postgres_history import _durable_revision_projection


# Regression: QA ISSUE-004 — native durable revisions were presented as legacy
def test_native_mcad_revision_projects_restorable_version_metadata():
    projection = _durable_revision_projection(
        {
            "schema_version": "mcad-revision-manifest.v1",
            "objective": "把宽度修改为 28 mm",
            "executions": [
                {
                    "operation": "execute",
                    "source_code": (
                        "width_mm = 28  # [10:1:100]\n"
                        "result = width_mm\n"
                    ),
                    "outputs": [
                        {"name": "step"},
                        {"name": "stl"},
                    ],
                }
            ],
        }
    )

    assert projection is not None
    assert projection["source"] == "execute_code"
    assert projection["prompt"] == "把宽度修改为 28 mm"
    assert "width_mm = 28" in projection["code"]
    assert projection["parameters"][0]["name"] == "width_mm"
    assert projection["available_exports"] == ["step", "stl"]
