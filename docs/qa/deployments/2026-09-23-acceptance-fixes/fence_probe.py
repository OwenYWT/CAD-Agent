import json,subprocess,time
from pathlib import Path
R=Path('/private/tmp/cad-fixes-20260923');D=[str(R/'bin/docker')];seen=set();results=[]
while not (R/'stop-fence-probe').exists():
 d=json.loads((R/'full-stack-report.json').read_text())
 for step in d.get('steps',[]):
  old=step['interrupted_job']
  if old['id'] in seen:continue
  code='''import asyncio,json,sys
from uuid import UUID
from sqlalchemy import text
from app.db import get_database_engine,close_database
from app.services.model_jobs import renew
async def run():
 old=json.loads(sys.stdin.read())
 async with get_database_engine().connect() as c:
  row=(await c.execute(text('SELECT generation,status FROM model_jobs WHERE id=:id'),{'id':UUID(old['id'])})).mappings().one()
  current=dict(row)
 if current['status']=='running' and current['generation']>old['generation']:
  job={k:UUID(old[k]) for k in ['id','tenant_id','principal_id']};job['generation']=old['generation']
  renewed=await renew(job)
  assert renewed is False, 'stale generation renewed a live replacement job'
  print(json.dumps({'job_id':old['id'],'stale_generation':old['generation'],'live_generation':current['generation'],'stale_renew_rejected':True}))
 await close_database()
asyncio.run(run())
'''
  p=subprocess.run(D+['exec','-i','cad-acceptance-20260923-backend-1','python','-c',code],input=json.dumps(old),text=True,capture_output=True)
  if p.returncode:
   if p.returncode in (137,143):continue
   if any(v in p.stderr for v in ['No such container','is not running','ConnectionRefusedError','ConnectionDoesNotExistError']):continue
   raise RuntimeError(f'exit={p.returncode}: {p.stderr}')
  if p.stdout.strip():results.append(json.loads(p.stdout.strip()));seen.add(old['id']);(R/'live-generation-fence.json').write_text(json.dumps(results,indent=2));print(p.stdout,flush=True)
 time.sleep(.3)
