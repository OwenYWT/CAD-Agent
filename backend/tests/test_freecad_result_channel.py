"""Protocol regressions: native logs must never be interpreted as results."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sandbox import capability_entry as dispatcher
from sandbox.freecad_result_channel import publish_result, read_result


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatcher, 'OUTPUT_ROOT', tmp_path)
    return tmp_path


def install_worker(monkeypatch, runtime, result, *, defect=None, returncode=1, logs='(53 %)'):
    def run(*_, **kwargs):
        env = kwargs['env']
        path = Path(env.get('CAD_FREECAD_RESULT_PATH', str(runtime/'old-runner-result.json')))
        envelope = {'schema_version':'freecad-runner-envelope.v1',
                    'invocation_id':env.get('CAD_FREECAD_INVOCATION_ID','not-passed'), 'result':result}
        if defect == 'stale': envelope['invocation_id']='previous-invocation'
        if defect == 'schema': envelope['schema_version']='unknown'
        if defect == 'partial': path.write_text('{')
        elif defect == 'symlink':
            target=runtime/'other.json';target.write_text(json.dumps(envelope));path.symlink_to(target)
        elif defect != 'missing': path.write_text(json.dumps(envelope))
        return SimpleNamespace(returncode=returncode, stdout=logs+json.dumps(result),stderr='native diagnostic')
    monkeypatch.setattr(dispatcher.subprocess,'run',run)


def failed():
    return {'schema_version':'freecad-operation-result.v1','status':'failed','files':{},
            'error':{'code':'sketch_redundant_constraints','message':'redundant',
                     'op_id':'relate-profiles','action':'sketch.add_constraint',
                     'details':{'object':'Profile','solver_status':-2}}}


@pytest.mark.parametrize('logs',['(53 %)','x'*(600*1024),'progress\n{"status":"succeeded"}\n'],ids=['interleaved','long-log','json-log'])
def test_solver_error_survives_mixed_or_truncated_logs(runtime,monkeypatch,logs):
    install_worker(monkeypatch,runtime,failed(),logs=logs)
    with pytest.raises(dispatcher.StructuredCapabilityError) as caught:
        dispatcher._freecad({})
    assert caught.value.error['code']=='sketch_redundant_constraints'
    assert caught.value.error['operation_id']=='relate-profiles'
    assert caught.value.error['details']['object']=='Profile'


@pytest.mark.parametrize('defect',['missing','partial','stale','schema','symlink'])
def test_invalid_result_file_cannot_be_rescued_by_success_in_stdout(runtime,monkeypatch,defect):
    result={'schema_version':'freecad-operation-result.v1','status':'succeeded',
            'files':{'fcstd':str(runtime/'model.FCStd')}}
    install_worker(monkeypatch,runtime,result,defect=defect,returncode=0,logs='')
    with pytest.raises(dispatcher.StructuredCapabilityError) as caught:
        dispatcher._freecad({})
    assert caught.value.error['code']=='sandbox_protocol_error'


def test_nonzero_exit_cannot_report_success(runtime,monkeypatch):
    result={'schema_version':'freecad-operation-result.v1','status':'succeeded',
            'files':{'fcstd':str(runtime/'model.FCStd')}}
    install_worker(monkeypatch,runtime,result,returncode=1,logs='')
    with pytest.raises(dispatcher.StructuredCapabilityError) as caught:
        dispatcher._freecad({})
    assert caught.value.error['code']=='sandbox_protocol_error'


def test_native_publisher_and_reader_round_trip_without_stdout(tmp_path, monkeypatch):
    path=tmp_path/'result.json'
    monkeypatch.setenv('CAD_FREECAD_RESULT_PATH',str(path))
    monkeypatch.setenv('CAD_FREECAD_INVOCATION_ID','invocation')
    publish_result(failed())
    assert read_result(path,'invocation') == failed()
    assert not path.with_suffix('.tmp').exists()


def test_success_uses_same_result_channel(runtime,monkeypatch):
    result={'schema_version':'freecad-operation-result.v1','status':'succeeded',
            'files':{'fcstd':str(runtime/'model.FCStd')}}
    install_worker(monkeypatch,runtime,result,returncode=0,logs='not JSON progress')
    files,metadata=dispatcher._freecad({})
    assert files=={'fcstd':runtime/'model.FCStd'}
    assert metadata['result']==result
    assert not list(runtime.glob('.freecad-result-*.json'))


@pytest.mark.parametrize('operation,schema', [
    ('execute','freecad-operation-result.v1'),
    ('scene','freecad-operation-result.v1'),
    ('bom','freecad-operation-result.v1'),
    ('engineering','freecad-engineering-result.v1'),
])
def test_result_channel_preserves_each_existing_capability_contract(runtime,monkeypatch,operation,schema):
    result={'schema_version':schema,'status':'succeeded','files':{'report':str(runtime/'report.json')}}
    install_worker(monkeypatch,runtime,result,returncode=0)
    files,metadata=dispatcher._freecad({'operation':operation})
    assert metadata['result']['schema_version']==schema
    assert files['report']==runtime/'report.json'


def test_result_from_another_capability_is_rejected(runtime,monkeypatch):
    result={'schema_version':'freecad-operation-result.v1','status':'succeeded','files':{'fcstd':str(runtime/'model.FCStd')}}
    install_worker(monkeypatch,runtime,result,returncode=0)
    with pytest.raises(dispatcher.StructuredCapabilityError) as caught:
        dispatcher._freecad({'operation':'engineering'})
    assert caught.value.error['code']=='sandbox_protocol_error'


def test_engineering_failure_uses_shared_native_error_contract(runtime,monkeypatch):
    install_worker(monkeypatch,runtime,failed())
    with pytest.raises(dispatcher.StructuredCapabilityError) as caught:
        dispatcher._freecad({'operation':'engineering'})
    assert caught.value.error['code']=='sketch_redundant_constraints'
