import subprocess
import sys

from app.sandbox.code_analyzer import analyze_code, analyze_execution_code


def test_analyzer_rejects_parallel_face_selector_for_workplane():
    warnings = analyze_code(
        "result = body.faces(\"|Y\").workplane().hole(4)\nshow_object(result)"
    )

    assert any("Selected faces must be co-planar" in warning for warning in warnings)


def test_analyzer_allows_single_direction_face_selector():
    warnings = analyze_code(
        "result = body.faces(\">Y\").workplane().hole(4)\nshow_object(result)"
    )

    assert not any("Selected faces must be co-planar" in warning for warning in warnings)


def test_analyzer_rejects_unsupported_workplane_cleanup():
    warnings = analyze_code(
        "result = cq.Workplane(\"XY\").rect(10, 10).offset2D(-1).cleanup()\n"
        "show_object(result)"
    )

    assert any("Workplane.cleanup()" in warning for warning in warnings)


def test_analyzer_rejects_subtracting_body_derived_cut_result_twice():
    warnings = analyze_code(
        "result = cq.Workplane('XY').box(90, 60, 32)\n"
        "top_wp = result.faces('>Z').workplane()\n"
        "groove_profile = top_wp.rect(84, 54)\n"
        "groove_cut = groove_profile.rect(79.2, 49.2).cutBlind(-1.5)\n"
        "result = result - groove_cut\n"
        "show_object(result)\n"
    )

    assert any("already applies the subtraction" in warning for warning in warnings)


def test_analyzer_rejects_cutting_body_with_body_derived_cut_result():
    warnings = analyze_code(
        "body = cq.Workplane('XY').box(90, 60, 32)\n"
        "top_wp = body.faces('>Z').workplane()\n"
        "modified_body = top_wp.rect(84, 54).cutThruAll()\n"
        "body = body.cut(modified_body)\n"
        "show_object(body)\n"
    )

    assert any("already applies the subtraction" in warning for warning in warnings)


def test_analyzer_does_not_block_across_conditional_control_flow():
    warnings = analyze_code(
        "result = cq.Workplane('XY').box(90, 60, 32)\n"
        "top_wp = result.faces('>Z').workplane()\n"
        "if groove_depth > 0:\n"
        "    groove_cut = top_wp.rect(84, 54).cutBlind(-groove_depth)\n"
        "result = result - groove_cut\n"
        "show_object(result)\n"
    )

    assert not any("already applies the subtraction" in warning for warning in warnings)


def test_analyzer_allows_assigning_body_derived_cut_result_directly():
    warnings = analyze_code(
        "result = cq.Workplane('XY').box(90, 60, 32)\n"
        "top_wp = result.faces('>Z').workplane()\n"
        "result = top_wp.rect(84, 54).cutBlind(-1.5)\n"
        "show_object(result)\n"
    )

    assert not any("already applies the subtraction" in warning for warning in warnings)


def test_analyzer_allows_subtracting_independent_cutter():
    warnings = analyze_code(
        "result = cq.Workplane('XY').box(90, 60, 32)\n"
        "cutter = cq.Workplane('XY').rect(84, 54).extrude(1.5)\n"
        "result = result - cutter\n"
        "show_object(result)\n"
    )

    assert not any("already applies the subtraction" in warning for warning in warnings)


def test_analyzer_completes_for_loop_with_show_object_after_loop():
    source = (
        "for position in positions:\n"
        + "    result = result.union(feature.translate(position))\n" * 120
        + "show_object(result)\n"
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-c",
            (
                "import sys; "
                "from app.sandbox.code_analyzer import analyze_code; "
                "print(repr(analyze_code(sys.stdin.read())))"
            ),
        ],
        input=source,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=2,
        check=True,
    )

    assert "show_object()" not in completed.stdout


