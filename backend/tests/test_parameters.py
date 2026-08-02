from app.parameters import apply_parameter_values, extract_parameters


CODE = '''\n# [Body]\n# 外壳宽度\nwidth_mm = 80  # [40:1:160]\n\n# 外壳深度\ndepth_mm = 50.5  # [30:120]\n\n# [Holes]\n# 螺丝孔直径\nhole_diameter_mm = 3.4  # [2:0.1:8]\nnegative_offset_mm = -2.5  # [-10:0.5:10]\nname = "not numeric"\n'''


def test_extracts_grouped_numeric_parameters_with_ranges_and_units():
    params = extract_parameters(CODE)

    assert [p.name for p in params] == [
        "width_mm",
        "depth_mm",
        "hole_diameter_mm",
        "negative_offset_mm",
    ]

    width = params[0]
    assert width.value == 80
    assert width.default_value == 80
    assert width.group == "Body"
    assert width.comment == "外壳宽度"
    assert width.unit == "mm"
    assert width.min == 40
    assert width.step == 1
    assert width.max == 160

    depth = params[1]
    assert depth.value == 50.5
    assert depth.min == 30
    assert depth.step is None
    assert depth.max == 120

    hole = params[2]
    assert hole.group == "Holes"
    assert hole.min == 2
    assert hole.step == 0.1
    assert hole.max == 8

    offset = params[3]
    assert offset.value == -2.5
    assert offset.min == -10
    assert offset.step == 0.5
    assert offset.max == 10


def test_apply_parameter_values_replaces_only_supported_top_level_assignments():
    updated = apply_parameter_values(
        CODE,
        {
            "width_mm": 96,
            "hole_diameter_mm": 4.2,
            "missing_mm": 123,
        },
    )

    assert "width_mm = 96  # [40:1:160]" in updated
    assert "hole_diameter_mm = 4.2  # [2:0.1:8]" in updated
    assert "depth_mm = 50.5  # [30:120]" in updated
    assert "missing_mm" not in updated


def test_apply_parameter_values_rejects_out_of_range_values():
    try:
        apply_parameter_values(CODE, {"width_mm": 999})
    except ValueError as exc:
        assert "width_mm" in str(exc)
        assert "range" in str(exc)
    else:
        raise AssertionError("expected range validation failure")


def test_extract_parameters_ignores_non_literal_range_without_losing_parameter():
    code = (
        "height_mm = 20  # [5:1:50]\n"
        "wall_mm = 2  # [0.5:0.1:height_mm - 1.0]\n"
    )

    params = extract_parameters(code)

    assert [parameter.name for parameter in params] == ["height_mm", "wall_mm"]
    wall = params[1]
    assert wall.value == 2
    assert wall.min is None
    assert wall.step is None
    assert wall.max is None
