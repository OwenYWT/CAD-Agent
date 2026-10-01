"""Explicit infrastructure allowance for Rosetta only; no application code changes."""
import json,subprocess,time
from pathlib import Path
R=Path('/private/tmp/cad-fixes-20260923');D=[str(R/'bin/docker')]
allowed=set()
for name in ['old-images.json','new-images.json']:allowed.add(json.loads((R/name).read_text())['sandbox'])
changed=[];seen=set()
while not (R/'stop-memory-observer').exists():
 ids=subprocess.check_output(D+['ps','-q'],text=True).split()
 if ids:
  inspected=subprocess.run(D+['inspect',*ids],text=True,capture_output=True)
  if inspected.returncode:
   if 'no such object' in inspected.stderr.lower():continue
   raise RuntimeError(inspected.stderr)
  rows=json.loads(inspected.stdout)
  for row in rows:
   if row['Id'] in seen or row['Image'] not in allowed:continue
   assert row['HostConfig']['NetworkMode']=='none'
   assert row['Config']['Labels'].get('com.docker.compose.project') is None
   # This daemon belongs only to this disposable VM and contains no production services.
   old=row['HostConfig']['Memory']
   updated=subprocess.run(D+['update','--memory','3g','--memory-swap','3g',row['Id']],text=True,capture_output=True)
   if updated.returncode:
    if 'no such container' in updated.stderr.lower():continue
    raise RuntimeError(updated.stderr)
   seen.add(row['Id']);changed.append({'container_id':row['Id'],'image':row['Image'],'requested_memory_bytes':old,'isolated_emulation_allowance_bytes':3*1024**3})
   (R/'emulation-resources.json').write_text(json.dumps(changed,indent=2));print('EMULATION_MEMORY_ALLOWANCE',row['Id'],old,flush=True)
 time.sleep(.1)
