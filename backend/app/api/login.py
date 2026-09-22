from app.models.authentication import LoginWithPasswordRequest
from app.services import authentication

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator

from app.config import settings
from app.storage import auth as auth_store
from app.storage.auth import VerificationThrottleError
from app.api.auth import get_current_user
from app.services.sms import SmsDeliveryError, send_verification_code_sms

router = APIRouter(prefix="/api/auth", tags=["auth"])
_bearer_scheme = HTTPBearer(auto_error=False)


class CodeRequest(BaseModel):
    phone: str = Field(..., min_length=6, max_length=20)
    purpose: str = Field(..., pattern="^(register|login|reset_password)$")


class RegisterWithCodeRequest(BaseModel):
    phone: str = Field(..., min_length=6, max_length=20)
    code: str = Field(..., min_length=4, max_length=12)
    password: str = Field(..., min_length=8, max_length=128)


class RegisterWithInviteRequest(BaseModel):
    phone: str = Field(..., min_length=6, max_length=20)
    invite_code: str = Field(..., min_length=3, max_length=120)
    password: str = Field(..., min_length=8, max_length=128)


class LoginWithCodeRequest(BaseModel):
    phone: str = Field(..., min_length=6, max_length=20)
    code: str = Field(..., min_length=4, max_length=12)


class ResetPasswordRequest(BaseModel):
    phone: str = Field(..., min_length=6, max_length=20)
    code: str = Field(..., min_length=4, max_length=12)
    password: str = Field(..., min_length=8, max_length=128)


class InviteCreateRequest(BaseModel):
    code: str | None = Field(default=None, min_length=3, max_length=120)
    max_uses: int = Field(default=1, ge=1, le=10000)
    expires_at: str | None = None

    @field_validator("expires_at")
    @classmethod
    def _validate_expires_at(cls, v: str | None) -> str | None:
        """Reject malformed timestamps at the edge (422) and normalize to a
        tz-aware ISO string, so the storage layer never stores a value that would
        later crash the naive-vs-aware comparison in consume_invite_code."""
        if v is None or v == "":
            return None
        try:
            dt = datetime.fromisoformat(v)
        except ValueError as exc:
            raise ValueError("expires_at 必须是 ISO 8601 时间格式") from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()


@router.get("/config")
async def auth_config():
    api_key_required = bool(settings.api_keys)
    return {
        "auth_required": settings.auth_required,
        "api_key_required": api_key_required,
        "auth_disabled": not settings.auth_required and not api_key_required,
    }


