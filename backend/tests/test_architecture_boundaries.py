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


def structural_fixture(tmp_path, sources):
    for filename,content in sources.items():
        path=tmp_path/filename
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(content)
    config=json.loads((ROOT/'docs/architecture/modules.json').read_text())
    return load('scripts/architecture/check_boundaries.py').structural_violations(tmp_path,config)


def test_independent_review_counterexamples_fail(tmp_path):
    found=structural_fixture(tmp_path,{
        'backend/app/services/monitor_queries.py':
            'from app.services.cloud_documents import _load_state\nfrom app.services.document_operations import submit\n',
        'backend/app/services/cloud_documents.py':'def _load_state(): pass\n',
        'backend/app/services/document_operations.py':'from app.services.monitor_queries import accounts\n',
        'backend/app/workflows/handlers/planning.py':'from app.workflows.handlers.cad_execution import _internal_mutator\n',
        'backend/app/workflows/handlers/cad_execution.py':'def _internal_mutator(): pass\n',
    })
    assert any(k.startswith('private-symbol|') and '_load_state' in k for k in found)
    assert any(k.startswith('private-symbol|') and '_internal_mutator' in k for k in found)
    assert any(k.startswith('direction|') and 'monitor_queries' in k for k in found)
    assert any(k.startswith('file-cycle|') for k in found)
    assert 'module-cycle|M11|M02' in found


def test_undeclared_file_and_private_module_fail(tmp_path):
    found=structural_fixture(tmp_path,{
        'backend/app/services/new_unowned.py':'',
        'backend/app/services/monitor_queries.py':'from app.freecad.semantic_state import bounded_agent_context\n',
        'backend/app/freecad/semantic_state.py':'',
    })
    assert 'ownership|backend/app/services/new_unowned.py' in found
    assert any(k.startswith('private-module|') for k in found)


def test_module_alias_does_not_hide_private_access(tmp_path):
    found=structural_fixture(tmp_path,{
        'backend/app/services/monitor_queries.py':'from app.services import cloud_documents as doc\ndef query():\n    return doc._load_state()\n',
        'backend/app/services/cloud_documents.py':'def _load_state(): pass\n',
    })
    assert any(k.startswith('private-symbol|') and '_load_state' in k for k in found)


def test_frontend_dynamic_import_cycle_and_private_entry_fail(tmp_path):
    found=structural_fixture(tmp_path,{
        'frontend/src/services/clients/documents.ts':'export const x=import("./engineering.ts");',
        'frontend/src/services/clients/engineering.ts':'export {x} from "./documents";',
    })
    assert any(k.startswith('file-cycle|frontend/') for k in found)


@pytest.mark.parametrize('target', ['../services/engineeringService',
    '../services/engineeringService.ts', '@/services/engineeringService',
    '@/services/engineeringService.ts'])
def test_alias_or_extension_cannot_bypass_legacy_facade_rule(tmp_path, target):
    path = tmp_path / 'frontend/src/components/Consumer.ts'
    path.parent.mkdir(parents=True)
    path.write_text(f'export const load = import("{target}");')
    config = json.loads((ROOT / 'docs/architecture/modules.json').read_text())
    found = load('scripts/architecture/check_boundaries.py').violations(tmp_path, config['import_rules'])
    assert any(k.startswith('frontend-no-legacy-facade|') for k in found)


def test_runtime_gate_rejects_missing_suite_and_missing_negative_case(tmp_path):
    gate=load('scripts/ci/require_runtime_evidence.py')
    required={'browser/report.json':{'kind':'json','fields':{
        'passed':True,'unknown_acceptance_same_request':True}}}
    with pytest.raises(ValueError,match='missing mandatory'):
        gate.validate(tmp_path,required)
    path=tmp_path/'browser/report.json';path.parent.mkdir()
    path.write_text('{"passed":true}')
    with pytest.raises(ValueError,match='unknown_acceptance_same_request'):
        gate.validate(tmp_path,required)
    path.write_text('{"passed":true,"unknown_acceptance_same_request":true}')
    assert gate.validate(tmp_path,required) == 1
