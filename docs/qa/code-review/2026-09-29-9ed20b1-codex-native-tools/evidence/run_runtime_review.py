"""Run current existing kernel contracts without changing source or old evidence."""
from pathlib import Path
import base64
import hashlib
import json
import subprocess

root = Path(__file__).resolve().parents[5]
evidence = Path(__file__).parent
files = ['feature_verification_contract.py', 'curved_surface_contract.py',
         'freecad_checkpoint_replay_contract.py', 'freecad_tool_replay_contract.py',
         'fixtures/live_freecad_checkpoint_plate_20260928.json',
         'fixtures/native_failures/disconnected_lip.step']
payload = {f:base64.b64encode((root/'backend/tests/e2e'/f).read_bytes()).decode() for f in files}
hashes = {f:hashlib.sha256((root/'backend/sandbox'/f).read_bytes()).hexdigest()
          for f in ['feature_verification.py','geometry_validation.py','freecad_entry.py','freecad_api.py']}
bootstrap = '''import base64, hashlib, json, os, pathlib, subprocess
for f, expected in HASHES.items():
    actual=hashlib.sha256(pathlib.Path('/opt/cad-agent',f).read_bytes()).hexdigest()
    assert actual==expected,(f,actual,expected)
print('RUNTIME_SOURCE_HASHES='+json.dumps(HASHES),flush=True)
for f, content in FILES.items():
    p=pathlib.Path('/tests',f);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(base64.b64decode(content))
for name, marker, native in [
    ('feature_verification_contract','FEATURE_MEASUREMENT_CONTRACT_PASSED',False),
    ('curved_surface_contract','CURVED_SURFACE_CONTRACT=',False),
    ('freecad_checkpoint_replay_contract','CAD_CHECKPOINT_REPLAY_CONTRACT=',True)]:
    path='/tests/'+name+'.py'
    command=(['/opt/freecad/bin/FreeCADCmd','-c',"exec(compile(open("+repr(path)+").read(),"+repr(path)+",'exec'))"]
             if native else ['python',path])
    result=subprocess.run(command,capture_output=True,text=True)
    print('CASE='+name,flush=True);print(result.stdout,flush=True);print(result.stderr,flush=True)
    assert result.returncode==0 and marker in result.stdout and 'Traceback (most recent call last)' not in result.stdout+result.stderr,name
print('RUNTIME_REVIEW_CONTRACTS_PASSED',flush=True)
'''
source = 'HASHES='+repr(hashes)+'\nFILES='+repr(payload)+'\n'+bootstrap
command=['/private/tmp/cad-fixes-20260923/bin/docker','run','--rm','-i','--network','none',
    '--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
    '--tmpfs','/tmp:rw,size=2g','--tmpfs','/tests:rw,size=32m',
    '--tmpfs','/sandbox/input:rw,mode=777','--tmpfs','/sandbox/output:rw,mode=777',
    '-e','PYTHONPATH=/opt/cad-agent','--entrypoint','python','cad-native-sandbox:whole-certified-test','-']
with (evidence/'runtime-contracts.log').open('w') as log:
    result=subprocess.run(command,input=source,text=True,stdout=log,stderr=subprocess.STDOUT)
print('Runtime review return code:',result.returncode)
raise SystemExit(result.returncode)
