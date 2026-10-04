"""Real authentication, role, TLS/WebSocket and private S3 delivery checks."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import ssl
import sys
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import boto3
from botocore.config import Config
import httpx
from websockets.sync.client import connect
from websockets.exceptions import InvalidStatus


def main(mode):
    private = Path(os.environ['CAD_NATIVE_E2E_PRIVATE'])
    base = os.environ['CAD_NATIVE_E2E_URL']
    admin = httpx.Client(base_url=base, timeout=30)

    def request(client, method, path, status=200, **kwargs):
        result = client.request(method, path, **kwargs)
        assert result.status_code == status, (method, path.split('?', 1)[0], result.status_code)
        return result.json() if result.content else None

    if mode == 'accounts':
        session = request(admin, 'POST', '/api/auth/login/password', json={'phone': 'admin', 'password': os.environ['CAD_CI_ADMIN_PASSWORD']})
        admin.headers['Authorization'] = 'Bearer ' + session['token']
        people = {'admin': {'phone': 'admin', 'password': os.environ['CAD_CI_ADMIN_PASSWORD'], **session}}
        for role in ('owner', 'editor', 'viewer', 'outsider'):
            invitation = request(admin, 'POST', '/api/auth/invites', json={'max_uses': 1})
            phone, password = '19' + str(secrets.randbelow(10**9)).zfill(9), secrets.token_urlsafe(24)
            person = request(admin, 'POST', '/api/auth/register/invite', json={'phone': phone, 'password': password, 'invite_code': invitation['code']})
            people[role] = dict(person, phone=phone, password=password)
        with private.open('x') as stream:
            private.chmod(0o600)
            json.dump(people, stream)
        return
    if mode != 'permissions':
        raise ValueError('unknown delivery phase')
    people = json.loads(private.read_text())
    report = Path(os.environ['CAD_CI_DEPLOY_REPORT'])
    browser = json.loads((report.parent/'packaged-browser/report.json').read_text())
    path = '/api/documents/' + browser['fixture']['document_id']
    clients = {role: httpx.Client(base_url=base, timeout=30, headers={'Authorization': 'Bearer ' + person['token']}) for role, person in people.items()}
    owner = clients['owner']
    saved = request(owner, 'GET', path)
    rows = []

    def check(role, method, url, status, **kwargs):
        result = request(clients[role], method, url, status, **kwargs)
        rows.append({'role': role, 'method': method, 'endpoint': url.split('?', 1)[0], 'status': status})
        return result

    invites = {}
    for role in ('editor', 'viewer'):
        invite = request(owner, 'POST', path+'/invitations?role='+role, 201)
        invites[role] = invite
        request(clients[role], 'POST', path+'/invitations/accept', json={'tenant_id': invite['tenant_id'], 'token': invite['token']})
        clients[role].headers['X-Workspace-Tenant'] = invite['tenant_id']
    clients['anonymous'] = httpx.Client(base_url=base, timeout=30)
    clients['invalid-token'] = httpx.Client(base_url=base, timeout=30, headers={'Authorization': 'Bearer invalid-ci-token'})
    for role in ('owner', 'editor', 'viewer'):
        viewed = check(role, 'GET', path, 200)
        assert viewed['can_edit'] == (role != 'viewer')
    for role in ('anonymous', 'invalid-token'):
        check(role, 'GET', path, 401)
    check('outsider', 'GET', path, 404)
    clients['outsider'].headers['X-Workspace-Tenant'] = invites['viewer']['tenant_id']
    check('outsider', 'GET', path, 403)
    for role in ('owner', 'editor', 'viewer'):
        body = {'feature_id': saved['features'][0]['id'], 'client_id': str(uuid4()), 'revision_id': saved['head_revision_id']}
        lease = check(role, 'POST', path+'/leases', 403 if role == 'viewer' else 200, json=body)
        if role != 'viewer':
            check(role, 'DELETE', path+'/leases/'+lease['token'], 204)
        if role != 'owner':
            check(role, 'GET', path+'/members', 403)
    check('owner', 'GET', path+'/members', 200)
    members = request(owner, 'GET', path+'/members')['members']
    editor = next(row for row in members if row['role'] == 'editor')
    check('owner', 'DELETE', path+'/members/'+editor['principal_id']+'?expected_role=editor', 204)
    # Revoking the last project membership also removes workspace membership;
    # the workspace authorization edge rejects this request before document RLS.
    check('editor', 'GET', path, 403)
    check('editor', 'POST', path+'/invitations/accept', 403, json={'tenant_id': invites['editor']['tenant_id'], 'token': invites['editor']['token']})
    assert request(owner, 'GET', path)['head_revision_id'] == saved['head_revision_id']

    tls = os.environ['CAD_CI_TLS_URL']
    ca = os.environ['CAD_CI_TLS_CA']
    context = ssl.create_default_context(cafile=ca)
    with httpx.Client(base_url=tls, verify=context, timeout=30) as edge:
        request(edge, 'GET', '/health')
        edge.headers.update(owner.headers)
        assert request(edge, 'GET', path)['head_revision_id'] == saved['head_revision_id']
    protocol = 'cad-agent-auth.' + base64.urlsafe_b64encode(people['owner']['token'].encode()).decode().rstrip('=')
    ws = tls.replace('https://', 'wss://') + path + '/stream'
    with connect(ws, ssl_context=context, subprotocols=[protocol], open_timeout=30) as socket:
        event = json.loads(socket.recv(timeout=30))
        assert event['type'] == 'document_snapshot' and event['data']['head_revision_id'] == saved['head_revision_id']
    try:
        with connect(ws, ssl_context=context, open_timeout=30):
            raise AssertionError('anonymous document WebSocket was accepted')
    except InvalidStatus as error:
        assert error.response.status_code == 403
    # Host Nginx preserves the port and URI needed by S3 SigV4. Use real MinIO,
    # verify signatures and ensure the unsigned bucket remains inaccessible.
    s3 = boto3.client('s3', endpoint_url=tls, region_name='us-east-1', verify=ca,
        aws_access_key_id='cad_ci', aws_secret_access_key=os.environ['MINIO_ROOT_PASSWORD'],
        config=Config(signature_version='s3v4', s3={'addressing_style': 'path'}))
    key, payload = 'ci-delivery/' + uuid4().hex, secrets.token_bytes(256)
    s3.put_object(Bucket='cad-native-artifacts', Key=key, Body=payload)
    signed = s3.generate_presigned_url('get_object', Params={'Bucket': 'cad-native-artifacts', 'Key': key}, ExpiresIn=60)
    with httpx.Client(verify=context, timeout=30) as edge:
        response = edge.get(signed)
        assert response.status_code == 200 and hashlib.sha256(response.content).digest() == hashlib.sha256(payload).digest()
        assert edge.get(tls+'/cad-native-artifacts/'+key).status_code == 403
        signature = parse_qs(urlsplit(signed).query)['X-Amz-Signature'][0]
        changed = ('0' if signature[0] != '0' else '1') + signature[1:]
        assert edge.get(signed.replace('X-Amz-Signature='+signature, 'X-Amz-Signature='+changed)).status_code == 403
    s3.delete_object(Bucket='cad-native-artifacts', Key=key)
    monitor_status = {}
    with httpx.Client(base_url='http://127.0.0.1:18061', timeout=30) as monitoring:
        for role, status in [('admin',200), ('owner',403), ('anonymous',401)]:
            monitoring.headers.pop('Authorization',None)
            if role != 'anonymous':
                monitoring.headers['Authorization'] = 'Bearer '+people[role]['token']
            request(monitoring,'GET','/api/monitor/accounts',status)
            monitor_status[role] = status
    report.write_text(json.dumps({'passed': True, 'auth_required': True, 'permissions': rows,
        'roles': ['owner', 'editor', 'viewer', 'outsider', 'anonymous', 'invalid-token', 'revoked-editor'],
        'revoked_invite_cannot_restore_access': True, 'head_unchanged': True,
        'tls_rest': True, 'authenticated_websocket': True, 'anonymous_websocket_denied': True,
        'private_s3_sigv4': True, 'self_signed_ci_certificate': True, 'monitor_permissions': monitor_status}, indent=2))
    for client in clients.values():
        client.close()
    admin.close()


if __name__ == '__main__':
    main(sys.argv[1])
