import argparse
import hashlib
import json
from types import SimpleNamespace

import pytest

from app.evaluation import cad_test_runner, integration
from app.evaluation.session import EvaluationSession
from app.evaluation.store import FileEvaluationStore
from benchmark import cad_test_batch


@pytest.mark.parametrize(('extra', 'native_status', 'expected_exit'), [
    ((), None, 0),
    (('fcstd',), None, 1),
    (('fcstd', 'stl'), 'failed', 1),
    (('fcstd', 'stl'), 'error', 1),
    (('fcstd', 'stl'), 'passed', 0),
])
def test_batch_score_requires_all_requested_delivery_checks(
    tmp_path, monkeypatch, extra, native_status, expected_exit,
):
    artifacts = {kind: tmp_path / kind for kind in ('step', *extra)}
    for path in artifacts.values():
        path.write_bytes(b'model')
    score = lambda **kwargs: {'status': 'pass'}
    monkeypatch.setattr(cad_test_batch, 'configure_process_mode', lambda mode: None)
    monkeypatch.setattr(cad_test_runner, 'resolve_mode', lambda: cad_test_runner.EvaluationMode.OBSERVE)
    monkeypatch.setattr(cad_test_runner, 'run_cad_test', score)
    monkeypatch.setattr(cad_test_batch, 'run_cad_test', score)
    monkeypatch.setattr(cad_test_runner, 'run_native_delivery_check', lambda **kwargs: {
        'status': native_status, 'passed': native_status == 'passed',
    })
    args = argparse.Namespace(case_id='case', out=tmp_path / 'out', scorer_image='',
                              timeout_seconds=1, step=artifacts['step'],
                              fcstd=artifacts.get('fcstd'), stl=artifacts.get('stl'))
    dataset = SimpleNamespace(root=tmp_path, case=lambda case_id: {'id': case_id})
    assert cad_test_batch._score(args, dataset) == expected_exit
    result = json.loads((args.out / 'result.json').read_text(encoding='utf-8'))
    assert (result['status'] == 'pass') == (expected_exit == 0)


def test_manual_attachment_without_seal_proof_does_not_score(tmp_path, monkeypatch):
    monkeypatch.setenv('CAD_EVALUATION_MODE', 'observe')
    store = FileEvaluationStore(tmp_path)
    run_id = 'live-workflow'
    store.create(EvaluationSession.create(
        mode='observe', evaluation_run_id=run_id, workflow_id='workflow',
        source_kind='benchmark', source_id='case',
    ))
    store.update_identity(run_id, candidate_revision_id='revision')
    artifact = tmp_path / 'local.step'
    artifact.write_bytes(b'not sealed')
    monkeypatch.setattr(integration, 'run_cad_test', lambda **kwargs: pytest.fail('unsealed input reached scorer'))

    integration.attach_cad_test_result(
        {'workflow_run_id': 'workflow', 'candidate_revision_id': 'revision'},
        artifact, case_id='case', root=tmp_path,
        config=cad_test_runner.CadTestRunnerConfig(root=tmp_path),
    )

    result = json.loads(store.path_for(run_id).read_text(encoding='utf-8'))['cad_test']
    assert result['status'] == 'blocked'
    assert result['reason'] == 'cad_test_sealed_artifact_identity_unavailable'


@pytest.mark.parametrize('swapped', ['step', 'fcstd', 'stl'])
def test_same_case_bundle_rejects_each_swapped_artifact_before_scoring(tmp_path, monkeypatch, swapped):
    monkeypatch.setenv('CAD_EVALUATION_MODE', 'observe')
    store = FileEvaluationStore(tmp_path)
    run_id = 'live-workflow'
    store.create(EvaluationSession.create(
        mode='observe', evaluation_run_id=run_id, workflow_id='workflow',
        source_kind='benchmark', source_id='case',
    ))
    store.update_identity(run_id, candidate_revision_id='revision')
    artifacts = {kind: tmp_path / f'candidate.{kind}' for kind in ('step', 'fcstd', 'stl')}
    for kind, path in artifacts.items():
        path.write_bytes(b'swap' if swapped == kind else b'seal')
    store.record_cad_test_seal(run_id, candidate_revision_id='revision', artifacts={
        kind: {'sha256': hashlib.sha256(b'seal').hexdigest(), 'size_bytes': 4}
        for kind in artifacts
    })
    monkeypatch.setattr(integration, 'run_cad_test_bundle', lambda **kwargs: pytest.fail('swapped input reached scorer'))

    integration.attach_cad_test_artifacts(
        {'workflow_run_id': 'workflow', 'candidate_revision_id': 'revision'},
        artifacts, case_id='case', root=tmp_path,
        config=cad_test_runner.CadTestRunnerConfig(root=tmp_path),
    )

    result = json.loads(store.path_for(run_id).read_text(encoding='utf-8'))['cad_test']
    assert result['status'] == 'blocked'
    assert result['reason'] == 'cad_test_artifact_identity_mismatch'
