import logging
import time
from collections import defaultdict

from fastapi import Request, HTTPException, Depends, Query, WebSocket
from fastapi.security import APIKeyHeader, HTTPBearer, HTTPAuthorizationCredentials

from app.config import settings

logger = logging.getLogger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(
    request: Request,
    bearer: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    x_api_key: str | None = Depends(_api_key_header),
):
    """Verify API credentials. Skip auth when api_keys list is empty (dev mode)."""
    if not settings.api_keys:
        return None

    if bearer and bearer.credentials in settings.api_keys:
        return bearer.credentials

    if x_api_key and x_api_key in settings.api_keys:
        logger.warning("X-API-Key header is deprecated. Use 'Authorization: Bearer <key>' instead.")
        return x_api_key

    raise HTTPException(status_code=401, detail="Invalid or missing API key")


def verify_ws_token(token: str | None) -> bool:
    """Verify WebSocket token. Returns True if valid or if auth is disabled."""
    if not settings.api_keys:
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
            return f"key:{api_key}"
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