def _client_host(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _check_login_attempts(request: Request, phone: str):
    try:
        return authentication.check_login_attempts(_client_host(request), phone)
    except authentication.AuthenticationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _record_failed_login(request: Request, phone: str):
    try:
        return authentication.record_failed_login(_client_host(request), phone)
    except authentication.AuthenticationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _clear_failed_login(request: Request, phone: str):
    try:
        return authentication.clear_failed_login(_client_host(request), phone)
    except authentication.AuthenticationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


async def _auth_response(user: dict) -> dict:
    return await authentication.create_auth_response(user)


def require_admin(user=Depends(get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Only admin can manage invite codes")
    return user


@router.post("/code/request")
async def request_code(req: CodeRequest):
    if not settings.auth_code_flows_enabled:
        raise HTTPException(status_code=403, detail="验证码登录/注册暂未开放，请使用邀请码注册或密码登录")
    try:
        code = await auth_store.issue_verification_code(req.phone, req.purpose)
    except VerificationThrottleError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if settings.auth_dev_expose_code:
        return {
            "message": "验证码已生成（开发模式直接返回，生产环境请关闭 AUTH_DEV_EXPOSE_CODE）。",
            "expires_in_minutes": settings.verification_code_ttl_minutes,
            "dev_code": code,
        }

    try:
        delivery = await send_verification_code_sms(req.phone, code, req.purpose)
    except SmsDeliveryError as exc:
        await auth_store.delete_verification_code(req.phone, req.purpose, code)
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {
        "message": "验证码已通过短信发送，请查收。",
        "expires_in_minutes": settings.verification_code_ttl_minutes,
        "delivery_provider": delivery.provider,
    }


@router.post("/register/code")
async def register_with_code(req: RegisterWithCodeRequest):
    if not settings.auth_code_flows_enabled:
        raise HTTPException(status_code=403, detail="验证码注册暂未开放，请使用邀请码注册")
    try:
        if not await auth_store.consume_verification_code(req.phone, "register", req.code):
            raise HTTPException(status_code=400, detail="验证码无效或已过期")
        user = await auth_store.create_user(req.phone, req.password, "verification_code")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await _auth_response(user)


@router.post("/register/invite")
async def register_with_invite(req: RegisterWithInviteRequest):
    try:
        if not await auth_store.consume_invite_code(req.invite_code):
            raise HTTPException(status_code=400, detail="邀请码无效、已过期或已用完")
        user = await auth_store.create_user(req.phone, req.password, "invite_code")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await _auth_response(user)


@router.post("/login/password")
async def login_with_password(req: LoginWithPasswordRequest, request: Request):
    try:
        return await authentication.login_with_password(req.phone, req.password, _client_host(request))
    except authentication.AuthenticationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/login/code")
async def login_with_code(req: LoginWithCodeRequest, request: Request):
    if not settings.auth_code_flows_enabled:
        raise HTTPException(status_code=403, detail="验证码登录暂未开放，请使用密码登录")
    _check_login_attempts(request, req.phone)
    try:
        if not await auth_store.consume_verification_code(req.phone, "login", req.code):
            _record_failed_login(request, req.phone)
            raise HTTPException(status_code=400, detail="验证码无效或已过期")
        user = await auth_store.get_user_by_phone(req.phone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not user:
        _record_failed_login(request, req.phone)
        raise HTTPException(status_code=400, detail="该手机号尚未注册，请先注册")
    await auth_store.touch_login(user["id"])
    user = await auth_store.get_user(user["id"])
    _clear_failed_login(request, req.phone)
    return await _auth_response(user)


@router.post("/password/reset")
async def reset_password(req: ResetPasswordRequest):
    if not settings.auth_code_flows_enabled:
        raise HTTPException(status_code=403, detail="验证码重置密码暂未开放，请联系管理员")
    try:
        if not await auth_store.consume_verification_code(req.phone, "reset_password", req.code):
            raise HTTPException(status_code=400, detail="验证码无效或已过期")
        user = await auth_store.reset_password(req.phone, req.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await _auth_response(user)


@router.post("/refresh")
async def refresh_token(bearer: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme)):
    token = bearer.credentials if bearer else None
    new_token = await auth_store.refresh_session_token(token)
    if not new_token:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user_id = await auth_store.verify_session_token(new_token)
    user = await auth_store.get_user(user_id) if user_id else None
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return {"token": new_token, "user": user}


@router.post("/logout")
async def logout(bearer: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme)):
    token = bearer.credentials if bearer else None
    await auth_store.revoke_session_token(token)
    return {"ok": True}


@router.get("/me")
async def me(user=Depends(get_current_user)):
    return {"user": user}


@router.get("/invites")
async def list_invites(_user=Depends(require_admin)):
    return await auth_store.list_invite_codes()


@router.post("/invites")
async def create_invite(req: InviteCreateRequest, _user=Depends(require_admin)):
    try:
        return await auth_store.create_invite_code(req.code, req.max_uses, req.expires_at)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/invites/{code}")
async def disable_invite(code: str, _user=Depends(require_admin)):
    await auth_store.disable_invite_code(code)
    return {"ok": True}


@router.delete("/account")
async def delete_account(user=Depends(get_current_user), bearer: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme)):
    try:
        await auth_store.delete_user(user["id"])
        token = bearer.credentials if bearer else None
        await auth_store.revoke_session_token(token)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}