def test_analyzer_rejects_show_object_inside_loop():
    warnings = analyze_code(
        "for feature in features:\n"
        "    result = result.union(feature)\n"
        "    show_object(result)\n"
    )

    assert any("show_object()" in warning and "loop" in warning for warning in warnings)


def test_analyzer_allows_unrelated_parallel_selector_and_workplane():
    warnings = analyze_code(
        "side_faces = body.faces('|Y')\n"
        "result = body.faces('>Z').workplane().hole(2)\n"
        "show_object(result)\n"
    )

    assert not any("Selected faces must be co-planar" in warning for warning in warnings)


def test_analyzer_ignores_show_object_text_in_comments_and_strings():
    warnings = analyze_code(
        "message = 'show_object(fake)'\n"
        "# show_object(fake)\n"
        "result = body\n"
        "show_object(result)\n"
    )

    assert not any("only one" in warning.lower() or "1 次" in warning for warning in warnings)


def test_analyzer_rejects_double_subtraction_assigned_to_new_name():
    warnings = analyze_code(
        "body = cq.Workplane('XY').box(10, 10, 10)\n"
        "modified = body.faces('>Z').workplane().hole(2)\n"
        "final = body - modified\n"
        "show_object(final)\n"
    )

    assert any("already applies the subtraction" in warning for warning in warnings)


def test_analyzer_rejects_augmented_double_subtraction():
    warnings = analyze_code(
        "body = cq.Workplane('XY').box(10, 10, 10)\n"
        "modified = body.faces('>Z').workplane().hole(2)\n"
        "body -= modified\n"
        "show_object(body)\n"
    )

    assert any("already applies the subtraction" in warning for warning in warnings)


def test_analyzer_does_not_block_inside_function_scope():
    warnings = analyze_code(
        "def modify(body):\n"
        "    modified = body.faces('>Z').workplane().hole(2)\n"
        "    return body - modified\n"
        "result = modify(source)\n"
        "show_object(result)\n"
    )

    assert not any("already applies the subtraction" in warning for warning in warnings)


def test_analyzer_does_not_join_conditional_provenance_paths():
    warnings = analyze_code(
        "body = cq.Workplane('XY').box(10, 10, 10)\n"
        "if use_hole:\n"
        "    candidate = body.faces('>Z').workplane().hole(2)\n"
        "else:\n"
        "    candidate = cq.Workplane('XY').circle(1).extrude(10)\n"
        "result = body - candidate\n"
        "show_object(result)\n"
    )

    assert not any("already applies the subtraction" in warning for warning in warnings)


def test_analyzer_rejects_show_object_inside_comprehension():
    warnings = analyze_code(
        "result = [show_object(feature) for feature in features]\n"
    )

    assert any("show_object()" in warning and "loop" in warning for warning in warnings)


def test_execution_analyzer_does_not_summarize_function_returns():
    warnings = analyze_execution_code(
        "def modified(body):\n"
        "    return body.faces('>Z').workplane().hole(2)\n"
        "body = cq.Workplane('XY').box(10, 10, 10)\n"
        "candidate = modified(body)\n"
        "result = body - candidate\n"
        "show_object(result)\n"
    )

    assert not any("already applies the subtraction" in warning for warning in warnings)


def test_execution_analyzer_does_not_infer_loop_target_provenance():
    warnings = analyze_execution_code(
        "for body in bodies:\n"
        "    modified = body.faces('>Z').workplane().hole(2)\n"
        "    result = body - modified\n"
        "show_object(result)\n"
    )

    assert not any("already applies the subtraction" in warning for warning in warnings)


def test_execution_analyzer_allows_subtracting_derivative_from_rebound_body():
    warnings = analyze_execution_code(
        "body = cq.Workplane('XY').box(10, 10, 10)\n"
        "modified = body.faces('>Z').workplane().hole(2)\n"
        "body = cq.Workplane('XY').box(20, 20, 20)\n"
        "result = body - modified\n"
        "show_object(result)\n"
    )

    assert not any("already applies the subtraction" in warning for warning in warnings)


