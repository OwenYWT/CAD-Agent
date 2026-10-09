"""Freeze solver evidence references, then verify bytes before Agent consumption."""
import hashlib
import json
from uuid import UUID
from sqlalchemy import text
from app.object_store import get_object


async def engineering_references(connection, document_id, revision_id):
    rows=(await connection.execute(text('''SELECT DISTINCT ON (t.task_kind,w.request_payload->'engineering_task'->>'component_name')
        a.id,a.sha256,a.size_bytes,t.workflow_run_id,t.source_revision_id,t.source_sha256
        FROM document_engineering_tasks t JOIN workflow_runs w ON w.id=t.workflow_run_id AND w.status='succeeded'
        JOIN artifacts a ON a.workflow_run_id=t.workflow_run_id AND a.revision_id=t.source_revision_id AND a.artifact_kind='engineering_report'
        WHERE t.document_id=:doc AND t.source_revision_id=:revision AND t.task_kind IN ('linear_static','contour_milling','native_measure')
        ORDER BY t.task_kind,w.request_payload->'engineering_task'->>'component_name',t.created_at DESC,t.workflow_run_id DESC LIMIT 4'''),
        {'doc':document_id,'revision':revision_id})).mappings().all()
    return [{'artifact_id':str(r['id']),'sha256':r['sha256'],'size_bytes':r['size_bytes'],
        'workflow_run_id':str(r['workflow_run_id']),'source_revision_id':str(r['source_revision_id']),
        'source_fcstd_sha256':r['source_sha256']} for r in rows]


async def verified_engineering_context(connection, references, document_id, revision_id):
    if len(references)>4:
        raise ValueError('工程参考数量超过上下文预算')
    evidence=[]
    for ref in references:
        row=(await connection.execute(text('''SELECT a.* FROM artifacts a JOIN document_engineering_tasks t ON t.workflow_run_id=a.workflow_run_id
            JOIN workflow_runs w ON w.id=t.workflow_run_id AND w.status='succeeded'
            WHERE a.id=:artifact AND t.document_id=:doc AND t.source_revision_id=:revision AND a.artifact_kind='engineering_report'
            AND a.revision_id=t.source_revision_id AND t.source_sha256=:source_sha'''),
            {'artifact':UUID(ref['artifact_id']),'doc':document_id,'revision':revision_id,'source_sha':ref['source_fcstd_sha256']})).mappings().one_or_none()
        if row is None or row['sha256']!=ref['sha256'] or row['size_bytes']!=ref['size_bytes'] or row['size_bytes']>128*1024 or str(row['workflow_run_id'])!=ref['workflow_run_id'] or str(revision_id)!=ref['source_revision_id']:
            raise ValueError('Agent 工程参考与提交时的不可变来源不一致')
        raw=await get_object(row['object_key'])
        if len(raw)!=ref['size_bytes'] or hashlib.sha256(raw).hexdigest()!=ref['sha256']:
            raise ValueError('Agent 工程参考文件完整性校验失败')
        report=json.loads(raw)
        if report.get('source_fcstd_sha256')!=ref['source_fcstd_sha256']:
            raise ValueError('Agent 工程参考对应不同的原生模型')
        keys=('kind','component_name','material','units','mesh_size_mm','nodes','elements','force_n','reaction_n',
              'force_balance_relative_error','fixed_face','loaded_face','maximum','solver','mesher','scope',
              'tool','postprocessor','work_origin_mm','passes','stepdown_mm','feed_mm_min','plunge_mm_min','spindle_rpm',
              'minimum_target_clearance_mm','radial_allowance_mm','internal_loops_not_machined',
              'measurement','value','unit','method','selectors','native_resolution','status')
        summary={key:report[key] for key in keys if key in report}
        if len(json.dumps(summary,ensure_ascii=False))>8000:
            raise ValueError('Agent 工程参考超过单报告上下文预算')
        evidence.append({'source':ref,'measured_result':summary,
            'applicability':'仅适用于此来源修订、材料、边界和网格；修改几何后必须重新计算，不得将本结果作为新修订的验证。'})
    return evidence
