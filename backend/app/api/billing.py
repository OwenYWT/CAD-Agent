"""Authenticated customer account reads and explicit platform-admin adjustments."""
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.api.auth import get_current_user, get_durable_principal
from app.db import tenant_transaction
from app.domain.identity import user_principal
from app.services import customer_billing as billing


router = APIRouter(prefix="/api/billing", tags=["billing"])


def error(exc):
    if isinstance(exc, PermissionError):
        return HTTPException(403, str(exc))
    return HTTPException(409, {"code": exc.code, "message": str(exc)})


async def require_admin(user=Depends(get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "只有平台管理员可以配置套餐和调整余额")
    return user


class BillingInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PlanVersionRequest(BillingInput):
    code: Literal["basic", "pro", "team"]
    version: int = Field(strict=True, ge=2, le=1000000)
    name: str = Field(min_length=1, max_length=80)
    published: bool
    monthly_price_minor: int | None = Field(default=None, strict=True, ge=0, le=10**12)
    currency: Literal["CNY"] = "CNY"
    monthly_provider_requests: int | None = Field(default=None, strict=True, ge=1, le=100000000)

    @model_validator(mode="after")
    def published_terms_required(self):
        if self.published and (self.monthly_price_minor is None or self.monthly_provider_requests is None):
            raise ValueError("发布套餐前必须明确价格和月调用额度")
        return self


class SubscriptionRequest(BillingInput):
    plan_version_id: UUID
    expected_revision: int = Field(strict=True, ge=0)
    idempotency_key: str = Field(min_length=1, max_length=200)
    expires_at: datetime | None = None
    status: Literal["active", "suspended"] = "active"

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value):
        if value is not None:
            if value.tzinfo is None:
                raise ValueError("到期时间必须包含时区")
            return value.astimezone(timezone.utc)
        return value


class WalletAdjustmentRequest(BillingInput):
    amount_minor: int = Field(strict=True, ge=-(10**12), le=10**12)
    idempotency_key: str = Field(min_length=1, max_length=200)
    reference: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)


@router.get("/me")
async def account(principal=Depends(get_durable_principal)):
    try:
        async with tenant_transaction(principal.tenant_id, principal.principal_id) as connection:
            return await billing.account_summary(connection, principal.tenant_id, principal.principal_id)
    except (billing.BillingError, PermissionError) as exc:
        raise error(exc) from exc


@router.get("/entries")
async def entries(principal=Depends(get_durable_principal)):
    try:
        async with tenant_transaction(principal.tenant_id, principal.principal_id) as connection:
            return await billing.wallet_entries(connection, principal.tenant_id, principal.principal_id)
    except (billing.BillingError, PermissionError) as exc:
        raise error(exc) from exc


@router.get("/plans")
async def plans(principal=Depends(get_durable_principal)):
    async with tenant_transaction(principal.tenant_id, principal.principal_id) as connection:
        return await billing.plans(connection)


@router.get("/admin/plans")
async def admin_plans(user=Depends(require_admin)):
    principal = user_principal(user["id"])
    async with tenant_transaction(principal.tenant_id, principal.principal_id, role="billing_admin") as connection:
        return await billing.plans(connection, include_drafts=True)


@router.post("/admin/plans", status_code=201)
async def create_plan(body: PlanVersionRequest, user=Depends(require_admin)):
    principal = user_principal(user["id"])
    try:
        async with tenant_transaction(principal.tenant_id, principal.principal_id, role="billing_admin") as connection:
            return await billing.create_plan_version(
                connection, actor_subject=principal.external_subject, values=body.model_dump(),
            )
    except billing.BillingError as exc:
        raise error(exc) from exc


@router.put("/admin/tenants/{tenant_id}/subscription")
async def assign_subscription(tenant_id: UUID, body: SubscriptionRequest, user=Depends(require_admin)):
    try:
        async with tenant_transaction(tenant_id, role="billing_admin") as connection:
            return await billing.assign_subscription(
                connection, tenant_id=tenant_id, actor_subject="user:" + user["id"], **body.model_dump(),
            )
    except billing.BillingError as exc:
        raise error(exc) from exc


@router.post("/admin/tenants/{tenant_id}/adjustments")
async def adjust_wallet(tenant_id: UUID, body: WalletAdjustmentRequest, user=Depends(require_admin)):
    try:
        async with tenant_transaction(tenant_id, role="billing_admin") as connection:
            return await billing.adjust_wallet(
                connection, tenant_id=tenant_id, actor_subject="user:" + user["id"], **body.model_dump(),
            )
    except billing.BillingError as exc:
        raise error(exc) from exc
