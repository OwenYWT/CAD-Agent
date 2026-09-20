"""Negative fixtures prove architecture and mandatory-suite gates fail closed."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(relative):
    spec = importlib.util.spec_from_file_location('gate', ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repository_boundaries():
    assert load('scripts/architecture/check_boundaries.py').audit() == []


def test_nested_import_and_frontend_reverse_dependency_are_detected(tmp_path):
    backend = tmp_path / 'backend/app/services/bad.py'
    backend.parent.mkdir(parents=True)
    backend.write_text('def call():\n    from app.api.login import login_with_password\n')
    frontend = tmp_path / 'frontend/src/services/clients/bad.ts'
    frontend.parent.mkdir(parents=True)
    frontend.write_text('export { read } from "../engineeringService";')
    config = json.loads((ROOT / 'docs/architecture/modules.json').read_text())
    found = load('scripts/architecture/check_boundaries.py').violations(tmp_path, config['import_rules'])
    assert any('business-no-http|' in edge for edge in found)
    assert any('clients-no-ui|' in edge for edge in found)


@pytest.mark.parametrize('body', ['', '<testcase name="x"><skipped/></testcase>',
                                    '<testcase name="x"><failure/></testcase>', '<testcase name="x"><error/></testcase>'])
def test_mandatory_suite_rejects_missing_execution(tmp_path, body):
    report = tmp_path / 'report.xml'
    report.write_text(f'<testsuite>{body}</testsuite>')
    with pytest.raises(ValueError):
        load('scripts/ci/require_test_report.py').validate(report)


def test_mandatory_suite_accepts_actual_passing_cases(tmp_path):
    report = tmp_path / 'report.xml'
    report.write_text('<testsuite><testcase name="x"/></testsuite>')
    assert load('scripts/ci/require_test_report.py').validate(report) == 1


def test_relative_and_parent_package_imports_cannot_bypass_boundaries(tmp_path):
    path = tmp_path / 'backend/app/services/bad.py'
    path.parent.mkdir(parents=True)
    path.write_text('from .. import api\nfrom app import api\n')
    config = json.loads((ROOT / 'docs/architecture/modules.json').read_text())
    found = load('scripts/architecture/check_boundaries.py').violations(tmp_path, config['import_rules'])
    assert any(edge.endswith('|app.api') for edge in found)
