"""Three-way semantic evidence from immutable revision checkpoints."""
from collections import deque

from app.services.document_rebase import ParameterRebaseConflict, prove_independent_parameter_edit


def common_ancestor(nodes, left, right):
    def distances(start):
        result = {}; queue = deque([(start, 0)])
        while queue:
            revision, distance = queue.popleft()
            if revision in result:
                continue
            if revision not in nodes:
                raise ValueError('分支历史不完整，无法确认共同修订')
            result[revision] = distance
            node = nodes[revision]
            for parent in set(filter(None, (node['parent_revision_id'], node.get('fork_source'), node.get('merge_source')))):
                queue.append((parent, distance + 1))
        return result
    a, b = distances(left), distances(right)
    shared = set(a) & set(b)
    if not shared:
        raise ValueError('分支没有可确认的共同修订')
    # Criss-cross histories need a virtual merge base; do not silently pick one.
    score = lambda revision: (max(a[revision], b[revision]), a[revision] + b[revision])
    best = min(map(score, shared))
    candidates = [r for r in shared if score(r) == best]
    if len(candidates) != 1:
        raise ValueError('存在多个同等共同修订，当前无法自动确定合并基准')
    return candidates[0]


def semantic_changes(before, after):
    old = {f['id']: f for f in before['features'] if f['kernel_name'] != 'CADAgentLedger'}
    new = {f['id']: f for f in after['features'] if f['kernel_name'] != 'CADAgentLedger'}
    changes = []
    for key in sorted(old.keys() | new.keys()):
        left, right = old.get(key), new.get(key)
        feature = right or left
        if left is None or right is None:
            changes.append({'feature_id':key, 'kernel_name':feature['kernel_name'], 'label':feature['label'],
                'kind':'added' if left is None else 'removed', 'fields':[]})
            continue
        fields = []
        for name in ('type','dependencies','parameters','geometry_sha256','global_placement','sketch_constraints_sha256','instance','role','intent'):
            if left.get(name) != right.get(name):
                fields.append({'name':name, 'before':left.get(name), 'after':right.get(name)})
        if fields:
            changes.append({'feature_id':key, 'kernel_name':feature['kernel_name'], 'label':feature['label'],
                'kind':'changed', 'fields':fields})
    return changes


def parameter_merge_proposal(nodes, common, source_revision, before, source, target):
    """Require a typed parameter-only source path; geometry similarity is insufficient."""
    touched = set(); seen = set(); current = source_revision
    while current != common:
        if current in seen or current not in nodes:
            raise ParameterRebaseConflict('来源修订路径无法确认')
        seen.add(current); node = nodes[current]
        if node.get('fork_source'):
            current = node['fork_source']; continue
        payload = node.get('arguments') or {}
        restore = payload.get('revision_restore') or {}
        if node.get('fork_workflow') and node.get('source_workflow_run_id') == node['fork_workflow'] and (
            (payload.get('operation_context') or {}).get('rule') == 'explicit_branch_fork'
            and restore.get('source_revision_id') == str(node.get('branch_source_revision'))
        ):
            current = node['parent_revision_id']; continue
        modification = payload.get('structured_modification') or {}
        updates = modification.get('parameter_updates') or []
        if not updates or modification.get('native_edits') or node.get('action') != 'parameters.update':
            raise ParameterRebaseConflict('来源包含非参数操作，无法证明可自动合并；可审阅后采用来源分支几何')
        touched.update(update['parameter_id'] for update in updates)
        current = node['parent_revision_id']
    values = lambda state: {p['id']:p for f in state['features'] for p in f['parameters']}
    a, b, c = values(before), values(source), values(target)
    if a.keys() != b.keys():
        raise ParameterRebaseConflict('来源参数结构发生变化')
    updates = []
    for key in sorted(a):
        if a[key]['value'] == b[key]['value']:
            continue
        if key not in touched or not b[key]['editable']:
            raise ParameterRebaseConflict('参数变化缺少可执行操作证据')
        if key not in c:
            raise ParameterRebaseConflict(f'目标已删除参数 {key}')
        if b[key]['value'] != c[key]['value']:
            updates.append({'parameter_id':key, 'value':b[key]['value']})
    if not updates:
        return {'updates':[], 'proof':None, 'reason':'来源没有尚未应用的参数变更'}
    proof = prove_independent_parameter_edit(before, target, updates)
    return {'updates':updates, 'proof':proof, 'reason':None}
