"""Prove the real provider received the frozen, verified engineering reference."""
import asyncio
import json
import sys
from uuid import UUID
from sqlalchemy import text
from app.db import tenant_transaction,close_database
from app.domain.identity import user_principal
from app.workflows.temporal import get_temporal_client,temporal_agent_v2_workflow_id


async def main():
    owner=user_principal(sys.argv[1]);workflow_id=UUID(sys.argv[2]);analysis_id=sys.argv[3]
    async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
        refs=await conn.scalar(text("SELECT arguments->'_engineering_evidence' FROM cad_operations WHERE id=:id"),{'id':workflow_id})
        assert refs and any(r['workflow_run_id']==analysis_id for r in refs)
    client=await get_temporal_client()
    history=await client.get_workflow_handle(temporal_agent_v2_workflow_id(workflow_id)).fetch_history()
    witnessed=[]
    for event in history.events:
        if not event.HasField('activity_task_completed_event_attributes'): continue
        decoded=await client.data_converter.decode(event.activity_task_completed_event_attributes.result.payloads)
        for result in decoded:
            if not isinstance(result,dict): continue
            provenance=result.get('engineering_context') or {}
            references=provenance.get('references') or []
            if any(r['workflow_run_id']==analysis_id for r in references):
                assert all(r in refs for r in references)
                assert provenance.get('request_hash') and provenance.get('response_hash')
                assert provenance.get('provider') not in {None,'deterministic','mock'}
                assert provenance.get('context_sha256')
                witnessed.append({'provider':provenance['provider'],'model':provenance['model'],
                    'engineering_references':references,'context_sha256':provenance['context_sha256']})
    assert witnessed,'No real provider completion used the persisted analysis reference'
    print('CAD_ENGINEERING_AGENT_PROVENANCE='+json.dumps(witnessed),flush=True)
    await close_database()


asyncio.run(main())
