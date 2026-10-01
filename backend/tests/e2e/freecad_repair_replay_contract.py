"""Replay a retained failed provider plan, without a provider call or geometry edits."""
import copy
import json
import sys
import tempfile
from pathlib import Path
sys.path.insert(0,'/opt/cad-agent')
import freecad_entry as runner
App,Part=runner.App,runner.Part
fixture=json.loads((Path('/tests/fixtures/native_failures/no_effect_cut.json')).read_text())
with tempfile.TemporaryDirectory() as root:
    root=Path(root);runner.INPUT_ROOT=root/'input';runner.INPUT_ROOT.mkdir()
    runner.OUTPUT_ROOT=root/'output';runner.OUTPUT_ROOT.mkdir()
    def run(plan):
        return runner.run_task({'schema_version':'mcad-capability-task.v1','capability':'freecad','operation':'execute',
            'inputs':{},'params':{'plan':plan}})
    try:run(fixture['plan'])
    except runner.FreeCADRunnerError as exc:
        assert exc.code=='subtractive_feature_no_effect'
        failure={'operation_id':exc.op_id,'error_code':exc.code,'details':exc.details}
        assert exc.details['before_volume']==exc.details['after_volume']
        assert exc.details['profile_normal_z']==1
    else:raise AssertionError('Original no-op cut should fail')
    repaired=copy.deepcopy(fixture['plan'])
    target=next(op for op in repaired['operations'] if op['op_id']==failure['operation_id'])
    assert target['args']['reversed'] is True
    target['args']['reversed']=False
    result=run(repaired)
    shape=Part.read(result['files']['step'])
    assert shape.isValid() and len(shape.Solids)==1
    assert shape.Volume<failure['details']['before_volume']
    doc=App.openDocument(result['files']['fcstd'])
    assert doc.hollow.Length.Value==38 and doc.base_block.Length.Value==40
    App.closeDocument(doc.Name)
print('CAD_REPAIR_REPLAY_CONTRACT='+json.dumps({'retained_failure':'subtractive_feature_no_effect','only_direction_changed':True,'final_step_valid':True}))
