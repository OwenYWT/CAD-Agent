"""Exercise real ZIP validation and filesystem writes, including replay and tampering."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from uuid import uuid4
import zipfile

import pytest
from pydantic import ValidationError
from app.freecad.release_contracts import ReleaseSubmission
from app.integrations.bridge_contracts import BridgeClientInfo

spec=importlib.util.spec_from_file_location('cad_local_bridge',Path(__file__).resolve().parents[1]/'app/integrations/local_bridge_client.py')
bridge=importlib.util.module_from_spec(spec);spec.loader.exec_module(bridge)


def archive_fixture(extra=None):
    source={'project_id':str(uuid4()),'document_id':str(uuid4()),'revision_id':str(uuid4()),'fcstd_sha256':hashlib.sha256(b'unit-test-input-bytes').hexdigest()}
    files={'design.FCStd':b'unit-test-input-bytes'}
    manifest={'schema_version':'cad-engineering-release.v1','source':source,'files':[{'path':name,'size_bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()} for name,raw in files.items()]}
    files['manifest.json']=json.dumps(manifest).encode()
    if extra:files.update(extra)
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as archive:
        for name,raw in files.items():archive.writestr(name,raw)
    raw=stream.getvalue()
    delivery={**source,'release_id':str(uuid4()),'lease_token':str(uuid4()),'archive_sha256':hashlib.sha256(raw).hexdigest(),
        'archive_size_bytes':len(raw),'manifest_sha256':hashlib.sha256(files['manifest.json']).hexdigest()}
    return raw,delivery


def test_real_filesystem_delivery_replay_reads_every_file_and_never_overwrites(tmp_path):
    raw,delivery=archive_fixture();receipt=bridge.write_release(tmp_path,delivery,raw)
    assert receipt==bridge.write_release(tmp_path,delivery,raw)
    output=Path(receipt['directory'])/'design.FCStd';assert output.stat().st_mode&0o777==0o600
    output.write_bytes(b'local change')
    with pytest.raises(bridge.BridgeError,match='写入后校验失败'):bridge.write_release(tmp_path,delivery,raw)
    assert output.read_bytes()==b'local change'


@pytest.mark.parametrize('name',['../escape','/absolute','sub/file','undeclared.txt'])
def test_archive_traversal_and_undeclared_entries_are_rejected(name):
    raw,delivery=archive_fixture({name:b'payload'})
    with pytest.raises(bridge.BridgeError):bridge.verified_archive(raw,delivery)


def test_archive_and_manifest_hashes_bind_exact_input():
    raw,delivery=archive_fixture()
    for changes in ({'archive_sha256':'0'*64},{'manifest_sha256':'0'*64},{'document_id':str(uuid4())}):
        with pytest.raises(bridge.BridgeError):bridge.verified_archive(raw,{**delivery,**changes})


def test_existing_symlink_destination_rejected(tmp_path):
    raw,delivery=archive_fixture();outside=tmp_path/'outside';outside.mkdir()
    (tmp_path/delivery['project_id']).symlink_to(outside,target_is_directory=True)
    with pytest.raises(bridge.BridgeError,match='符号链接'):bridge.write_release(tmp_path,delivery,raw)
    assert not list(outside.iterdir())


@pytest.mark.parametrize('url',['http://example.com','https://user:pass@example.com','https://example.com/api','https://example.com?token=1','file:///tmp'])
def test_daemon_credentials_only_go_to_explicit_secure_origin(url):
    with pytest.raises(bridge.BridgeError):bridge.validate_server(url)


def test_unpaired_config_permissions_and_duplicate_process_lock(tmp_path):
    config=tmp_path/'bridge.json';config.write_text(json.dumps({'server':'http://127.0.0.1:8018','directory':str(tmp_path)}));config.chmod(0o644)
    with pytest.raises(bridge.BridgeError,match='600'):bridge.read_config(config)
    config.chmod(0o600);assert bridge.read_config(config)['directory']==str(tmp_path)
    with bridge.config_lock(config):
        with pytest.raises(bridge.BridgeError,match='已经在运行'):
            with bridge.config_lock(config):pass


def test_release_rejects_duplicate_selected_evidence_and_whitespace_names():
    value={'release_name':'v1','expected_revision_id':uuid4(),'expected_state_version':0,'idempotency_key':'release'}
    duplicate=uuid4()
    for changes in ({'release_name':'  '},{'engineering_workflow_ids':[duplicate,duplicate]},{'source':{}}):
        with pytest.raises(ValidationError):ReleaseSubmission.model_validate({**value,**changes})


def test_bridge_does_not_advertise_unimplemented_hardware_or_execute_commands():
    value={'protocol':'cad-local-bridge.v1','version':'1.0.0','hostname':'local','target_label':'CAD'}
    for changes in ({'capabilities':['serial_execute']},{'command':'rm'},{'capabilities':['arbitrary_plugins']}):
        with pytest.raises(ValidationError):BridgeClientInfo.model_validate({**value,**changes})
