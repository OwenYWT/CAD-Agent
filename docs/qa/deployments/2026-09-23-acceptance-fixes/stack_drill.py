"""Destructive fault injection ONLY in the disposable cad-acceptance stack."""
import base64,hashlib,json,os,secrets,subprocess,time
from pathlib import Path
from uuid import uuid4
import httpx
from dotenv import dotenv_values
from websockets.sync.client import connect
R=Path('/private/tmp/cad-fixes-20260923');S=Path('/private/tmp/cad-deploy-20260922/source')
D=[str(R/'bin/docker')]; PROJECT='cad-acceptance-20260923'
C=D+['compose','-p',PROJECT,'--env-file',str(R/'stack.env'),'-f',str(S/'deploy/acceptance/compose.yml')]
BASE='http://127.0.0.1:18160';MON='http://127.0.0.1:18061'
evidence={'project':PROJECT,'execution_host':'independent Lima VM; AMD64 images on ARM64 host via Rosetta','steps':[]}
report=R/'full-stack-report.json'
def save():report.write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
def command(args,stdin=None):
 p=subprocess.run(args,input=stdin,text=True,capture_output=True)
 if p.returncode:raise RuntimeError(str(args[:8])+': '+p.stderr[-1500:])
 return p.stdout.strip()
def compose(*a,stdin=None):return command(C+list(a),stdin)
def sql(q):return compose('exec','-T','postgres','psql','-U','cad_acceptance','-d','cad_acceptance','-v','ON_ERROR_STOP=1','-At',stdin=q)
def api(c,m,p,expected=200,**kw):
 r=c.request(m,BASE+p,timeout=60,**kw);assert r.status_code==expected,(p,r.status_code,r.text[:1200]);return r.json() if r.content else None
def ready():
 while True:
  try:
   if httpx.get(BASE+'/ready',timeout=5).status_code==200:return
  except httpx.HTTPError:pass
  time.sleep(2)
def snapshot_images():
 items=json.loads(compose('ps','--all','--format','json') if False else command(D+['inspect']+compose('ps','--all','-q').split()))
 return {i['Config']['Labels']['com.docker.compose.service']:{'image':i['Image'],'id':i['Id']} for i in items}
def private_env(path,changes):
 v=dict(dotenv_values(path));v.update(changes);path.write_text(''.join(k+'='+str(x)+'\n' for k,x in v.items()));path.chmod(0o600)
def switch(which):
 imgs=json.loads((R/(which+'-images.json')).read_text())
 before=snapshot_images()
 compose('down','--remove-orphans') # No -v: only this project's durable volumes survive.
 private_env(R/'stack.env',{'BACKEND_IMAGE':imgs['backend'],'FRONTEND_IMAGE':imgs['frontend'],'MONITORING_IMAGE':imgs['backend']})
 sb=('cad-native-sandbox' if which=='old' else 'cad-native-sandbox')+'@'+imgs['sandbox']
 for path in [R/'stack-backend.env',R/'stack-monitor.env']:private_env(path,{'SANDBOX_IMAGE':sb})
 compose('up','-d');ready();after=snapshot_images()
 for service in ('postgres','minio','temporal','backend','workflow-worker','frontend','monitoring'):
  assert before[service]['id']!=after[service]['id'],service
 return {'from':before,'to':after,'schema':sql('SELECT version_num FROM alembic_version;')}
def jobs(wid):
 return json.loads(sql("SELECT COALESCE(json_agg(j),'[]') FROM (SELECT id,tenant_id,principal_id,workflow_run_id,operation,payload_hash,generation,status FROM model_jobs WHERE workflow_run_id='"+str(uuid4() if wid is None else __import__('uuid').UUID(wid))+"' ORDER BY created_at) j;"))
def submit(owner,token):
 sid,panel=str(uuid4()),str(uuid4());protocol='cad-agent-auth.'+base64.urlsafe_b64encode(token.encode()).decode().rstrip('=')
 with connect(BASE.replace('http','ws',1)+f'/ws/{sid}',subprotocols=[protocol]) as ws:
  ws.send(json.dumps({'type':'user_message','panel_id':panel,'operation_intent':'generate','text':'Create a rectangular plate 60 mm by 40 mm, thickness 8 mm. Add one centered 6 mm diameter through hole. Export STEP and STL.','idempotency_key':str(uuid4())}))
  while True:
   e=json.loads(ws.recv(timeout=60))
   if e['type']=='task_submitted':return e['data']
   if e['type']=='generation_result' and e.get('data',{}).get('error'):raise AssertionError(e['data']['error'])
def wait(owner,wid):
 previous=None
 while True:
  task=api(owner,'GET',f'/api/tasks/{wid}/snapshot');status=task['status']
  if status!=previous:print(wid,status,flush=True);previous=status
  if status=='waiting_confirmation':api(owner,'POST',f'/api/tasks/{wid}/confirmation',json={'accepted':True,'note':'Isolated release drill: inspect real plan'})
  if status in {'succeeded','failed','cancelled','timed_out'}:
   assert status=='succeeded',{k:task.get(k) for k in ('id','status','error_code','error_message','error')};return task
  time.sleep(1)