def test_execution_analyzer_preserves_mutually_exclusive_branch_relations():
    warnings = analyze_execution_code(
        "a = cq.Workplane('XY').box(10, 10, 10)\n"
        "b = cq.Workplane('XY').box(20, 20, 20)\n"
        "if cond:\n"
        "    parent = a\n"
        "    candidate = b.faces('>Z').workplane().hole(2)\n"
        "else:\n"
        "    parent = b\n"
        "    candidate = a.faces('>Z').workplane().hole(2)\n"
        "result = parent - candidate\n"
        "show_object(result)\n"
    )

    assert not any("already applies the subtraction" in warning for warning in warnings)


def test_execution_analyzer_allows_parallel_faces_narrowed_to_first():
    warnings = analyze_execution_code(
        "result = body.faces('|Y').first().workplane().hole(2)\n"
        "show_object(result)\n"
    )

    assert not any("Selected faces must be co-planar" in warning for warning in warnings)


def test_execution_analyzer_allows_cleanup_on_unrelated_object():
    warnings = analyze_execution_code(
        "class Resource:\n"
        "    def cleanup(self):\n"
        "        self.closed = True\n"
        "resource = Resource()\n"
        "resource.cleanup()\n"
        "result = cq.Workplane('XY').box(1, 1, 1)\n"
        "show_object(result)\n"
    )

    assert not any("Workplane.cleanup()" in warning for warning in warnings)


def test_execution_analyzer_skips_conditional_path_expansion():
    branches = "".join(
        f"if flag_{index}:\n"
        f"    value_{index} = cq.Workplane('XY').box({index + 1}, 1, 1)\n"
        "else:\n"
        f"    value_{index} = cq.Workplane('XY').box({index + 2}, 1, 1)\n"
        for index in range(1, 18)
    )
    source = (
        "a = cq.Workplane('XY').box(10, 10, 10)\n"
        "b = cq.Workplane('XY').box(20, 20, 20)\n"
        "if flag_0:\n"
        "    parent = a\n"
        "    candidate = b.faces('>Z').workplane().hole(2)\n"
        "else:\n"
        "    parent = a\n"
        "    candidate = a.faces('>Z').workplane().hole(2)\n"
        + branches
        + "result = parent - candidate\n"
        + "show_object(result)\n"
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-c",
            (
                "import sys; "
                "from app.sandbox.code_analyzer import analyze_execution_code; "
                "print(repr(analyze_execution_code(sys.stdin.read())))"
            ),
        ],
        input=source,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=2,
        check=True,
    )

    assert completed.stdout.strip() == "[]"


def test_execution_analyzer_tracks_destructured_values_independently():
    warnings = analyze_execution_code(
        "a, b = (\n"
        "    cq.Workplane('XY').box(10, 10, 10),\n"
        "    cq.Workplane('XY').box(20, 20, 20),\n"
        ")\n"
        "candidate = b.faces('>Z').workplane().hole(2)\n"
        "result = a - candidate\n"
        "show_object(result)\n"
    )

    assert not any("already applies the subtraction" in warning for warning in warnings)


def test_execution_analyzer_does_not_infer_function_defaults():
    warnings = analyze_execution_code(
        "body = cq.Workplane('XY').box(10, 10, 10)\n"
        "def modified(source=body):\n"
        "    return source.faces('>Z').workplane().hole(2)\n"
        "candidate = modified()\n"
        "result = body - candidate\n"
        "show_object(result)\n"
    )

    assert not any("already applies the subtraction" in warning for warning in warnings)


def test_execution_analyzer_does_not_infer_function_globals():
    warnings = analyze_execution_code(
        "body = cq.Workplane('XY').box(10, 10, 10)\n"
        "def modified():\n"
        "    return body.faces('>Z').workplane().hole(2)\n"
        "body = cq.Workplane('XY').box(20, 20, 20)\n"
        "candidate = modified()\n"
        "result = body - candidate\n"
        "show_object(result)\n"
    )

    assert not any("already applies the subtraction" in warning for warning in warnings)


