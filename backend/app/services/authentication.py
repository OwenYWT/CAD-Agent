"""Password authentication and throttling shared by CAD and monitoring adapters."""
from collections import OrderedDict
from time import monotonic
from app.config import settings
from app.storage import auth as auth_store

async def can_access_shared_onshape(credential: str | None) -> bool:
    if credential is None:
        return not settings.auth_required and not settings.api_keys
    if credential in settings.api_keys or not credential.startswith("user:"):
        return False
    user = await auth_store.get_user(credential.removeprefix("user:"))
    return bool(user and user.get("is_admin"))


class AttemptWindows(OrderedDict):
    """Active keys in last-write order. Never evict an unexpired penalty."""
    def __init__(self, seconds, capacity=10000):
        super().__init__()
        self.seconds = seconds
        self.capacity = capacity

    def full(self, key, limit, now):
        cutoff = now - self.seconds
        while self and next(iter(self.values()))[-1] <= cutoff:
            self.popitem(last=False)
        window = self.get(key)
        if window is None:
            return len(self) >= self.capacity
        return sum(t > cutoff for t in window) >= limit

    def record(self, key, limit, now):
        if not self.full(key, limit, now):
            self[key] = [t for t in self.get(key, ()) if t > now - self.seconds] + [now]
            self.move_to_end(key)


_failed_logins = AttemptWindows(300)
_MAX_FAILED_PER_KEY = 5
_MAX_FAILED_PER_IP = 30


class AuthenticationError(ValueError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail

def check_login_attempts(host: str, phone: str):
    now = monotonic()
    if (_failed_logins.full(f"{host}:{phone}", _MAX_FAILED_PER_KEY, now)
            or _failed_logins.full(f"ip:{host}", _MAX_FAILED_PER_IP, now)):
        raise AuthenticationError(status_code=429, detail="登录失败次数过多，请 5 分钟后再试")


def record_failed_login(host: str, phone: str):
    now = monotonic()
    _failed_logins.record(f"ip:{host}", _MAX_FAILED_PER_IP, now)
    _failed_logins.record(f"{host}:{phone}", _MAX_FAILED_PER_KEY, now)


def clear_failed_login(host: str, phone: str):
    _failed_logins.pop(f"{host}:{phone}", None)


async def create_auth_response(user: dict) -> dict:
    if settings.durable_control_plane_enabled:
        from app.repositories.identity import reconcile_authenticated_user

        await reconcile_authenticated_user(user)
    return {
        "token": await auth_store.create_session_token(user["id"]),
        "user": user,
    }


async def login_with_password(phone: str, password: str, host: str):
    check_login_attempts(host, phone)
    try:
        user = await auth_store.authenticate_password(phone, password)
    except ValueError as exc:
        raise AuthenticationError(status_code=400, detail=str(exc)) from exc
    if not user:
        record_failed_login(host, phone)
        raise AuthenticationError(status_code=400, detail="手机号或密码错误")
    clear_failed_login(host, phone)
    return await create_auth_response(user)


def reset_login_throttle() -> None:
    """Clear the process throttle for isolated test/application lifecycle setup."""
    _failed_logins.clear()
