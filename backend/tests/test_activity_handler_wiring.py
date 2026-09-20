"""Check every retained wire adapter against the actual extracted handler API."""
import ast
import inspect
from pathlib import Path
from app.workflows import activities


def test_all_activity_wrappers_bind_explicit_dependencies():
    tree = ast.parse(Path(activities.__file__).read_text())
    checked = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if not (len(node.body) == 1 and isinstance(node.body[0], ast.Return)):
            continue
        value = node.body[0].value
        if not isinstance(value, ast.Await) or not isinstance(value.value, ast.Call):
            continue
        call = value.value
        if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
            handler = getattr(getattr(activities, call.func.value.id), call.func.attr)
        elif isinstance(call.func, ast.Name):
            handler = getattr(activities, call.func.id)
        else:
            continue
        inspect.signature(handler).bind(*[object() for _ in call.args], **{kw.arg: object() for kw in call.keywords})
        checked.append(node.name)
    assert len(checked) >= 30, checked