def test_execution_analyzer_invalidates_names_changed_by_control_flow():
    warnings = analyze_execution_code(
        "body = cq.Workplane('XY').box(10, 10, 10)\n"
        "modified = body.faces('>Z').workplane().hole(2)\n"
        "if replace_body:\n"
        "    body = cq.Workplane('XY').box(20, 20, 20)\n"
        "result = body - modified\n"
        "show_object(result)\n"
    )

    assert not any("already applies the subtraction" in warning for warning in warnings)


def test_execution_analyzer_scales_for_long_linear_source():
    source = (
        "result = cq.Workplane('XY').box(1, 1, 1)\n"
        + "".join(
            f"result = result.union(feature_{index})\n"
            for index in range(5000)
        )
        + "show_object(result)\n"
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-c",
            (
                "import sys; "
                "from app.sandbox.code_analyzer import analyze_execution_code; "
                "print(repr(analyze_execution_code(sys.stdin.read())))"
            ),
        ],
        input=source,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=2,
        check=True,
    )

    assert completed.stdout.strip() == "[]"


def test_execution_analyzer_does_not_taint_subtractive_call_arguments():
    warnings = analyze_execution_code(
        "body = cq.Workplane('XY').box(10, 10, 10)\n"
        "cutter_body = cq.Workplane('XY').box(5, 5, 5)\n"
        "candidate = cutter_body.faces('>Z').workplane().hole(body.val().Volume())\n"
        "result = body - candidate\n"
        "show_object(result)\n"
    )

    assert not any("already applies the subtraction" in warning for warning in warnings)


def test_execution_analyzer_invalidates_module_name_written_by_class_global():
    warnings = analyze_execution_code(
        "body = cq.Workplane('XY').box(10, 10, 10)\n"
        "modified = body.faces('>Z').workplane().hole(2)\n"
        "class Replacement:\n"
        "    global body\n"
        "    body = cq.Workplane('XY').box(20, 20, 20)\n"
        "result = body - modified\n"
        "show_object(result)\n"
    )

    assert not any("already applies the subtraction" in warning for warning in warnings)


def test_execution_analyzer_invalidates_global_names_declared_by_function():
    warnings = analyze_execution_code(
        "body = cq.Workplane('XY').box(10, 10, 10)\n"
        "modified = body.faces('>Z').workplane().hole(2)\n"
        "def replace_body():\n"
        "    global body\n"
        "    body = cq.Workplane('XY').box(20, 20, 20)\n"
        "replace_body()\n"
        "result = body - modified\n"
        "show_object(result)\n"
    )

    assert not any("already applies the subtraction" in warning for warning in warnings)


def test_execution_analyzer_invalidates_match_capture_names():
    warnings = analyze_execution_code(
        "body = cq.Workplane('XY').box(10, 10, 10)\n"
        "modified = body.faces('>Z').workplane().hole(2)\n"
        "match cq.Workplane('XY').box(20, 20, 20):\n"
        "    case body:\n"
        "        pass\n"
        "result = body - modified\n"
        "show_object(result)\n"
    )

    assert not any("already applies the subtraction" in warning for warning in warnings)


def test_execution_analyzer_fails_open_for_tuple_subscript_rhs():
    warnings = analyze_execution_code(
        "body = cq.Workplane('XY').box(10, 10, 10)\n"
        "cutter = cq.Workplane('XY').box(5, 5, 5)\n"
        "modified = (\n"
        "    body,\n"
        "    cutter.faces('>Z').workplane().hole(2),\n"
        ")[1]\n"
        "result = body - modified\n"
        "show_object(result)\n"
    )

    assert not any("already applies the subtraction" in warning for warning in warnings)
