"""Customer plans and balances; provider expenditure remains a separate ledger."""
from datetime import timezone
import json
from uuid import UUID, uuid4

from sqlalchemy import text

SUPPORTED_CUSTOMER_BILLING_CONTRACTS = frozenset({"customer-billing.v1"})


class BillingError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


async def ensure_account(connection, tenant_id: UUID):
    kind = await connection.scalar(
        text("SELECT kind FROM tenants WHERE id=:tenant"), {"tenant": tenant_id},
    )
    if kind not in {"personal", "organization"}:
        return False
    for table in ("tenant_subscriptions", "tenant_wallet_accounts"):
        await connection.execute(text(
            f"INSERT INTO {table}(tenant_id) VALUES(:tenant) ON CONFLICT DO NOTHING"
        ), {"tenant": tenant_id})
    return True


async def _lock_customer(connection, tenant_id):
    # Shared by quota admission and administrator changes. Provider reservations
    # acquire their existing locks first; billing operations never lock them.
    await connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
        {"scope": "customer-billing:" + str(tenant_id)},
    )


async def require_billing_owner(connection, tenant_id, principal_id):
    role = await connection.scalar(text("""SELECT m.role FROM tenant_memberships m
        JOIN principals p ON p.tenant_id=m.tenant_id AND p.id=m.principal_id
        JOIN tenants t ON t.id=m.tenant_id
        WHERE m.tenant_id=:tenant AND m.principal_id=:principal
          AND p.status='active' AND t.status='active'"""),
        {"tenant": tenant_id, "principal": principal_id})
    if role not in {"owner", "admin"}:
        raise PermissionError("只有工作区所有者或管理员可以查看套餐和余额")


async def _subscription(connection, tenant_id):
    row = (await connection.execute(text("""SELECT s.*,p.code,p.version,p.name,
        p.monthly_price_minor,p.currency,p.monthly_provider_requests,p.published
        FROM tenant_subscriptions s JOIN billing_plan_versions p ON p.id=s.plan_version_id
        WHERE s.tenant_id=:tenant"""), {"tenant": tenant_id})).mappings().one_or_none()
    if row is None:
        raise BillingError("customer_account_missing", "未找到客户账户")
    return dict(row)


async def month_usage(connection, tenant_id):
    # Count the existing durable reservations, including retries and unknown
    # dispatches. Changing plans cannot reset usage or turn unknown calls free.
    return int(await connection.scalar(text("""SELECT count(*) FROM provider_budget_reservations
        WHERE tenant_id=:tenant AND created_at>=date_trunc('month',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
          AND created_at<(date_trunc('month',now() AT TIME ZONE 'UTC')+interval '1 month') AT TIME ZONE 'UTC'"""),
        {"tenant": tenant_id}))


async def check_provider_request(connection, tenant_id):
    """Run inside the provider reservation transaction, before a new dispatch."""
    if not await ensure_account(connection, tenant_id):
        return  # Internal service identities retain their existing provider policy.
    await _lock_customer(connection, tenant_id)
    subscription = await _subscription(connection, tenant_id)
    now = await connection.scalar(text("SELECT clock_timestamp()"))
    if subscription["status"] != "active" or (
        subscription["expires_at"] is not None and subscription["expires_at"] <= now
    ):
        raise BillingError("subscription_inactive", "套餐已暂停或到期，请联系管理员；已保存模型仍可查看")
    limit = subscription["monthly_provider_requests"]
    if limit is not None and await month_usage(connection, tenant_id) >= limit:
        raise BillingError("tenant_quota_exhausted", "本月套餐模型调用额度已用完")


async def account_summary(connection, tenant_id, principal_id):
    await require_billing_owner(connection, tenant_id, principal_id)
    if not await ensure_account(connection, tenant_id):
        raise BillingError("customer_account_unavailable", "该身份不属于客户计费账户")
    subscription = await _subscription(connection, tenant_id)
    wallet = (await connection.execute(
        text("SELECT currency,balance_minor FROM tenant_wallet_accounts WHERE tenant_id=:tenant"),
        {"tenant": tenant_id},
    )).mappings().one()
    now = await connection.scalar(text("SELECT clock_timestamp()"))
    effective_status = subscription["status"]
    if subscription["expires_at"] is not None and subscription["expires_at"] <= now:
        effective_status = "expired"
    return {
        "tenant_id": str(tenant_id),
        "plan": {key: subscription[key] for key in (
            "code", "version", "name", "monthly_price_minor", "currency", "monthly_provider_requests",
        )},
        "subscription": {
            "status": effective_status, "expires_at": subscription["expires_at"],
            "revision": subscription["revision"],
        },
        "wallet": dict(wallet),
        "usage": {"period": now.astimezone(timezone.utc).strftime("%Y-%m"),
                  "provider_requests": await month_usage(connection, tenant_id),
                  "unit": "provider_request", "timezone": "UTC"},
        # The application has no payment gateway yet. Do not present an admin
        # adjustment or a stored provider-cost receipt as a successful payment.
        "online_payments_enabled": False,
    }


async def plans(connection, *, include_drafts=False):
    return [dict(row) for row in (await connection.execute(text(
        "SELECT id,code,version,name,published,monthly_price_minor,currency,monthly_provider_requests "
        "FROM billing_plan_versions " + ("" if include_drafts else "WHERE published ") +
        "ORDER BY code,version DESC"
    ))).mappings()]


