from app.storage.postgres_history import _source_parameters


# Regression: QA ISSUE-002 — durable history reload dropped editable parameters
def test_durable_history_projects_parameters_from_immutable_source_code():
    parameters = _source_parameters(
        "# [基础尺寸]\n"
        "# 外壳宽度\n"
        "width_mm = 32.0  # [10:1:100]\n"
        "height_mm = 12.0  # [5:1:50]\n"
        "result = width_mm * height_mm\n"
    )

    assert parameters is not None
    assert [parameter["name"] for parameter in parameters] == [
        "width_mm",
        "height_mm",
    ]
    assert parameters[0]["group"] == "基础尺寸"
    assert parameters[0]["unit"] == "mm"
    assert parameters[0]["min"] == 10
    assert parameters[0]["max"] == 100
