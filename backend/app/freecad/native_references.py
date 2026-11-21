"""Native names backed by a verified FCStd and typed-operation provenance.

This module is shared with the FreeCAD runner and uses only the standard library.
Names are looked up in the complete kernel map, never via FreeCAD's ordinal
fallback. A reference remains scoped to its object incarnation and input file.
"""
from __future__ import annotations

import re

try:
    from freecad_native_reference_contracts import (
        SCHEMA, CREATED_FEATURE_TYPES, NativeReferenceError, digest, reference_id, validate_catalog,
    )
except ModuleNotFoundError:
    from app.contracts.native_references import (
        SCHEMA, CREATED_FEATURE_TYPES, NativeReferenceError, digest, reference_id, validate_catalog,
    )


_INDEX = re.compile(r'^(Face|Edge)([1-9][0-9]*)$')


def _indexed(obj, mapped_name, kind, maps=None):
    # Reading the full map avoids the old EdgeN suffix fallback in getSubObject.
    if _INDEX.fullmatch(mapped_name):
        raise NativeReferenceError('topology_mapping_unavailable', 'An ordinal name cannot prove native identity')
    mapping = obj.Shape.ElementMap if maps is None else maps[0]
    indexed = mapping.get(mapped_name)
    match = _INDEX.fullmatch(indexed or '')
    if match is None or match[1].lower() != kind:
        raise NativeReferenceError('topology_reference_missing', 'Native name no longer maps to the selected element')
    aliases = [key for key, value in mapping.items() if value == indexed] if maps is None else maps[1][indexed]
    if aliases != [mapped_name]:
        raise NativeReferenceError('topology_reference_ambiguous', 'Native element has multiple source names; reselect it')
    elements = obj.Shape.Faces if kind == 'face' else obj.Shape.Edges
    if int(match[2]) > len(elements):
        raise NativeReferenceError('topology_reference_missing', 'Native index is outside the actual shape')
    return indexed, elements[int(match[2])-1]


def _geometry(element, kind):
    geometry = element.Surface if kind == 'face' else element.Curve
    center = geometry.Center if getattr(geometry, 'TypeId', '') == 'Part::GeomCircle' else element.CenterOfMass
    result = {'geometry_type': str(geometry.TypeId), 'center_mm': list(center)}
    if hasattr(geometry, 'Radius'):
        result['radius_mm'] = float(geometry.Radius)
    return result


