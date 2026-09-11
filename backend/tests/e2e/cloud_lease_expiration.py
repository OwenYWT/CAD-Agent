"""Use the real server's 90-second TTL; no clock or configuration substitution."""
import json
import os
from pathlib import Path
import time
from uuid import uuid4

import httpx

p = json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())
m = json.loads(Path('/tmp/cad-expansion-collaboration-document.json').read_text())
c = httpx.Client(base_url=os.environ['CAD_NATIVE_E2E_URL'], timeout=30,
    headers={'Authorization': 'Bearer ' + p['owner']['token']})
doc = '/api/documents/' + m['document_id']
response = c.get(doc); response.raise_for_status(); snapshot = response.json()
feature = next(f['id'] for f in snapshot['features'] if f['kernel_name'] == 'PadA')
body = {'feature_id': feature, 'revision_id': snapshot['head_revision_id'], 'client_id': str(uuid4())}
r = c.post(doc + '/leases', json=body); r.raise_for_status()
old = r.json()['token']; started = time.monotonic()
print('Lease acquired; waiting for actual server expiration', flush=True)
while time.monotonic() - started < 100:
    response = c.get(doc + '/collaboration'); response.raise_for_status()
    if not any(l['feature_id'] == feature for l in response.json()['leases']): break
    time.sleep(2)
else:
    raise AssertionError('Lease did not expire')
r = c.post(doc + '/leases', json={**body, 'lease_token': old})
assert r.status_code == 409, (r.status_code, r.text)
new = c.post(doc + '/leases', json={**body, 'client_id': str(uuid4())}); new.raise_for_status()
assert new.json()['token'] != old
try:
    r = c.post(doc + '/operations', json={'action': 'parameters.update',
        'expected_base_revision_id': snapshot['head_revision_id'], 'expected_state_version': snapshot['state_version'],
        'allow_rebase': False, 'lease_token': old, 'idempotency_key': str(uuid4()),
        'modification': {'expected_state_sha256': snapshot['parameter_state_sha256'],
            'parameter_updates': [{'parameter_id': 'PadA.Length', 'value': 16}]}})
    assert r.status_code == 409, (r.status_code, r.text)
finally:
    c.delete(doc + '/leases/' + new.json()['token']).raise_for_status()
report = {'real_server_expiration': True, 'expired_renewal_rejected': True,
    'expired_submission_rejected': True, 'new_fencing_token': True, 'elapsed_s': round(time.monotonic() - started, 1)}
Path('/tmp/cad-expansion-lease-expiry.json').write_text(json.dumps(report, indent=2))
print(json.dumps(report), flush=True)
c.close()