def commit(owner,task):
 change=task['change_set']['id'];api(owner,'POST',f'/api/change-sets/{change}/accept',json={'note':'Real artifact and job recovery verified'})
 api(owner,'POST',f'/api/change-sets/{change}/commit');return change
def artifacts(owner,doc):
 snap=api(owner,'GET',f'/api/documents/{doc}'); hashes={}
 for kind in ('fcstd','mesh','state'):
  a=snap[kind];r=owner.get(BASE+a['url']);assert r.status_code==200
  h=hashlib.sha256(r.content).hexdigest();assert h==a['sha256'];hashes[kind]=h
 assert snap['features'];return {'document_id':doc,'revision_id':snap['head_revision_id'],'artifacts':hashes,'state_version':snap['state_version']}
def monitor(token,admin):
 while True:
  try:
   if httpx.get(MON+'/health',timeout=5).status_code==200:break
  except httpx.HTTPError:pass
  time.sleep(1)
 with httpx.Client(base_url=MON,timeout=60) as c:
  for endpoint in ('accounts','tasks','calls'):
   p='/api/monitor/'+endpoint
   assert c.get(p).status_code==401
   assert c.get(p,headers={'Authorization':'Bearer '+token}).status_code==403
   r=c.get(p,headers={'Authorization':'Bearer '+admin});assert r.status_code==200,(p,r.status_code,r.text[:500])
 return {'anonymous':401,'ordinary_user':403,'admin':200}
def drill(owner,token,which):
 sub=submit(owner,token);wid=sub['workflow_run_id'];print('Submitted',which,wid,flush=True)
 while True:
  running=[j for j in jobs(wid) if j['status']=='running']
  if running:break
  task=api(owner,'GET',f'/api/tasks/{wid}/snapshot');assert task['status'] not in {'failed','succeeded','cancelled','timed_out'},task['status'];time.sleep(.1)
 interrupted=running[0]
 compose('kill','-s','SIGKILL','workflow-worker')
 frozen=next(j for j in jobs(wid) if j['id']==interrupted['id']);assert frozen['status']=='running',frozen
 evidence['steps'].append({'target':which,'workflow_id':wid,'interrupted_job':frozen,'status':'interrupted'});save()
 images=switch(which)
 task=wait(owner,wid);after=next(j for j in jobs(wid) if j['id']==frozen['id'])
 assert after['generation']>frozen['generation'] and after['status']=='succeeded',(frozen,after)
 assert after['payload_hash']==frozen['payload_hash']
 change=commit(owner,task);result=artifacts(owner,sub['branch_id'])
 evidence['steps'][-1].update(status='passed',recovered_job=after,change_set_id=change,result=result,stack_recreated=images);save();return result

def main():
 ready();assert set(snapshot_images())>={'postgres','minio','temporal','backend','workflow-worker','frontend','monitoring'}
 creds=json.loads((R/'stack-private.json').read_text());pw=creds['monitor_password']
 assert pw.replace('-','').replace('_','').isalnum()
 if sql("SELECT count(*) FROM pg_roles WHERE rolname='cad_acceptance_monitor';")=='0':
  sql("CREATE ROLE cad_acceptance_monitor LOGIN PASSWORD '"+pw+"'; GRANT cad_agent_monitor TO cad_acceptance_monitor;")
 config=dotenv_values(R/'stack-backend.env')
 admin=httpx.Client();a=api(admin,'POST','/api/auth/login/password',json={'phone':'admin','password':config['ADMIN_PASSWORD']});admin.headers['Authorization']='Bearer '+a['token']
 invite=api(admin,'POST','/api/auth/invites',json={'max_uses':1});phone='19'+str(secrets.randbelow(10**9)).zfill(9);password=secrets.token_urlsafe(24)
 person=api(admin,'POST','/api/auth/register/invite',json={'phone':phone,'password':password,'invite_code':invite['code']})
 creds.update(owner={**person,'phone':phone,'password':password},admin={'phone':'admin','password':config['ADMIN_PASSWORD'],'token':a['token']});(R/'stack-private.json').write_text(json.dumps(creds));(R/'stack-private.json').chmod(0o600)
 owner=httpx.Client(headers={'Authorization':'Bearer '+person['token']})
 evidence['old_monitor']=monitor(person['token'],a['token']);save()
 first=drill(owner,person['token'],'new');evidence['new_monitor']=monitor(person['token'],a['token']);save()
 second=drill(owner,person['token'],'old');assert artifacts(owner,first['document_id'])==first
 evidence['rollback_monitor']=monitor(person['token'],a['token']);save()
 evidence['final_upgrade']=switch('new')
 assert artifacts(owner,first['document_id'])==first and artifacts(owner,second['document_id'])==second
 assert api(owner,'POST','/api/auth/login/password',json={'phone':phone,'password':password})['token']
 evidence['final_monitor']=monitor(person['token'],a['token']);evidence['passed']=True;save();print('FULL STACK UPGRADE AND ROLLBACK PASSED',flush=True)
try:main()
except BaseException as exc:
 evidence['passed']=False;evidence['error']=str(exc);save();raise
