import json,subprocess,time
from pathlib import Path
from dotenv import dotenv_values
import httpx
R=Path('/private/tmp/cad-fixes-20260923');S=Path('/private/tmp/cad-deploy-20260922/source')
D=[str(R/'bin/docker')];C=D+['compose','-p','cad-acceptance-20260923','--env-file',str(R/'stack.env'),'-f',str(S/'deploy/acceptance/compose.yml')]
a=json.loads((R/'stack-private.json').read_text());rows=[]
new=json.loads((R/'new-images.json').read_text());old=json.loads((R/'old-images.json').read_text())
def switch(backend,monitor):
 p=R/'stack.env';v=dict(dotenv_values(p));v['BACKEND_IMAGE']=backend;v['MONITORING_IMAGE']=monitor;p.write_text(''.join(k+'='+str(x)+'\n' for k,x in v.items()));p.chmod(0o600)
 subprocess.run(C+['up','-d','--no-deps','backend','monitoring'],check=True,capture_output=True)
 for base in ('http://127.0.0.1:18061','http://127.0.0.1:18160'):
  while True:
   try:
    if httpx.get(base+'/health',timeout=5).status_code==200:break
   except httpx.HTTPError:pass
   time.sleep(1)
 with httpx.Client(base_url='http://127.0.0.1:18061',timeout=30) as c:
  for endpoint in ('accounts','tasks','calls'):
   p='/api/monitor/'+endpoint
   assert c.get(p).status_code==401
   assert c.get(p,headers={'Authorization':'Bearer '+a['owner']['token']}).status_code==403
   assert c.get(p,headers={'Authorization':'Bearer '+a['admin']['token']}).status_code==200
 rows.append({'backend_image':backend,'monitoring_image':monitor,'anonymous':401,'ordinary':403,'admin':200})
 (R/'monitor-image-matrix.json').write_text(json.dumps({'passed':True,'combinations':rows},indent=2))
try:
 switch(new['backend'],old['backend']);switch(old['backend'],new['backend'])
finally:switch(new['backend'],new['backend'])
print('MIXED MONITOR IMAGES PASSED')
