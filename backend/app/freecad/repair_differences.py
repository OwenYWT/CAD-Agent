"""Field-level rejection evidence for immutable operation-plan boundaries."""
from .contracts import FreeCADPlanningError
from pydantic import ValidationError


class RepairRejected(FreeCADPlanningError):
    def __init__(self, code, reason, differences=()):
        super().__init__(code, reason)
        self.differences = list(differences)


def rejection_differences(error):
    if hasattr(error, 'differences'):
        return error.differences
    if isinstance(error, ValidationError):
        return [{'field': '.'.join(map(str, row['loc'])), 'error_type': row['type'], 'reason': row['msg']}
                for row in error.errors(include_url=False, include_context=False, include_input=False)]
    return []


def operation_differences(before, after):
    old = {op.op_id: op.model_dump(mode='json') for op in before}
    new = {op.op_id: op.model_dump(mode='json') for op in after}
    result = []

    def compare(identity, left, right, path):
        if left == right:
            return
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(left.keys() | right.keys()):
                compare(identity, left.get(key), right.get(key), f'{path}.{key}' if path else key)
        else:
            result.append({'operation_id': identity, 'field': path,
                           'before': left, 'after': right})

    for identity in dict.fromkeys([*old, *new]):
        compare(identity, old.get(identity), new.get(identity), '')
    if list(old) != list(new):
        result.append({'operation_id': None, 'field': 'operation_order',
                       'before': list(old), 'after': list(new)})
    return result
