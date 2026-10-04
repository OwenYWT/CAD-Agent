"""Executed inside the shipping monitor image with its private monitor.env."""
import asyncio
import json
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from app.db import close_database
from app.services.monitor_queries import reader, monitor_engine, close_monitor_database


async def main():
    async with reader() as conn:
        current,session=(await conn.execute(text('SELECT current_user::text,session_user::text'))).one()
        flags=(await conn.execute(text("SELECT rolsuper,rolbypassrls,pg_has_role(session_user,'cad_agent_runtime','MEMBER'),pg_has_role(session_user,'cad_agent_auth','MEMBER') FROM pg_roles WHERE rolname=session_user"))).one()
        assert current == 'cad_agent_monitor' and session == 'cad_ci_monitor' and not any(flags)
    denied=[]
    for name,sql in [('write',"UPDATE llm_calls SET status='failed' WHERE false"),
                     ('auth',"SELECT password_hash FROM auth_users LIMIT 0"),
                     ('runtime-role',"SET ROLE cad_agent_runtime")]:
        try:
            async with monitor_engine().begin() as conn:
                await conn.execute(text(sql))
                raise AssertionError('monitor operation was allowed: '+name)
        except DBAPIError as error:
            assert getattr(error.orig,'sqlstate',None) in ('42501','25006'),name
            denied.append(name)
    print(json.dumps({'passed':True,'current_user':current,'session_user':session,
        'superuser':False,'bypass_rls':False,'auth_or_runtime_member':False,'denied_operations':denied}))
    await close_monitor_database();await close_database()


asyncio.run(main())
