"""Read-only component tessellation of a persisted FCStd, with rigid instances."""
from __future__ import annotations

import hashlib
import json
import re
import struct
import zipfile
from pathlib import Path

import FreeCAD as App
import MeshPart
import Part
from freecad_topology import resolve_topology_selector, TopologyResolutionError

LOD = {'coarse': (0.8, 0.8), 'medium': (0.15, 0.35), 'fine': (0.03, 0.12)}
MAX_INSTANCES = 5000
MAX_MESH_BYTES = 128 * 1024 * 1024


def _parent_placement(document, obj):
    parents = [p for p in document.Objects if p.TypeId == 'App::Part' and obj in p.Group]
    if len(parents) > 1:
        raise ValueError('component belongs to multiple placement containers')
    if not parents:
        return App.Placement()
    parent = parents[0]
    return _parent_placement(document, parent).multiply(parent.Placement)


def component_shapes(document, *, visible_only=False):
    """Body tips, standalone solids and links, without intermediate features."""
    body_children = {o.Name for b in document.Objects if b.TypeId == 'PartDesign::Body' for o in b.Group}
    result = []
    ledger=document.getObject('CADAgentLedger')
    selected=set(getattr(ledger,'ExportObjects',()) or ()) if ledger is not None else set()
    if selected and not selected <= {o.Name for o in document.Objects}:
        raise ValueError('declared final export object is missing')
    for obj in document.Objects:
        if selected and obj.Name not in selected:
            continue
        if obj.Name in body_children or obj.TypeId == 'App::Part':
            continue
        shape = getattr(obj, 'Shape', None)
        if shape is None or shape.isNull() or not shape.Solids:
            continue
        if visible_only and getattr(obj, 'Visibility', True) is False:
            continue
        if obj.TypeId == 'App::Link' and getattr(obj, 'ElementCount', 0):
            raise ValueError('link arrays require expansion into explicit instances')
        placed = shape.copy()
        placed.Placement = _parent_placement(document, obj).multiply(shape.Placement)
        result.append((obj, placed))
    if len(result) > MAX_INSTANCES:
        raise ValueError(f'scene exceeds the {MAX_INSTANCES} instance limit')
    return result


def tessellate_scene(document, output: Path, known_definitions=()):
    known = set(known_definitions)
    if len(known) > 10000 or any(not isinstance(k, str) or re.fullmatch(r'[a-f0-9]{64}', k) is None for k in known):
        raise ValueError('invalid known geometry definitions')
    definitions, instances, generated = {}, [], {}
    total_bytes = 0
    for obj, placed in component_shapes(document, visible_only=True):
        source = obj.getLinkedObject(True) if obj.TypeId == 'App::Link' else obj
        if getattr(source, 'Shape', None) is None:
            raise ValueError('instance source must be a solid component')
        # Do not hash Link's wrapper topology. It is an instance of its source
        # component; only the world matrix belongs to the link.
        local = source.Shape.copy()
        local.Placement = App.Placement()
        digest = hashlib.sha256(local.exportBrepToString().encode()).hexdigest()
        face_bindings = []
        for axis in 'xyz':
            for extreme in ('min', 'max'):
                selector = {'schema_version': 'topology-selector.v1', 'backend': 'freecad', 'revision_id': 'scene', 'object_name': obj.Name, 'subelement_kind': 'face', 'geometry': 'planar', 'axis': axis, 'extreme': extreme, 'tolerance_mm': 1e-5}
                try:
                    resolved = resolve_topology_selector(document, selector, expected_revision_id='scene')
                    face = obj.getSubObject(resolved['subelement_name'])
                    matches = [index for index, native in enumerate(obj.Shape.Faces, 1) if native.isSame(face)]
                    if len(matches) == 1:
                        face_bindings.append({'face_index': matches[0], 'axis': axis, 'extreme': extreme})
                except TopologyResolutionError:
                    continue
        instances.append({'face_bindings': face_bindings, 'kernel_name': obj.Name, 'label': obj.Label, 'geometry_sha256': digest,
            'matrix': list(placed.Placement.toMatrix().A), 'is_instance': obj.TypeId == 'App::Link'})
        if digest in definitions:
            continue
        bounds = local.BoundBox
        definition = {'bounds_mm': [bounds.XMin, bounds.YMin, bounds.ZMin, bounds.XMax, bounds.YMax, bounds.ZMax], 'lods': {}}
        definitions[digest] = definition
        if digest in known:
            definition['cached'] = True
            continue
        for lod, (linear, angular) in LOD.items():
            facets, face_ranges = [], []
            for face_index, face in enumerate(local.Faces, 1):
                face_mesh = MeshPart.meshFromShape(Shape=face, LinearDeflection=linear, AngularDeflection=angular, Relative=False)
                start = len(facets)
                facets.extend(face_mesh.Facets)
                face_ranges.append({'face_index': face_index, 'start': start, 'count': len(facets) - start})
                if (84 + 50 * len(facets)) + total_bytes > MAX_MESH_BYTES:
                    raise ValueError('tessellation exceeds the mesh output budget')
            count = len(facets)
            size = 84 + 50 * count
            total_bytes += size
            if count < 1 or total_bytes > MAX_MESH_BYTES:
                raise ValueError('tessellation is empty or exceeds the mesh output budget')
            payload = bytearray(b'CAD Agent component mesh v1'.ljust(80, b'\0') + struct.pack('<I', count))
            for facet in facets:
                values = [*facet.Normal, *(v for point in facet.Points for v in point)]
                payload.extend(struct.pack('<12fH', *values, 0))
            data = bytes(payload)
            name = digest + '-' + lod + '.stl'
            generated[name] = data
            definition['lods'][lod] = {'name': name, 'sha256': hashlib.sha256(data).hexdigest(),
                'size_bytes': len(data), 'triangles': count, 'face_ranges': face_ranges, 'linear_deflection_mm': linear, 'angular_deflection_rad': angular}
    if not instances:
        raise ValueError('FCStd contains no visible solid components')
    scene = {'schema_version': 'cad-scene.v1', 'units': 'mm', 'instances': instances, 'definitions': definitions,
        'generated_definitions': sum(not d.get('cached') for d in definitions.values()),
        'reused_definitions': sum(bool(d.get('cached')) for d in definitions.values())}
    manifest = output / 'scene.json'
    manifest.write_text(json.dumps(scene, sort_keys=True, separators=(',', ':')))
    bundle = output / 'meshes.zip'
    with zipfile.ZipFile(bundle, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted(generated.items()):
            archive.writestr(name, data)
    return {'scene': str(manifest), 'meshes': str(bundle)}


def run_scene(task):
    params = task.get('params')
    if not isinstance(params, dict) or set(params) - {'known_definitions'}:
        raise ValueError('invalid scene parameters')
    filename = task.get('inputs', {}).get('base')
    if not isinstance(filename, str) or Path(filename).name != filename:
        raise ValueError('scene requires a declared FCStd input')
    path = Path('/sandbox/input') / filename
    if path.suffix.lower() != '.fcstd' or not path.is_file() or path.is_symlink():
        raise ValueError('scene FCStd input is invalid')
    document = App.openDocument(str(path))
    try:
        files = tessellate_scene(document, Path('/sandbox/output'), params.get('known_definitions', []))
        return {'schema_version': 'freecad-operation-result.v1', 'status': 'succeeded', 'files': files}
    finally:
        App.closeDocument(document.Name)
