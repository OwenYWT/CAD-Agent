"""Deterministic, evidence-bound repair accounting shared with workflow replay."""
from __future__ import annotations

import hashlib
import json


def observation(snapshot, plan=None):
    failure = snapshot['failure']
    details = failure.get('details') or {}
    code = failure['code']
    family = 'cut' if code == 'subtractive_feature_no_effect' else 'profile' if code == 'profile_geometry_invalid' else 'constraint'
    target = details.get('profile') if family == 'cut' else details.get('object')
    sketch = next((s for s in snapshot['sketches'] if s['name'] == target), None)
    if sketch is None:
        raise ValueError('failed sketch is absent from native repair observation')
    # Source hashes, logical IDs and constraint ordinals are not physical progress.
    state = {'geometry': sketch['geometry'], 'placement': sketch['placement'],
        'constraints': [{k: c.get(k) for k in ('type', 'value', 'first', 'first_position',
            'second', 'second_position', 'third', 'third_position', 'driving')}
            for c in sketch['constraints']],
        'solver': {k: sketch['solver'].get(k) for k in ('solver_status', 'fully_constrained',
            'constraint_status', 'degrees_of_freedom')}}
    if family == 'cut':
        state['cut'] = json.loads(details.get('cut_diagnostic_json') or '{}')
    def stable(value):
        if isinstance(value, float):
            return round(value, 8) or 0  # Normalize signed zero as well as kernel jitter.
        if isinstance(value, dict):
            return {k: stable(v) for k, v in value.items()}
        if isinstance(value, list):
            return [stable(v) for v in value]
        return value
    state = stable(state)
    state['constraints'].sort(key=lambda row: json.dumps(row, sort_keys=True))
    signature = hashlib.sha256(json.dumps(state, sort_keys=True, allow_nan=False).encode()).hexdigest()
    # A valid constraint patch may delete the redundant failing constraint.
    # Its preserved downstream feature, not that deleted ID, proves progress.
    progress_operation = snapshot['failed_operation_id']
    if plan is not None and family == 'constraint' and failure.get('action', '').startswith('sketch.'):
        cursor = next(i for i, op in enumerate(plan['operations']) if op['op_id'] == progress_operation)
        consumers = [op for op in plan['operations'][cursor+1:] if op['action'].startswith('feature.')
            and (target in (op['args'].get('profile'), op['args'].get('path'))
                 or target in (op['args'].get('profiles') or []))]
        if consumers:
            progress_operation = consumers[0]['op_id']
    return {'failed_operation_id': snapshot['failed_operation_id'], 'family': family,
        'target': snapshot['failed_operation_id'] if family == 'cut' else target, 'signature': signature,
        'progress_operation_id': progress_operation,
        'completed_operations': [row['operation_id'] for row in snapshot['operations'] if row['status'] == 'executed']}


class RepairStopped(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class NativeRepairBudget:
    """A cap over all native tool turns plus independent physical-target caps."""
    def __init__(self, policy):
        self.policy = dict(policy)
        for name in ('max_attempts', 'cut_attempts', 'constraint_attempts', 'profile_attempts'):
            if type(self.policy.get(name)) is not int or not 1 <= self.policy[name] <= 32:
                raise ValueError('invalid persisted native repair resource policy')
        self.used = 0
        self.attempts = {}
        self.signatures = {}
        self.pending = None
        self.resolved_operations = []

    def _resolved(self, operation):
        if operation not in self.resolved_operations:
            self.resolved_operations.append(operation)

    def succeeded(self):
        if self.pending is not None:
            self._resolved(self.pending['failed_operation_id'])
        self.pending = None

    def reserve(self, failure):
        def stop(reason, code='agent_freecad_repair_no_progress'):
            resolved = '、'.join(self.resolved_operations) or '无'
            raise RepairStopped(code, f"当前失败操作：{failure.get('operation_id')}；已通过的修复操作：{resolved}；"
                f"停止原因：{reason}（已使用 {self.used}/{self.policy['max_attempts']} 次原生修复）。"
                f"原始错误 {failure.get('error_code')}：{failure.get('error_message')}")
        try:
            current = json.loads((failure.get('details') or {}).get('repair_progress_json') or '{}')
            if (current['failed_operation_id'] != failure['operation_id'] or not current['target']
                    or not current['signature'] or current['family'] not in {'cut', 'constraint', 'profile'}
                    or not isinstance(current['completed_operations'], list)):
                raise ValueError('invalid observation')
        except (ValueError, KeyError, TypeError):
            stop('缺少与本次执行绑定的原生诊断，不能安全继续修复')
        if self.pending is not None:
            previous = self.pending['failed_operation_id']
            same_target = (self.pending['family'], self.pending['target']) == (current['family'], current['target'])
            if previous != current['failed_operation_id'] and not same_target:
                boundary = self.pending.get('progress_operation_id', previous)
                if boundary in current['completed_operations']:
                    self._resolved(previous)
                elif boundary != current['failed_operation_id']:
                    stop('修复后执行回退到此前操作')
                # A different failure at that boundary can follow a successful
                # constraint solve. Continue its diagnosis without claiming the
                # downstream feature passed.
        key = (current['family'], current['target'])
        if current['signature'] in self.signatures.get(key, set()):
            stop('同一对象没有几何或求解进展')
        if self.used >= self.policy['max_attempts']:
            stop('整体原生修复预算已耗尽', 'agent_freecad_repair_budget_exhausted')
        count = self.attempts.get(key, 0)
        if count >= self.policy[current['family'] + '_attempts']:
            stop('当前对象修复预算已耗尽', 'agent_freecad_repair_budget_exhausted')
        self.used += 1
        self.attempts[key] = count + 1
        self.signatures.setdefault(key, set()).add(current['signature'])
        self.pending = current
        return count + 1
