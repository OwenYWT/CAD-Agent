"""Authorized, disposable geometry cache derived only from verified FCStd bytes."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import tempfile
from uuid import UUID, uuid4
import zipfile

from sqlalchemy import text

from app.db import tenant_transaction
from app.freecad.semantic_state import feature_id
from app.object_store import download_object, get_object, put_object, sha256_object
from app.services.cloud_documents import authorized_document, checkpoint

LOD = {'coarse', 'medium', 'fine'}
HEX = re.compile(r'^[a-f0-9]{64}$')
MAX_BYTES = 128 * 1024 * 1024


def validate_scene(scene):
    if scene.get('schema_version') != 'cad-scene.v1' or scene.get('units') != 'mm':
        raise ValueError('invalid kernel scene schema')
    definitions, instances = scene.get('definitions'), scene.get('instances')
    if not isinstance(definitions, dict) or not isinstance(instances, list) or not 1 <= len(instances) <= 5000:
        raise ValueError('invalid kernel scene inventory')
    names = set()
    for instance in instances:
        name, matrix = instance.get('kernel_name'), instance.get('matrix')
        if not isinstance(name, str) or name in names or re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', name) is None:
            raise ValueError('invalid or duplicate scene component name')
        names.add(name)
        if instance.get('geometry_sha256') not in definitions or not isinstance(matrix, list) or len(matrix) != 16:
            raise ValueError('invalid scene transform or geometry reference')
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in matrix):
            raise ValueError('scene transform is not finite')
    for digest, definition in definitions.items():
        if HEX.fullmatch(digest) is None:
            raise ValueError('invalid geometry digest')
        bounds = definition.get('bounds_mm')
        if not isinstance(bounds, list) or len(bounds) != 6 or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in bounds):
            raise ValueError('invalid component bounds')
    return scene


def _public_scene(projection, document_id, revision_id):
    # The database projection has storage IDs; only document-authorized URLs
    # and content hashes cross the browser boundary.
    result = json.loads(json.dumps(projection))
    result.update(document_id=str(document_id), revision_id=str(revision_id))
    for instance in result['instances']:
        instance['feature_id'] = feature_id(UUID(result.get('lineage_id', str(document_id))), instance['kernel_name'])
    for definition in result['definitions'].values():
        for lod in definition['lods'].values():
            lod.pop('blob_id', None)
            lod.pop('name', None)
            lod['url'] = f"/api/documents/{document_id}/scenes/{revision_id}/meshes/{lod['sha256']}"
    return result


async def build_document_scene(context, document_id, revision_id, scene_task, execute_scene, complete_job):
    projection = await checkpoint(context, document_id, revision_id)
    if not projection['fcstd']:
        raise ValueError('此版本没有原生 FCStd，无法生成部件场景')
    runtime = scene_task["runtime_digest"]
    schema = scene_task["schema_version"]
    async with tenant_transaction(context.tenant_id, context.principal_id) as conn:
        doc = await authorized_document(conn, context, document_id)
        existing = await conn.scalar(text('SELECT projection FROM document_scenes WHERE document_id=:doc AND revision_id=:revision AND runtime_digest=:runtime AND scene_schema=:schema'),
            {'doc':document_id, 'revision':revision_id, 'runtime':runtime, 'schema':schema})
        if existing is not None:
            await complete_job(conn)
            return _public_scene(existing, document_id, revision_id)
        source = (await conn.execute(text('SELECT * FROM artifacts WHERE id=:id'), {'id':UUID(projection['fcstd']['artifact_id'])})).mappings().one()
        cached = [dict(row) for row in (await conn.execute(text('SELECT * FROM document_geometry_blobs WHERE project_id=:project AND runtime_digest=:runtime'),
            {'project':doc['project_id'], 'runtime':runtime})).mappings()]
    by_geometry = {}
    for row in cached:
        by_geometry.setdefault(row['geometry_sha256'], {})[row['lod']] = row
    known = scene_task['known_definitions']
    if any(digest not in by_geometry or set(by_geometry[digest]) != LOD for digest in known):
        raise ValueError('冻结的部件缓存已缺失，不能替换为其他版本网格')
    with tempfile.TemporaryDirectory(prefix='cad-scene-input-') as directory:
        path = Path(directory) / 'checkpoint.FCStd'
        measured = await download_object(source['object_key'], path)
        if measured['sha256'] != source['sha256'] or measured['size_bytes'] != source['size_bytes']:
            raise ValueError('场景来源 FCStd 完整性校验失败')
        files = await execute_scene(source, known, Path(directory))
        scene = validate_scene(json.loads(files['scene'].read_text()))
        scene['lineage_id'] = projection.get('lineage_id', str(document_id))
        created = []
        with zipfile.ZipFile(files['meshes']) as archive:
            infos = archive.infolist()
            if sum(i.file_size for i in infos) > MAX_BYTES or len(infos) != len({i.filename for i in infos}):
                raise ValueError('invalid mesh archive budget or duplicate entries')
            expected_files = set()
            for digest, definition in scene['definitions'].items():
                if definition.get('cached'):
                    if digest not in known:
                        raise ValueError('kernel referenced an unavailable geometry cache')
                    definition['lods'] = {lod:{**row['metadata'], 'blob_id':str(row['id'])} for lod,row in by_geometry[digest].items()}
                    continue
                if set(definition['lods']) != LOD:
                    raise ValueError('kernel did not produce all required LODs')
                for lod, item in definition['lods'].items():
                    name = item['name']
                    if name != digest + '-' + lod + '.stl':
                        raise ValueError('invalid mesh filename')
                    expected_files.add(name)
                    payload = archive.read(name)
                    if len(payload) != item['size_bytes'] or hashlib.sha256(payload).hexdigest() != item['sha256']:
                        raise ValueError('component mesh integrity verification failed')
                    key = f"geometry/tenants/{context.tenant_id}/projects/{doc['project_id']}/{item['sha256']}.stl"
                    await put_object(key, payload, content_type='model/stl')
                    stored = await sha256_object(key)
                    if stored['sha256'] != item['sha256'] or stored['size_bytes'] != len(payload):
                        raise RuntimeError('stored component mesh checksum mismatch')
                    blob_id = uuid4()
                    item['blob_id'] = str(blob_id)
                    created.append({'id':blob_id, 'tenant':context.tenant_id, 'project':doc['project_id'], 'geometry':digest,
                        'runtime':runtime, 'lod':lod, 'sha':item['sha256'], 'size':len(payload), 'key':key,
                        'metadata':json.dumps({k:v for k,v in item.items() if k != 'blob_id'})})
            if expected_files != {i.filename for i in infos}:
                raise ValueError('mesh archive contains undeclared files')
        async with tenant_transaction(context.tenant_id, context.principal_id) as conn:
            await authorized_document(conn, context, document_id)
            await complete_job(conn)
            for row in created:
                await conn.execute(text("""INSERT INTO document_geometry_blobs(id,tenant_id,project_id,geometry_sha256,runtime_digest,lod,sha256,size_bytes,object_key,metadata)
                    VALUES(:id,:tenant,:project,:geometry,:runtime,:lod,:sha,:size,:key,CAST(:metadata AS jsonb)) ON CONFLICT DO NOTHING"""), row)
                winner = (await conn.execute(text('SELECT id,sha256 FROM document_geometry_blobs WHERE project_id=:project AND geometry_sha256=:geometry AND runtime_digest=:runtime AND lod=:lod'), row)).mappings().one()
                if winner['sha256'] != row['sha']:
                    raise RuntimeError('same geometry/runtime/LOD produced different mesh bytes')
                scene['definitions'][row['geometry']]['lods'][row['lod']]['blob_id'] = str(winner['id'])
            await conn.execute(text("""INSERT INTO document_scenes(tenant_id,document_id,revision_id,runtime_digest,scene_schema,projection)
                VALUES(:tenant,:doc,:revision,:runtime,:schema,CAST(:scene AS jsonb)) ON CONFLICT DO NOTHING"""),
                {'tenant':context.tenant_id,'doc':document_id,'revision':revision_id,'runtime':runtime,'schema':schema,'scene':json.dumps(scene)})
        return _public_scene(scene, document_id, revision_id)


async def scene_mesh(context, document_id, revision_id, sha256):
    if HEX.fullmatch(sha256) is None:
        raise KeyError('mesh')
    async with tenant_transaction(context.tenant_id, context.principal_id) as conn:
        doc = await authorized_document(conn, context, document_id)
        scenes = (await conn.execute(text('SELECT projection FROM document_scenes WHERE document_id=:doc AND revision_id=:revision'),
            {'doc':document_id,'revision':revision_id})).scalars().all()
        blob_ids = {UUID(item['blob_id']) for scene in scenes for definition in scene['definitions'].values()
            for item in definition['lods'].values() if item['sha256'] == sha256}
        if not blob_ids:
            raise KeyError('mesh not in document revision')
        row = (await conn.execute(text('SELECT * FROM document_geometry_blobs WHERE id=:id AND project_id=:project'),
            {'id':next(iter(blob_ids)),'project':doc['project_id']})).mappings().one()
    payload = await get_object(row['object_key'])
    if len(payload) != row['size_bytes'] or hashlib.sha256(payload).hexdigest() != sha256:
        raise RuntimeError('部件网格完整性校验失败')
    return payload
