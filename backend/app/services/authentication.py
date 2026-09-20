"""Password authentication and throttling shared by CAD and monitoring adapters."""
from collections import defaultdict
from time import time
from app.config import settings
from app.storage import auth as auth_store

_FAILED_DESCRIPTION = "Shared per-process login throttle; never a CAD task state."
_failed_logins: dict[str, list[float]] = defaultdict(list)
_LOGIN_WINDOW_S = 300
_MAX_FAILED_PER_KEY = 5
_MAX_FAILED_PER_IP = 30

class AuthenticationError(ValueError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail

def _recent(key: str, now: float) -> list[float]:
    window = [item for item in _failed_logins[key] if item > now - _LOGIN_WINDOW_S]
    _failed_logins[key] = window
    return window


def _check_login_attempts(host: str, phone: str):
    now = time()
    per_key = _recent(f"{host}:{phone}", now)
    per_ip = _recent(f"ip:{host}", now)
    if len(per_key) >= _MAX_FAILED_PER_KEY or len(per_ip) >= _MAX_FAILED_PER_IP:
        raise AuthenticationError(status_code=429, detail="登录失败次数过多，请 5 分钟后再试")


def _record_failed_login(host: str, phone: str):
    now = time()
    _failed_logins[f"{host}:{phone}"].append(now)
    _failed_logins[f"ip:{host}"].append(now)


def _clear_failed_login(host: str, phone: str):
    _failed_logins.pop(f"{host}:{phone}", None)


async def _auth_response(user: dict) -> dict:
    if settings.durable_control_plane_enabled:
        from app.repositories.identity import reconcile_authenticated_user

        await reconcile_authenticated_user(user)
    return {
        "token": await auth_store.create_session_token(user["id"]),
        "user": user,
    }


async def login_with_password(phone: str, password: str, host: str):
    _check_login_attempts(host, phone)
    try:
        user = await auth_store.authenticate_password(phone, password)
    except ValueError as exc:
        raise AuthenticationError(status_code=400, detail=str(exc)) from exc
    if not user:
        _record_failed_login(host, phone)
        raise AuthenticationError(status_code=400, detail="手机号或密码错误")
    _clear_failed_login(host, phone)
    return await _auth_response(user)
