"""Frozen previous-release bytes; never regenerate expectations in a test run."""
import json
from pathlib import Path
import pytest
from app.execution.canonical import canonical_json_bytes, canonical_sha256
from app.contracts.model_operations import MODEL_OPERATIONS
from sandbox.freecad_result_channel import read_result, ResultProtocolError

GOLDEN = json.loads((Path(__file__).parent/'fixtures/contracts/baseline-62084d5.json').read_text())

@pytest.mark.parametrize('vector', GOLDEN['vectors'], ids=lambda v: v['name'])
def test_previous_release_canonical_contract(vector):
    assert canonical_json_bytes(vector['value']).decode() == vector['canonical_utf8']
    assert canonical_sha256(vector['value']) == vector['sha256']


def test_old_native_result_uses_invocation_identity(tmp_path):
    value = next(v['value'] for v in GOLDEN['vectors'] if v['name'] == 'native-error')
    path = tmp_path/'result.json'; path.write_text(json.dumps(value))
    assert read_result(path, 'golden-invocation') == value['result']
    with pytest.raises(ResultProtocolError):
        read_result(path, 'another-invocation')


def test_durable_operation_names_do_not_change():
    assert MODEL_OPERATIONS == {
        'agent_v2.requirements','agent_v2.decompose','agent_v2.generate_source',
        'agent_v2.repair_source','agent_v2.generate_operations','agent_v2.repair_operations',
        'agent_v2.judge_visual','agent_v2.repair_visual',
    }
