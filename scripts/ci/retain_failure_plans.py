"""Keep failed workflow plans before deleting this job's disposable services."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import text
from app.config import settings
from app.db import get_database_engine, close_database


async def main():
    target=urlsplit(settings.database_url)
    if os.getenv('APP_ENVIRONMENT') != 'test' or not os.environ.get('CAD_CI_SCOPE','').startswith('cad-ci-') or target.hostname not in ('127.0.0.1','localhost') or target.path != '/cad_ci':
        raise ValueError('failure retention requires this job’s isolated cad_ci database')
    out=Path(os.environ['CAD_CI_REPORT_ROOT'])/'failed-plans'
    out.mkdir(parents=True,exist_ok=True)
    async with get_database_engine().connect() as conn:
        rows=(await conn.execute(text("SELECT s.id,s.workflow_run_id,s.source_hash,s.source_code FROM agent_generated_sources s JOIN workflow_runs w ON w.id=s.workflow_run_id WHERE w.status IN ('failed','cancelled','timed_out') ORDER BY s.created_at LIMIT 129"))).mappings().all()
    if len(rows) > 128:
        raise ValueError('failure evidence exceeds the 128-source retention budget')
    budget=64*1024*1024
    for row in rows:
        value=json.dumps(dict(row),default=str,ensure_ascii=False).encode()
        budget -= len(value)
        if budget < 0:
            raise ValueError('failure evidence exceeds the 64 MiB retention budget')
        (out/(str(row['workflow_run_id'])+'-'+str(row['id'])+'.json')).write_bytes(value)
    (out/'manifest.json').write_text(json.dumps({'scope':os.environ['CAD_CI_SCOPE'],'failed_source_count':len(rows),
        'sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in out.glob('*.json')}},indent=2))
    await close_database()


if __name__=='__main__':asyncio.run(main())
