"""Wire identities and receiver checks for native FreeCAD references (stdlib-only)."""
import hashlib
import json
import math
import re


SCHEMA = 'native-topology.v1'


_HASH = re.compile(r'^[0-9a-f]{64}$')


CREATED_FEATURE_TYPES = {'feature.pad': 'PartDesign::Pad', 'feature.pocket': 'PartDesign::Pocket',
          'feature.hole': 'PartDesign::Hole', 'feature.fillet': 'PartDesign::Fillet',
          'feature.chamfer': 'PartDesign::Chamfer'}


class NativeReferenceError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def reference_id(incarnation, kind, mapped_name):
    return digest([incarnation, kind, mapped_name])


def validate_catalog(catalog, fcstd_sha256=None):
    """Validate persisted metadata before it can grant native-reference authority."""
    try:
        if catalog['schema_version'] != SCHEMA or not _HASH.fullmatch(catalog['fcstd_sha256']):
            raise ValueError('invalid schema or FCStd identity')
        if fcstd_sha256 is not None and catalog['fcstd_sha256'] != fcstd_sha256:
            raise ValueError('reference catalog belongs to another FCStd')
        if not isinstance(catalog['objects'], dict):
            raise ValueError('invalid object inventory')
        ids = set()
        for name, owner in catalog['objects'].items():
            origin = owner['origin']
            if (not isinstance(name, str) or not name or set(origin) != {'plan_sha256', 'op_id'}
                    or not isinstance(origin['op_id'], str) or not origin['op_id']
                    or owner['incarnation'] != digest([origin, name]) or not _HASH.fullmatch(origin['plan_sha256'])):
                raise ValueError('invalid creation identity')
            if (owner['type_id'] not in CREATED_FEATURE_TYPES.values() or type(owner['native_id']) is not int
                    or owner['native_id'] <= 0 or not isinstance(owner['map_version'], str) or not owner['map_version']):
                raise ValueError('unsupported owner')
            if owner['coverage']['recorded'] != len(owner['references']):
                raise ValueError('incomplete reference inventory')
            coverage = owner['coverage']
            if (any(type(coverage[key]) is not int or coverage[key] < 0 for key in ('total', 'recorded', 'unavailable'))
                    or coverage['total'] != coverage['recorded'] + coverage['unavailable']):
                raise ValueError('invalid reference coverage')
            for ref in owner['references']:
                kind = ref['subelement_kind']
                if kind not in {'face', 'edge'} or not ref['mapped_name'] or len(ref['mapped_name']) > 4096:
                    raise ValueError('invalid native name')
                expected = reference_id(owner['incarnation'], kind, ref['mapped_name'])
                if ref['reference_id'] != expected or expected in ids:
                    raise ValueError('duplicate or altered reference identity')
                ids.add(expected)
                if (not isinstance(ref['geometry_type'], str) or not ref['geometry_type']
                        or not all(type(v) in (int, float) and math.isfinite(v) for v in ref['center_mm'])
                        or len(ref['center_mm']) != 3):
                    raise ValueError('invalid measurement')
                if 'radius_mm' in ref and (type(ref['radius_mm']) not in (int, float)
                        or not math.isfinite(ref['radius_mm']) or ref['radius_mm'] <= 0):
                    raise ValueError('invalid radius')
        for use in catalog['uses']:
            owner = catalog['objects'].get(use['object_name'], {})
            if (use['feature'] not in catalog['objects']
                    or use['reference_id'] not in {ref['reference_id'] for ref in owner.get('references', [])}):
                raise ValueError('missing dress-up reference')
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise NativeReferenceError('topology_reference_catalog_invalid', str(exc)) from exc
    return catalog


def selectors(catalog, object_name, revision_id):
    if not catalog or revision_id is None:
        return []
    owner = validate_catalog(catalog)['objects'].get(object_name)
    if owner is None:
        return []
    return [{'schema_version': 'topology-selector.v2', 'backend': 'freecad',
             'revision_id': str(revision_id), 'object_name': object_name,
             'subelement_kind': ref['subelement_kind'], 'reference_id': ref['reference_id']}
            for ref in owner['references']]


def verify_transition(catalog, plan, base=None):
    """The receiver checks creation provenance independently of the runner."""
    validate_catalog(catalog)
    if base is not None:
        validate_catalog(base)
    operations = plan['operations']
    if any(op['action'] == 'api.execute' for op in operations):
        if catalog['objects'] or catalog['uses']:
            raise NativeReferenceError('topology_reference_unverified', 'Native programs cannot publish typed creation provenance')
        return
    prior = (base or {}).get('objects', {})
    created = {op['args']['name']: op for op in operations if op['action'] in CREATED_FEATURE_TYPES}
    if set(catalog['objects']) != set(prior) | set(created):
        raise NativeReferenceError('topology_reference_unverified', 'Saved creation inventory differs from the executed typed plan')
    for name, owner in catalog['objects'].items():
        if name in prior and all(owner[k] == prior[name][k] for k in ('origin', 'incarnation', 'native_id', 'type_id')):
            continue
        op = created.get(name)
        if (op is None or owner['origin'] != {'plan_sha256': digest(plan), 'op_id': op['op_id']}
                or owner['type_id'] != CREATED_FEATURE_TYPES[op['action']]):
            raise NativeReferenceError('topology_reference_unverified', 'Creation provenance is not in the executed typed plan')
    expected_uses = list((base or {}).get('uses', []))
    for op in created.values():
        selector = op['args'].get('selector')
        if selector and selector.get('schema_version') == 'topology-selector.v2':
            use = {'feature': op['args']['name'], 'object_name': selector['object_name'],
                   'reference_id': selector['reference_id']}
            if use not in expected_uses:
                expected_uses.append(use)
    if catalog['uses'] != expected_uses:
        raise NativeReferenceError('topology_reference_unverified', 'Saved dependent references differ from the executed plan')