class NativeReferences:
    def __init__(self, document, catalog=None, *, fcstd_sha256=None):
        self.owners = {}
        self.uses = []
        self.handles = {}
        self.catalog = validate_catalog(catalog, fcstd_sha256) if catalog is not None else None
        if self.catalog:
            self.owners = {name: {key: owner[key] for key in ('origin', 'incarnation', 'native_id', 'type_id')}
                           for name, owner in self.catalog['objects'].items()}
            self.uses = list(self.catalog['uses'])
            for name, owner in self.owners.items():
                obj = document.getObject(name)
                if obj is None or obj.ID != owner['native_id'] or obj.TypeId != owner['type_id']:
                    raise NativeReferenceError('topology_owner_recreated', 'Reference owner does not match the saved creation identity')
                self.handles[name] = obj

    def invalidate(self):
        # An arbitrary native program may rebuild a same-named object or its map.
        self.owners.clear()
        self.handles.clear()
        self.uses.clear()
        self.catalog = None

    def created(self, document, operation, plan_sha256):
        action, args = operation['action'], operation['args']
        if action not in CREATED_FEATURE_TYPES:
            return
        obj = document.getObject(args['name'])
        if obj is None or obj.TypeId != CREATED_FEATURE_TYPES[action]:
            raise NativeReferenceError('topology_owner_missing', 'Typed creation did not produce its declared owner')
        origin = {'plan_sha256': plan_sha256, 'op_id': operation['op_id']}
        self.owners[obj.Name] = {'origin': origin, 'incarnation': digest([origin, obj.Name]),
                               'native_id': obj.ID, 'type_id': obj.TypeId}
        self.handles[obj.Name] = obj
        selector = args.get('selector')
        if selector and selector.get('schema_version') == 'topology-selector.v2':
            self.uses.append({'feature': obj.Name, 'object_name': selector['object_name'],
                              'reference_id': selector['reference_id']})

    def resolve(self, document, selector, expected_revision_id):
        required = {'schema_version', 'backend', 'revision_id', 'object_name', 'subelement_kind', 'reference_id'}
        if (set(selector) != required or selector['schema_version'] != 'topology-selector.v2'
                or selector['backend'] != 'freecad' or selector['revision_id'] != expected_revision_id):
            raise NativeReferenceError('topology_reference_stale', 'Native selection does not match this revision')
        owner = (self.catalog or {}).get('objects', {}).get(selector['object_name'])
        matches = [r for r in (owner or {}).get('references', []) if r['reference_id'] == selector['reference_id']
                   and r['subelement_kind'] == selector['subelement_kind']]
        if len(matches) != 1:
            raise NativeReferenceError('topology_reference_unverified', 'Reference was not published by the verified input state')
        obj = document.getObject(selector['object_name'])
        if (obj is None or obj != self.handles.get(selector['object_name'])
                or obj.ID != owner['native_id'] or obj.TypeId != owner['type_id']):
            raise NativeReferenceError('topology_owner_recreated', 'Selected owner was deleted or recreated')
        if str(obj.Shape.ElementMapVersion) != owner['map_version']:
            raise NativeReferenceError('topology_mapping_unavailable', 'Native naming version changed')
        ref = matches[0]
        indexed, element = _indexed(obj, ref['mapped_name'], ref['subelement_kind'])
        if _geometry(element, ref['subelement_kind'])['geometry_type'] != ref['geometry_type']:
            raise NativeReferenceError('topology_reference_changed', 'Selected element changed geometric type')
        return {'object_name': obj.Name, 'subelement_name': ';' + ref['mapped_name'] + '.' + indexed,
                'subelement_kind': ref['subelement_kind'], 'reference_id': ref['reference_id'],
                'resolution_method': 'native-element-map', 'incarnation': owner['incarnation']}

    def verify_uses(self, document, expected_revision_id):
        for use in self.uses:
            resolved = self.resolve(document, {'schema_version': 'topology-selector.v2', 'backend': 'freecad',
                'revision_id': expected_revision_id, 'object_name': use['object_name'],
                'subelement_kind': 'edge', 'reference_id': use['reference_id']}, expected_revision_id)
            feature = document.getObject(use['feature'])
            target = document.getObject(use['object_name'])
            base = feature.Base if feature is not None else None
            if (not base or base[0] != target or len(base[1]) != 1 or feature.UseAllEdges
                    or not target.getSubObject(base[1][0]).isSame(target.getSubObject(resolved['subelement_name']))):
                raise NativeReferenceError('topology_dependency_changed', 'Saved dress-up no longer uses its selected native edge')

    def project(self, document, fcstd_sha256):
        objects = {}
        for name, owner in self.owners.items():
            obj = document.getObject(name)
            if obj is None or obj.ID != owner['native_id'] or obj.TypeId != owner['type_id']:
                raise NativeReferenceError('topology_owner_recreated', 'Saved owner differs from its typed creation')
            refs = []
            total = len(obj.Shape.Faces) + len(obj.Shape.Edges)
            mapping = obj.Shape.ElementMap
            reverse = {}
            for mapped, indexed in mapping.items():
                reverse.setdefault(indexed, []).append(mapped)
            for kind, elements in (('face', obj.Shape.Faces), ('edge', obj.Shape.Edges)):
                for index in range(1, len(elements)+1):
                    native = obj.Shape.getElementMappedName(kind.title() + str(index))
                    if not native:
                        continue
                    try:
                        indexed, element = _indexed(obj, native, kind, (mapping, reverse))
                        measurement = _geometry(element, kind)
                    except (NativeReferenceError, AttributeError, RuntimeError, TypeError, ValueError):
                        continue
                    refs.append({'reference_id': reference_id(owner['incarnation'], kind, native),
                                 'mapped_name': native, 'subelement_kind': kind, **measurement})
            objects[name] = {**owner, 'map_version': str(obj.Shape.ElementMapVersion), 'references': refs,
                             'coverage': {'total': total, 'recorded': len(refs), 'unavailable': total-len(refs)}}
        return validate_catalog({'schema_version': SCHEMA, 'fcstd_sha256': fcstd_sha256,
                                 'objects': objects, 'uses': self.uses})
