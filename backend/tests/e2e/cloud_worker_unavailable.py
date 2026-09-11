"""Real authenticated ASGI -> PostgreSQL -> Temporal no-poller rejection.

The queue override applies only to this short-lived test process inside the API
container. The running API/worker configuration is not changed.
"""
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4


def main():
    private = json.loads(Path(os.environ["CAD_NATIVE_E2E_PRIVATE"]).read_text())
    data = {"token": private["owner"]["token"], "document_id": private["document_id"]}
    script = 'import json\npayload=json.loads(' + repr(json.dumps(data)) + ')\n' + '''
import asyncio,httpx
from uuid import uuid4
from sqlalchemy import text
from app.main import app
from app.db import get_database_engine,close_database
async def main():
 key=str(uuid4());path='/api/documents/'+payload['document_id']
 async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://acceptance',headers={'Authorization':'Bearer '+payload['token']}) as client:
  response=await client.get(path);assert response.status_code==200
  before=response.json()
  hole=next(f for f in before['features'] if f['type']=='PartDesign::Hole')
  parameter=next(p for p in hole['parameters'] if p['property_name']=='Diameter')
  response=await client.post(path+'/operations',json={'action':'parameters.update','expected_base_revision_id':before['head_revision_id'],'expected_state_version':before['state_version'],'idempotency_key':key,'modification':{'expected_state_sha256':before['parameter_state_sha256'],'parameter_updates':[{'parameter_id':parameter['id'],'value':parameter['value']+.1}]}})
  assert response.status_code==503,(response.status_code,response.text)
  assert response.json()['detail']['code']=='workflow_worker_unavailable' and response.json()['detail']['retryable']
  after=(await client.get(path)).json();assert after['head_revision_id']==before['head_revision_id']
 async with get_database_engine().connect() as conn:
  count=await conn.scalar(text('SELECT count(*) FROM workflow_runs WHERE idempotency_key=:key'),{'key':key})
 assert count==0
 print('CAD_WORKER_UNAVAILABLE='+json.dumps({'http_status':response.status_code,'body':response.json(),'new_workflow_count':count,'head_unchanged':True,'real_temporal_empty_queue':True}),flush=True)
 await close_database()
asyncio.run(main())
'''
    result = subprocess.run([os.getenv("CAD_PODMAN", "/opt/homebrew/bin/podman"), "exec", "-i", "-e", "PYTHONPATH=/app/backend",
        "-e", "TEMPORAL_AGENT_V2_TASK_QUEUE=cad-remediation-empty-" + uuid4().hex,
        os.environ["CAD_NATIVE_E2E_API"], "python", "-"], input=script, text=True, capture_output=True, timeout=90)
    assert result.returncode == 0, (result.stdout + result.stderr)[-2500:]
    evidence = json.loads(next(line.split("=",1)[1] for line in result.stdout.splitlines() if line.startswith("CAD_WORKER_UNAVAILABLE=")))
    Path(os.environ["CAD_WORKER_UNAVAILABLE_REPORT"]).write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    print(json.dumps(evidence, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
