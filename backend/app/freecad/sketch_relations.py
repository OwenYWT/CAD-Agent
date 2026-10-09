"""Conservative relationship edits shared by host validation and the native runner."""
from copy import deepcopy
import hashlib
import json

try:
    from app.freecad.constraint_relationships import native_args, relation_rows, satisfies, derive_row, serialize_row
except ModuleNotFoundError:
    from freecad_constraint_relationships import native_args, relation_rows, satisfies, derive_row, serialize_row


def relation_certificate(sketch, args, document_objects=None):
    if sketch.get('type_id') != 'Sketcher::SketchObject' or sketch.get('sketch_constraints_sha256') != args['expected_constraints_sha256']:
        raise ValueError('草图约束身份已变化，请重新读取有效基线')
    inspection=sketch.get('inspection') or {}
    geometry=inspection.get('geometry') or {}
    recorded=inspection.get('constraints') or {}
    if (geometry.get('total') != len(geometry.get('items',[])) or recorded.get('total') != len(recorded.get('items',[]))
            or not geometry.get('items') or sketch.get('external_geometry')):
        raise ValueError('需要完整原生几何和约束；外部引用保持保护')
    basis=geometry['items']
    if any(change['action']!='add' for change in args['changes']):
        # Removing a constraint renumbers all later native indexes. Even an
        # expression on another feature can refer to those indexes or names.
        for obj in document_objects if document_objects is not None else [sketch]:
            expressions=obj.get('expressions')
            if not isinstance(expressions,dict) or expressions.get('total')!=0:
                raise ValueError('表达式记录不完整或文档含表达式引用，删除与替换继续保护')
    originals={c.get('logical_id') or f'protected_{c["index"]}':c for c in recorded['items']}
    rules=[]
    for identity,constraint in originals.items():
        native=native_args(constraint)
        if native is None or relation_rows(native,basis) is None:
            raise ValueError('含不支持的非线性、外部或未知关系，不能安全编辑关系')
        rules.append({'logical_id':identity,'native_index':constraint['index'],
            'args':{**native,'driving':constraint.get('driving',True)}})
    changed=set()
    removed=[]
    for change in args['changes']:
        identity=change['logical_id']
        if identity in changed:
            raise ValueError('同一关系不能在一批修改中重复操作')
        changed.add(identity)
        old=originals.get(identity)
        action=change['action']
        if action=='add' and old is not None:
            raise ValueError('关系 ID 已存在')
        if action!='add':
            if old is None or old.get('origin') not in {'user_relation','typed_operation'} or old.get('logical_id')!=identity:
                raise ValueError('来源未知或原模型关系保持保护；仅可修改本系统明确创建的关系')
            if old['type'] not in {'Horizontal','Vertical','Coincident','Equal'}:
                raise ValueError('尺寸和必要驱动参数保持保护')
            removed.append(next(rule for rule in rules if rule['logical_id']==identity))
            rules=[rule for rule in rules if rule['logical_id']!=identity]
        if action!='delete':
            relation=deepcopy(change['constraint'])
            for reference in ('first','second'):
                if isinstance(relation.get(reference),dict) and 'geometry_index' in relation[reference]:
                    relation[reference].setdefault('point_position',None)
            rows=relation_rows(relation,basis)
            if relation.get('kind') not in {'horizontal','vertical','coincident','equal'} or rows is None or not all(satisfies(row,basis) for row in rows):
                raise ValueError('新关系必须由当前真实几何证明，不能移动几何或增加任意尺寸')
            rules.append({'logical_id':identity,'args':relation})
    equations=[];refs=[]
    for rule in rules:
        if rule['args'].get('driving',True):
            for index,row in enumerate(relation_rows(rule['args'],basis) or []):
                equations.append(row);refs.append({'logical_id':rule['logical_id'],'row':index})
    derivations=[]
    for rule in removed:
        for target in relation_rows(rule['args'],basis) or []:
            weights=derive_row(target,equations)
            if weights is None:
                raise ValueError('修改会丢失必要几何关系，请保留或提供可证明的等价关系')
            derivations.append({'logical_id':rule['logical_id'],'target':serialize_row(target),
                'supports':[{**ref,'coefficient':str(w)} for ref,w in zip(refs,weights) if w]})
    specification={'constraints':rules,'construction_geometry':deepcopy(basis),'derivations':derivations,'corrected':[]}
    return {'schema_version':'native-sketch-relations.v1','sketches':{args['sketch']:specification},
        'source_constraints_sha256':args['expected_constraints_sha256'],
        'changes_hash':hashlib.sha256(json.dumps(args['changes'],sort_keys=True,separators=(',',':')).encode()).hexdigest()}


def verify_relation_receipts(metadata, plan):
    native=metadata.get('result') or {}
    expected=[op for op in plan['operations'] if op['action']=='sketch.patch_relations']
    records=[row for row in native.get('validations',[]) if row.get('gate')=='sketch_relationships']
    if native.get('status')!='succeeded' or len(records)!=len(expected):
        raise ValueError('原生关系验证证据缺失')
    reports=[]
    for op in expected:
        row=next((item for item in records if item.get('op_id')==op['op_id']),None)
        if row is None:
            raise ValueError('原生关系验证缺少操作身份')
        report=json.loads(row.get('evidence_json') or '{}')
        args=op['args']
        identity={'sketch':args['sketch'],'source_constraints_sha256':args['expected_constraints_sha256'],
            'changes_hash':hashlib.sha256(json.dumps(args['changes'],sort_keys=True,separators=(',',':')).encode()).hexdigest()}
        probes=report.get('parameter_probes',[])
        if (row.get('status')!='passed' or report.get('status')!='passed'
            or report.get('schema_version')!='constraint-repair-validation.v1'
            or report.get('input_identity')!=identity or row.get('contract_hash')!=report.get('contract_hash')
            or row.get('parameter_probes')!=len(probes)
            or {item.get('sketch') for item in report.get('sketches',[])}!={args['sketch']}
            or any(item.get('degrees_of_freedom')!=0 for item in report['sketches'])
            or any(item.get('status')!='passed' or item.get('downstream_recomputed') is not True for item in probes)):
            raise ValueError('原生求解、参数联动或来源身份验证未通过')
        reports.append(report)
    return reports
