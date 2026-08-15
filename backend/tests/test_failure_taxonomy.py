"""Failure taxonomy (A1) — verify classify() is total and correct, that the prompt
table is generated from the single source of truth, and that every legacy _ERROR_HINTS
pattern + ERROR_FIX_PROMPT keyword still maps to a class.

Pure unit tests — no Docker/LLM/orchestrator.
"""
import pytest

from app.agent.failure_taxonomy import (
    FAILURE_CLASSES,
    FixPath,
    classify,
    fix_hint_for,
    render_prompt_table,
)


# Every substring that the legacy _ERROR_HINTS list matched on must still classify.
LEGACY_HINT_SUBSTRINGS = {
    "at least one solid on the stack": "empty_stack_boolean",
    "BRep_API: not done": "fillet_too_large",
    "shell failed": "shell_failed",
    "StdFail_NotDone": "shell_failed",
    "Wire is not closed": "wire_not_closed",
    "Geom_UndefinedDerivative": "revolve_crosses_axis",
    "not enough wires": "loft_insufficient_wires",
    "Standard_NullObject": "null_selector",
}

# Extra keywords that only lived in the ERROR_FIX_PROMPT markdown table.
PROMPT_ONLY_KEYWORDS = {
    "ModuleNotFoundError": "disallowed_import",
    "SyntaxError": "syntax_error",
    "maximum recursion": "infinite_recursion",
}


@pytest.mark.parametrize("substr,key", list(LEGACY_HINT_SUBSTRINGS.items()))
def test_legacy_hint_substrings_classify(substr, key):
    assert classify(None, f"error: {substr} here").key == key


@pytest.mark.parametrize("substr,key", list(PROMPT_ONLY_KEYWORDS.items()))
def test_prompt_table_keywords_classify(substr, key):
    assert classify(None, f"{substr}: detail").key == key


def test_substring_matches_traceback_too():
    """classify scans message AND traceback."""
    fc = classify("SomeError", "generic message", traceback="...Standard_NullObject...")
    assert fc.key == "null_selector"


def test_case_insensitive():
    assert classify(None, "BREP_API: NOT DONE").key == "fillet_too_large"


# --- gate-anchored classes take precedence over substrings ---

def test_gate_geometry_error():
    # even if the message contains an OCCT substring, the gate wins
    fc = classify(None, "Wire is not closed", gate="GeometryError")
    assert fc.key == "geometry_invalid"


def test_gate_static_analysis():
    assert classify(None, "anything", gate="StaticAnalysis").key == "static_analysis"


def test_gate_validation_error_maps_disallowed_import():
    # the import-whitelist gate message ("禁止导入模块: os") classifies by substring
    assert classify("ValidationError", "禁止导入模块: os", gate="ValidationError").key == "disallowed_import"


@pytest.mark.parametrize("etype,key,path,retry_budget", [
    ("SandboxUnavailable", "sandbox_unavailable", FixPath.HARD_STOP, 0),
    ("DockerUnavailable", "sandbox_unavailable", FixPath.HARD_STOP, 0),
    ("ContainerLaunchError", "container_launch", FixPath.HARD_STOP, 0),
    ("DockerError", "container_launch", FixPath.HARD_STOP, 0),
    ("ArtifactRejected", "artifact_rejected", FixPath.HARD_STOP, 0),
    ("ExecutionCancelled", "execution_cancelled", FixPath.HARD_STOP, 0),
    ("ExecutionTimeout", "exec_timeout", FixPath.CODE, 1),
    ("TimeoutError", "exec_timeout", FixPath.CODE, 1),
    ("ExecutionOOM", "exec_oom", FixPath.CODE, 1),
    ("InvalidCode", "invalid_code", FixPath.CODE, 2),
    ("CADKernelError", "cad_kernel", FixPath.CODE, 2),
])
def test_execution_error_types(etype, key, path, retry_budget):
    fc = classify(etype, "boom", gate="exec")
    assert fc.key == key
    assert fc.fix_path is path
    assert fc.retry_budget == retry_budget


def test_vision_gates():
    assert classify(None, "x", gate="vision_mismatch").fix_path is FixPath.VISUAL
    assert classify(None, "x", gate="vision_mismatch").retry_budget == 2
    assert classify(None, "x", gate="vision_indeterminate").fix_path is FixPath.HARD_STOP


# --- totality + invariants ---

def test_classify_is_total():
    assert classify(None, None).key == "unknown"
    assert classify("", "totally novel xyz error").key == "unknown"
    assert classify(None, "").key == "unknown"


def test_all_keys_unique():
    keys = [fc.key for fc in FAILURE_CLASSES]
    assert len(keys) == len(set(keys))


def test_fix_hint_includes_scope():
    fc = classify(None, "BRep_API: not done")
    hint = fix_hint_for(fc)
    assert fc.fix_hint in hint
    assert fc.fix_scope in hint  # A3 minimal-change directive is appended


# --- generated prompt table is the single source of truth ---

def test_render_prompt_table_covers_code_classes():
    table = render_prompt_table()
    for fc in FAILURE_CLASSES:
        if fc.fix_path is FixPath.CODE and fc.key != "unknown":
            assert fc.label in table, f"{fc.label} missing from generated table"


def test_render_prompt_table_excludes_infra_and_vision():
    table = render_prompt_table()
    assert "DockerUnavailable" not in table
    assert "SandboxUnavailable" not in table
    assert "ArtifactRejected" not in table
    assert "vision" not in table
    assert "未知错误" not in table  # unknown is excluded