async def create_plan_version(connection, *, actor_subject, values):
    await connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
        {"scope": f"billing-plan:{values['code']}:{values['version']}"},
    )
    existing = (await connection.execute(text(
        "SELECT * FROM billing_plan_versions WHERE code=:code AND version=:version"
    ), values)).mappings().one_or_none()
    if existing:
        if any(existing[key] != value for key, value in values.items()):
            raise BillingError("billing_idempotency_conflict", "该套餐版本已存在且配置不同，请创建新版本")
        return {"id": str(existing["id"]), **values}
    plan_id = uuid4()
    await connection.execute(text("""INSERT INTO billing_plan_versions
        (id,code,version,name,published,monthly_price_minor,currency,monthly_provider_requests,created_by)
        VALUES(:id,:code,:version,:name,:published,:monthly_price_minor,:currency,:monthly_provider_requests,:actor)"""),
        {"id": plan_id, "actor": actor_subject, **values})
    return {"id": str(plan_id), **values}


async def assign_subscription(connection, *, tenant_id, actor_subject, idempotency_key,
                              plan_version_id, expected_revision, expires_at, status):
    if not await ensure_account(connection, tenant_id):
        raise BillingError("customer_account_unavailable", "目标不是客户工作区")
    await _lock_customer(connection, tenant_id)
    request = {"plan_version_id": str(plan_version_id), "expected_revision": expected_revision,
               "expires_at": expires_at.isoformat() if expires_at else None, "status": status}
    old = (await connection.execute(text(
        "SELECT operation,request,result FROM tenant_billing_events "
        "WHERE tenant_id=:tenant AND idempotency_key=:key"
    ), {"tenant": tenant_id, "key": idempotency_key})).mappings().one_or_none()
    if old:
        if old["operation"] != "subscription.assign" or old["request"] != request:
            raise BillingError("billing_idempotency_conflict", "相同操作标识对应的套餐请求不同")
        return old["result"]
    plan = await connection.scalar(
        text("SELECT id FROM billing_plan_versions WHERE id=:id AND published"),
        {"id": plan_version_id},
    )
    if plan is None:
        raise BillingError("plan_unpublished", "套餐不存在或尚未发布")
    if expires_at and expires_at <= await connection.scalar(text("SELECT clock_timestamp()")):
        raise BillingError("subscription_expiry_invalid", "套餐到期时间必须晚于当前时间")
    revision = await connection.scalar(text("""UPDATE tenant_subscriptions
        SET plan_version_id=:plan,status=:status,expires_at=:expires,revision=revision+1,updated_at=now()
        WHERE tenant_id=:tenant AND revision=:expected RETURNING revision"""),
        {"tenant": tenant_id, "plan": plan_version_id, "status": status,
         "expires": expires_at, "expected": expected_revision})
    if revision is None:
        raise BillingError("subscription_revision_conflict", "套餐已被修改，请刷新后重试")
    result = {"tenant_id": str(tenant_id), "plan_version_id": str(plan_version_id),
              "revision": revision, "status": status, "expires_at": request["expires_at"]}
    await connection.execute(text("""INSERT INTO tenant_billing_events
        (id,tenant_id,idempotency_key,operation,request,result,actor_subject)
        VALUES(:id,:tenant,:key,'subscription.assign',CAST(:request AS jsonb),CAST(:result AS jsonb),:actor)"""),
        {"id": uuid4(), "tenant": tenant_id, "key": idempotency_key,
         "request": json.dumps(request), "result": json.dumps(result), "actor": actor_subject})
    return result


async def adjust_wallet(connection, *, tenant_id, actor_subject, idempotency_key,
                        amount_minor, reference, reason):
    """Audited manual adjustment, never an assertion that a payment was received."""
    if type(amount_minor) is not int or not 0 < abs(amount_minor) <= 10**12:
        raise BillingError("wallet_amount_invalid", "金额必须为非零整数分")
    if not await ensure_account(connection, tenant_id):
        raise BillingError("customer_account_unavailable", "目标不是客户工作区")
    await _lock_customer(connection, tenant_id)
    old = (await connection.execute(text("""SELECT * FROM tenant_wallet_entries
        WHERE tenant_id=:tenant AND idempotency_key=:key"""),
        {"tenant": tenant_id, "key": idempotency_key})).mappings().one_or_none()
    if old:
        if (old["amount_minor"], old["reference"], old["reason"]) != (amount_minor, reference, reason):
            raise BillingError("billing_idempotency_conflict", "相同操作标识对应的金额或依据不同")
        return dict(old)
    balance = await connection.scalar(text("""UPDATE tenant_wallet_accounts
        SET balance_minor=balance_minor+:amount,updated_at=now()
        WHERE tenant_id=:tenant AND balance_minor+:amount BETWEEN 0 AND 1000000000000
        RETURNING balance_minor"""), {"tenant": tenant_id, "amount": amount_minor})
    if balance is None:
        raise BillingError("wallet_balance_invalid", "调整后的余额超出允许范围")
    return dict((await connection.execute(text("""INSERT INTO tenant_wallet_entries
        (id,tenant_id,kind,amount_minor,balance_after_minor,idempotency_key,reference,reason,actor_subject)
        VALUES(:id,:tenant,'admin_adjustment',:amount,:balance,:key,:reference,:reason,:actor) RETURNING *"""),
        {"id": uuid4(), "tenant": tenant_id, "amount": amount_minor, "balance": balance,
         "key": idempotency_key, "reference": reference, "reason": reason,
         "actor": actor_subject})).mappings().one())


async def wallet_entries(connection, tenant_id, principal_id):
    await require_billing_owner(connection, tenant_id, principal_id)
    return [dict(row) for row in (await connection.execute(text("""SELECT id,kind,amount_minor,
        balance_after_minor,currency,reference,reason,created_at FROM tenant_wallet_entries
        WHERE tenant_id=:tenant ORDER BY created_at DESC,id DESC LIMIT 100"""),
        {"tenant": tenant_id})).mappings()]
