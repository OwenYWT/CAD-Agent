"""Create an isolated test account; credentials are private runtime files only."""
import asyncio
import json
import os
from pathlib import Path
import secrets
from app.storage import auth
from app.repositories.identity import reconcile_authenticated_user
from app.db import close_database
from app.config import settings

async def main():
    path = Path(os.environ['CAD_NATIVE_E2E_PRIVATE'])
    assert not path.exists(), 'refusing to replace credentials'
    accounts = {}
    for role in ('owner','editor'):
        password = secrets.token_urlsafe(24)
        phone = '19' + str(secrets.randbelow(10**9)).zfill(9)
        user = await auth.create_user(phone, password, 'invite_code')
        await reconcile_authenticated_user(user)
        token = await auth.create_session_token(user['id'])
        accounts[role] = {'phone':phone,'password':password,'user':user,'token':token}
    await auth.ensure_admin_user()
    admin = await auth.authenticate_password('admin',settings.admin_password)
    assert admin and admin['is_admin'], 'isolated CI administrator was not provisioned'
    accounts['admin'] = {'phone':'admin','password':settings.admin_password,'user':admin}
    with path.open('x') as stream:
        path.chmod(0o600)
        json.dump(accounts, stream)
    await close_database()
asyncio.run(main())
