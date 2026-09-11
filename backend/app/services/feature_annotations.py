"""User-authored semantics are versioned separately from measured kernel state."""
import json
from uuid import UUID

from sqlalchemy import text

from app.db import tenant_transaction
from app.domain.projects import Permission


async def annotation_context(connection, document_id, *, through_sequence=None):
    rows=(await connection.execute(text("""SELECT DISTINCT ON(feature_id)
        feature_id,kernel_name,version,role,intent,revision_id,principal_id
        FROM feature_annotations WHERE document_id=:doc
          AND (CAST(:seq AS bigint) IS NULL OR event_sequence<=:seq)
        ORDER BY feature_id,version DESC"""),
        {"doc":document_id,"seq":through_sequence})).mappings()
    return [{k:str(v) if isinstance(v,UUID) else v for k,v in row.items()} for row in rows]


def annotate_projection(projection, annotations):
    by_id={a['feature_id']:a for a in annotations}
    return {**projection,"features":[{**f,**({
        "role":by_id[f['id']]['role'],"intent":by_id[f['id']]['intent'],
        "annotation_version":by_id[f['id']]['version'],"annotation_source":"user",
    } if f['id'] in by_id else {"annotation_version":0})} for f in projection['features']]}


async def save_annotation(context,document_id,feature_id,*,revision_id,expected_version,
                          annotation_id,role,intent):
    from app.services.cloud_documents import authorized_document,checkpoint,DocumentConflict
    projection=await checkpoint(context,document_id,revision_id)
    feature=next((f for f in projection['features'] if f['id']==str(feature_id)),None)
    if feature is None:
        raise ValueError("特征不属于指定文档版本")
    async with tenant_transaction(context.tenant_id,context.principal_id) as conn:
        doc=await authorized_document(conn,context,document_id,Permission.MODIFY_DESIGN,lock=True)
        existing=(await conn.execute(text("SELECT * FROM feature_annotations WHERE id=:id"),
                                      {"id":annotation_id})).mappings().one_or_none()
        if existing:
            if (existing['document_id'],existing['feature_id'],existing['revision_id'],
                existing['principal_id'],existing['version'],existing['role'],existing['intent']) != (
                document_id,feature_id,revision_id,context.principal_id,expected_version+1,role,intent):
                raise DocumentConflict("标注 ID 已用于其他内容")
            return {"version":existing['version'],"replayed":True}
        version=await conn.scalar(text("SELECT COALESCE(max(version),0) FROM feature_annotations WHERE document_id=:doc AND feature_id=:feature"),
                                  {"doc":document_id,"feature":feature_id})
        if doc['head_revision_id']!=revision_id or version!=expected_version:
            raise DocumentConflict("模型或特征标注已更新，请重新读取后保存")
        seq=await conn.scalar(text("UPDATE cloud_documents SET event_sequence=event_sequence+1 WHERE id=:doc RETURNING event_sequence"),{"doc":document_id})
        await conn.execute(text("""INSERT INTO feature_annotations(id,tenant_id,document_id,feature_id,kernel_name,
            revision_id,principal_id,version,event_sequence,role,intent)
            VALUES(:id,:tenant,:doc,:feature,:name,:revision,:principal,:version,:seq,:role,:intent)"""),
            {"id":annotation_id,"tenant":context.tenant_id,"doc":document_id,"feature":feature_id,
             "name":feature['kernel_name'],"revision":revision_id,"principal":context.principal_id,
             "version":version+1,"seq":seq,"role":role,"intent":intent})
        payload={"feature_id":str(feature_id),"role":role,"intent":intent,
                 "annotation_version":version+1,"annotation_source":"user"}
        await conn.execute(text("""INSERT INTO document_events(tenant_id,document_id,sequence,event_type,payload)
            VALUES(:tenant,:doc,:seq,'feature.annotated',CAST(:payload AS jsonb))"""),
            {"tenant":context.tenant_id,"doc":document_id,"seq":seq,"payload":json.dumps(payload)})
    return {"version":version+1,"replayed":False}
