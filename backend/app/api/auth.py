import hashlib
import logging
import time
from collections import defaultdict

from fastapi import Request, HTTPException, Depends, WebSocket
from fastapi.security import APIKeyHeader, HTTPBearer, HTTPAuthorizationCredentials

from app.config import settings
from app.storage import auth as auth_store

logger = logging.getLogger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def _bind_authenticated_user(request: Request, user: dict) -> None:
    if not settings.durable_control_plane_enabled:
        return
    from app.repositories.identity import reconcile_authenticated_user

    request.state.principal_context = await reconcile_authenticated_user(user)


async def _bind_api_key(request: Request, api_key: str) -> None:
    if not settings.durable_control_plane_enabled:
        return
    from app.repositories.identity import reconcile_api_key

    request.state.principal_context = await reconcile_api_key(api_key)


async def _bind_local_anonymous(request: Request) -> None:
    if not settings.durable_control_plane_enabled:
        return
    from app.repositories.identity import reconcile_local_anonymous

    request.state.principal_context = await reconcile_local_anonymous()


async def get_current_user(
    request: Request,
    bearer: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
):
    token = bearer.credentials if bearer else None
    user_id = await auth_store.verify_session_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or missing login token")
    user = await auth_store.get_user(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    await _bind_authenticated_user(request, user)
    return user


async def get_optional_user(
    request: Request,
    bearer: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
):
    """Like get_current_user, but tolerates the local-dev auth-off mode.

    - A valid login token always resolves to that user (per-user data isolation).
    - With no/invalid token AND auth disabled (auth_required False, no api_keys),
      returns None (anonymous) so dev mode keeps working without a login.
    - With no/invalid token while auth is enabled, raises 401.
    """
    token = bearer.credentials if bearer else None
    if token:
        user_id = await auth_store.verify_session_token(token)
        if user_id:
            user = await auth_store.get_user(user_id)
            if user:
                await _bind_authenticated_user(request, user)
                return user
    if not settings.auth_required and not settings.api_keys:
        await _bind_local_anonymous(request)
        return None
    raise HTTPException(status_code=401, detail="Invalid or missing login token")


async def verify_api_key(
    request: Request,
    bearer: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    x_api_key: str | None = Depends(_api_key_header),
):
    """Verify API credentials.

    Accepts a login session Bearer token or a legacy static API key. Auth is only
    skipped (returns None) when BOTH auth_required is False AND no api_keys are
    configured — i.e. an explicit local-dev opt-out. With the default
    auth_required=True, a valid login token (or legacy key) is mandatory.
    """
    if bearer:
        user_id = await auth_store.verify_session_token(bearer.credentials)
        if user_id:
            user = await auth_store.get_user(user_id)
            if not user:
                raise HTTPException(status_code=401, detail="User not found")
            await _bind_authenticated_user(request, user)
            return f"user:{user_id}"
        if bearer.credentials in settings.api_keys:
            await _bind_api_key(request, bearer.credentials)
            return bearer.credentials

    if x_api_key and x_api_key in settings.api_keys:
        logger.warning("X-API-Key header is deprecated. Use 'Authorization: Bearer <key>' instead.")
        await _bind_api_key(request, x_api_key)
        return x_api_key

    if not settings.auth_required and not settings.api_keys:
        await _bind_local_anonymous(request)
        return None

    raise HTTPException(status_code=401, detail="Invalid or missing API key")


async def get_ws_user_id(token: str | None) -> str | None:
    if token:
        return await auth_store.verify_session_token(token)
    return None


async def verify_ws_token(token: str | None) -> bool:
    """Verify WebSocket token. Returns True if valid or if auth is disabled."""
    if token and await auth_store.verify_session_token(token):
        return True
    if not settings.auth_required and not settings.api_keys:
        return True
    return token is not None and token in settings.api_keys


class RateLimiter:
    """Simple in-memory sliding-window rate limiter per API key / IP."""

    _MAX_KEYS = 10000  # prevent unbounded growth

    def __init__(self, rpm: int | None = None):
        self.rpm = rpm or settings.rate_limit_per_minute
        self._windows: dict[str, list[float]] = defaultdict(list)
        self._last_cleanup = time.time()

    def _client_key(self, request: Request | WebSocket, api_key: str | None) -> str:
        if api_key:
            fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
            return f"key-sha256:{fingerprint}"
        # Behind a trusted proxy, the real client IP is the first X-Forwarded-For hop;
        # otherwise request.client.host is the proxy itself (one shared bucket for all).
        if settings.trust_proxy_headers:
            xff = request.headers.get("x-forwarded-for")
            if xff:
                real_ip = xff.split(",")[0].strip()
                if real_ip:
                    return f"ip:{real_ip}"
        client = getattr(request, "client", None)
        return f"ip:{client.host}" if client else "ip:unknown"

    def _cleanup_stale_keys(self):
        """Remove keys with no recent activity to bound memory."""
        now = time.time()
        if now - self._last_cleanup < 60:
            return
        self._last_cleanup = now
        cutoff = now - 120
        stale = [k for k, v in self._windows.items() if not v or v[-1] < cutoff]
        for k in stale:
            del self._windows[k]

    async def check(self, request: Request | WebSocket, api_key: str | None = None):
        if self.rpm <= 0:
            return
        self._cleanup_stale_keys()
        key = self._client_key(request, api_key)
        now = time.time()
        window = self._windows[key]
        cutoff = now - 60
        self._windows[key] = [t for t in window if t > cutoff]
        if len(self._windows[key]) >= self.rpm:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded ({self.rpm} requests/minute)",
            )
        self._windows[key].append(now)


rate_limiter = RateLimiter()
