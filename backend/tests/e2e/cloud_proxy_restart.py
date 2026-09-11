"""Real API address change with a running Nginx and an open browser document.

Requires explicit CAD_NATIVE_E2E_API and CAD_NATIVE_E2E_FRONTEND container names.
Run only against an isolated acceptance deployment. No reload of Nginx is used.
"""
import base64
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright
from websockets.sync.client import connect

from cloud_document_acceptance import BASE, PRIVATE, call


def main():
    api = os.environ['CAD_NATIVE_E2E_API']
    frontend = os.environ['CAD_NATIVE_E2E_FRONTEND']
    runtime = os.getenv('CAD_NATIVE_E2E_PODMAN', '/opt/homebrew/bin/podman')
    web = os.environ['CAD_NATIVE_E2E_WEB']
    private = json.loads(PRIVATE.read_text())
    client = httpx.Client(headers={'Authorization': 'Bearer '+private['owner']['token']})
    path = '/api/documents/'+private['document_id']

    def inspect(container, field):
        return subprocess.check_output([runtime, 'inspect', '--format', '{{json .'+field+'}}', container], text=True).strip()

    def session_socket():
        protocol = 'cad-agent-auth.'+base64.urlsafe_b64encode(private['owner']['token'].encode()).decode().rstrip('=')
        with connect(BASE.replace('http', 'ws', 1)+'/ws/'+str(uuid4()), subprotocols=[protocol], open_timeout=15):
            pass

    before = call(client, 'GET', path)
    call(client, 'GET', '/ready')
    session_socket()
    previous_networks = json.loads(inspect(api, 'NetworkSettings.Networks'))
    previous_start = inspect(api, 'State.StartedAt')
    frontend_start = inspect(frontend, 'State.StartedAt')
    snapshots, errors = [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
        page = browser.new_context(viewport={'width': 1440, 'height': 1000}).new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))

        def watch(socket):
            if path+'/stream' not in socket.url:
                return

            def frame(payload):
                value = json.loads(payload)
                if value.get('type') == 'document_snapshot':
                    snapshots.append(value['data'])

            socket.on('framereceived', frame)

        page.on('websocket', watch)
        page.goto(web)
        page.locator('input[type="text"]').fill(private['owner']['phone'])
        page.locator('input[type="password"]').fill(private['owner']['password'])
        page.locator('button[type="submit"]').click()
        expect(page.get_by_role('button', name='新建设计', exact=True)).to_be_visible(timeout=20000)
        page.goto(web+'?document='+private['document_id']+'&workspace='+private['tenant_id'])
        expect(page.locator('canvas')).to_be_visible(timeout=20000)
        expect(page.get_by_role('treeitem', name='Hole', exact=True)).to_be_visible(timeout=20000)
        page.wait_for_timeout(1000)
        assert snapshots, 'browser never received its initial document snapshot'
        prior_snapshots = len(snapshots)
        subprocess.run([runtime, 'restart', '--time', '10', api], check=True, capture_output=True, timeout=45)
        started = time.monotonic()
        last_status = None
        while time.monotonic()-started < 75:
            try:
                response = client.get(BASE+'/ready', timeout=8)
                last_status = response.status_code
                if response.status_code == 200 and response.json()['status'] == 'ready':
                    break
            except httpx.HTTPError:
                pass
            page.wait_for_timeout(1000)
        else:
            raise AssertionError(f'Nginx did not recover after API restart: HTTP {last_status}')
        ready_seconds = time.monotonic()-started
        deadline = time.monotonic()+30
        while len(snapshots) <= prior_snapshots and time.monotonic() < deadline:
            page.wait_for_timeout(500)
        assert len(snapshots) > prior_snapshots, 'open document did not reconnect its real WebSocket'
        assert snapshots[-1]['head_revision_id'] == before['head_revision_id']
        expect(page.get_by_role('treeitem', name='Hole', exact=True)).to_be_visible()
        assert page.locator('canvas').evaluate("c => !!c.getContext('webgl2')")
        assert not errors, errors
        browser.close()
    current_networks = json.loads(inspect(api, 'NetworkSettings.Networks'))
    changed = {name: {'before': value['IPAddress'], 'after': current_networks[name]['IPAddress']}
               for name, value in previous_networks.items() if value['IPAddress'] != current_networks[name]['IPAddress']}
    if os.getenv('CAD_NATIVE_E2E_REQUIRE_ADDRESS_CHANGE') == '1':
        assert changed, 'address change was not exercised'
    assert inspect(api, 'State.StartedAt') != previous_start
    assert inspect(frontend, 'State.StartedAt') == frontend_start
    after = call(client, 'GET', path)
    assert (after['head_revision_id'], after['state_version'], after['fcstd']) == (before['head_revision_id'], before['state_version'], before['fcstd'])
    assert call(client, 'GET', path+'/events?after='+str(after['event_sequence']))['events'] == []
    session_socket()
    evidence = {'api_container': api, 'frontend_container': frontend, 'changed_addresses': changed,
                'proxy_recovered_seconds': round(ready_seconds, 3), 'frontend_not_restarted': True,
                'authenticated_rest_and_query_preserved': True, 'session_websocket_reconnected': True,
                'open_browser_document_reconnected': True, 'document_head_unchanged': True, 'page_errors': errors}
    Path('/tmp/cad-expansion-proxy-restart.json').write_text(json.dumps(evidence, indent=2))
    print('CAD_PROXY_RESTART='+json.dumps(evidence), flush=True)


if __name__ == '__main__':
    main()
