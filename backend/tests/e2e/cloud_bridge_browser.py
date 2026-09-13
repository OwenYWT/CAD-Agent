"""Browser download/pair/deliver + downloaded standalone daemon + actual files."""
import hashlib
import json
import os
from pathlib import Path
from acceptance_paths import evidence_path
import subprocess
from uuid import uuid4

from playwright.sync_api import expect,sync_playwright
from cloud_document_acceptance import PRIVATE


def main():
    private=json.loads(PRIVATE.read_text());web=os.environ['CAD_NATIVE_E2E_WEB']
    release=json.loads(evidence_path('cad-expansion-release-browser.json').read_text())['release_id']
    root=Path('/tmp')/('cad-bridge-browser-'+uuid4().hex[:10]);root.mkdir(mode=0o700)
    output=root/'released';output.mkdir(mode=0o700);config=root/'private.json';client=root/'cad_local_bridge.py'
    errors=[];console_errors=[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        page=browser.new_context(viewport={'width':1440,'height':1000}).new_page()
        page.on('pageerror',lambda e:errors.append(str(e)));page.on('console',lambda m:console_errors.append(m.text) if m.type=='error' else None)
        page.goto(web);page.locator('input[type="text"]').fill(private['owner']['phone'])
        page.locator('input[type="password"]').fill(private['owner']['password']);page.locator('button[type="submit"]').click()
        expect(page.get_by_role('button',name='新建设计',exact=True)).to_be_visible(timeout=20000)
        page.goto(web+'?document='+private['document_id']+'&workspace='+private['tenant_id'])
        panel=page.get_by_test_id('local-bridge-panel');panel.locator('summary',has_text='本地 Bridge 交付').click()
        panel.locator('summary',has_text='配对本地客户端').click()
        with page.expect_download() as downloaded:panel.get_by_role('button',name='下载本地客户端').click()
        downloaded.value.save_as(client)
        source=Path(__file__).resolve().parents[2]/'app/integrations/local_bridge_client.py'
        assert hashlib.sha256(client.read_bytes()).hexdigest()==hashlib.sha256(source.read_bytes()).hexdigest()
        panel.get_by_role('textbox',name='本地连接名称').fill('Browser directory '+root.name[-10:])
        with page.expect_response(lambda r:r.url.endswith('/bridges/pair') and r.request.method=='POST') as created:
            panel.get_by_role('button',name='生成一次性配对码').click()
        assert created.value.status==201
        bridge_id=created.value.json()['bridge_id']
        code=panel.get_by_role('textbox',name='一次性配对码').input_value()
        paired=subprocess.run([os.sys.executable,str(client),'pair','--server',web.rstrip('/'),'--directory',str(output),'--config',str(config)],
            input=code+'\n',capture_output=True,text=True,timeout=30)
        assert paired.returncode==0,(paired.stdout,paired.stderr)
        bridge=panel.locator(f'[data-bridge="{bridge_id}"]');expect(bridge).to_contain_text('在线',timeout=20000)
        expect(panel.get_by_role('textbox',name='一次性配对码')).to_have_count(0)
        panel.locator('summary',has_text='配对本地客户端').click()
        panel.get_by_role('combobox',name='本地交付发布版本').select_option(release)
        panel.get_by_role('combobox',name='本地交付目标连接').select_option(bridge_id)
        with page.expect_response(lambda r:r.url.endswith('/deliveries') and r.request.method=='POST') as delivered:
            panel.get_by_role('button',name='交付发布文件').click()
        assert delivered.value.status==202;delivery_id=delivered.value.json()['delivery_id']
        actual=subprocess.run([os.sys.executable,str(client),'run','--config',str(config),'--once'],capture_output=True,text=True,timeout=60)
        assert actual.returncode==0,(actual.stdout,actual.stderr)
        row=panel.locator(f'[data-delivery="{delivery_id}"]');expect(row).to_contain_text('文件已送达',timeout=20000)
        expect(row).to_contain_text('已校验 14 个文件')
        assert len(list(output.rglob('design.FCStd')))==1
        row.scroll_into_view_if_needed();page.screenshot(path=str(evidence_path('cad-expansion-bridge-browser.png')))
        bridge.get_by_role('button',name='撤销连接').click();expect(bridge).to_contain_text('已撤销',timeout=10000)
        assert not errors and not console_errors,(errors,console_errors)
        evidence={'bridge_id':bridge_id,'delivery_id':delivery_id,'downloaded_standalone_client_verified':True,
            'browser_pair_and_deliver':True,'real_local_files':True,'one_use_code_cleared_after_pairing':True,
            'browser_revocation':True,'page_errors':errors,'console_errors':console_errors,'output_directory':str(output)}
        evidence_path('cad-expansion-bridge-browser.json').write_text(json.dumps(evidence,indent=2))
        print('CAD_BRIDGE_BROWSER='+json.dumps(evidence),flush=True);browser.close()


if __name__=='__main__':main()
