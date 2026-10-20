"""Atomic provider reservations. HTTP never runs while a ledger lock is held.

Observation logs are deliberately absent from this boundary. An ambiguous send
keeps its reservation until a provider receipt is reconciled; lease expiry alone
does not prove that a request was free. Amounts use Decimal and PostgreSQL NUMERIC.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import logging
from uuid import UUID, uuid5

from sqlalchemy import text

from app.config import settings
from app.contracts.provider_budget import ProviderBudgetError
from app.db import get_database_engine, tenant_transaction
from app.execution.canonical import canonical_sha256
from app.execution.model_fence import assert_model_execution_current
from app.model_job_context import ModelJobContext

logger = logging.getLogger(__name__)


def _error(code, message):
    return ProviderBudgetError(code, message)


def _amount(value):
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0 or result.as_tuple().exponent < -12:
        raise ValueError('positive finite monetary amount with at most 12 decimals required')
    return result


def freeze_policy(*, mode, request_limit, month_request_limit, cost_contract=None):
    if (mode not in {'requests', 'hard_cost'} or type(request_limit) is not int
            or type(month_request_limit) is not int or min(request_limit, month_request_limit) < 1):
        raise _error('provider_budget_policy_invalid', 'Invalid provider request budget')
    policy = {'schema_version': 'provider-budget.v1', 'mode': mode,
              'request_limit': request_limit, 'month_request_limit': month_request_limit,
              'cost': None, 'reservation': '0'}
    if mode == 'requests':
        return policy
    try:
        cost = deepcopy(cost_contract)
        # Other providers require their own audited pricing/bound adapter. A
        # generic max_tokens field is not proof that reasoning is also bounded.
        if (not isinstance(cost, dict) or cost['provider'] != 'moonshot'
                or cost['model'] != 'kimi-k2.7-code' or cost['currency'] != 'CNY'
                or cost['output_parameter'] != 'max_tokens'
                or cost['reasoning_included_in_output'] is not True
                or any(not cost[k] for k in ('price_version', 'bound_version'))
                or not (cost['price_source'].startswith('https://platform.kimi.com/docs/')
                        or cost['price_source'] in {'https://platform.kimi.com/', 'https://platform.kimi.com'})
                or not cost['bound_source'].startswith('https://platform.kimi.com/docs/')):
            raise ValueError('unsupported billing contract')
        for key in ('input_bound', 'output_bound'):
            if type(cost[key]) is not int or not 0 < cost[key] <= 262144:
                raise ValueError('invalid token bound')
        # Use the full documented context and at least the documented default
        # output allowance. Never silently shrink generation to fit money left.
        if cost['input_bound'] != 262144 or not 32768 <= cost['output_bound'] <= cost['input_bound']:
            raise ValueError('unproven token bound')
        expiry = datetime.fromisoformat(cost['valid_until'].replace('Z', '+00:00'))
        if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
            raise ValueError('expired billing contract')
        for key in ('task_limit', 'month_limit', 'input_per_million', 'output_per_million'):
            cost[key] = str(_amount(cost[key]))
        if Decimal(cost['task_limit']) > Decimal(cost['month_limit']):
            raise ValueError('task limit exceeds monthly authorization')
        reservation = (cost['input_bound'] * Decimal(cost['input_per_million'])
                       + cost['output_bound'] * Decimal(cost['output_per_million'])) / Decimal(1_000_000)
        if reservation.as_tuple().exponent < -12:
            raise ValueError('reservation exceeds ledger precision')
        policy.update(cost=cost, reservation=str(reservation))
        return policy
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise _error('provider_cost_bound_unavailable', 'A current, verified provider billing contract is required') from exc


def prepare_request(policy, params, *, provider):
    params = deepcopy(params)
    if policy['mode'] != 'hard_cost':
        return params
    cost = policy['cost']
    if (provider != cost['provider'] or params['model'] != cost['model']
            or datetime.fromisoformat(cost['valid_until'].replace('Z', '+00:00')) <= datetime.now(timezone.utc)):
        raise _error('provider_cost_bound_unavailable', 'Request does not match the frozen billing contract')
    if (params.get('n', 1) != 1 or any(t.get('type') != 'function' for t in params.get('tools', []))
            or any(k in params for k in ('max_completion_tokens','max_output_tokens','prompt_cache_options'))
            or any(k in params.get('extra_body', {}) for k in
                   ('model', 'max_tokens', 'max_completion_tokens', 'max_output_tokens', 'tools', 'n', 'prompt_cache_options'))):
        raise _error('provider_cost_bound_unavailable', 'Unbounded provider option or separately billed tool')
    params[cost['output_parameter']] = cost['output_bound']
    return params


def receipt_amount(policy, provenance):
    cost, usage = policy['cost'], provenance.get('usage') or {}
    if cost is None:
        return None  # Requests-only accounting is never presented as free money.
    incoming, outgoing = usage.get('prompt_tokens'), usage.get('completion_tokens')
    if type(incoming) is not int or type(outgoing) is not int or min(incoming, outgoing) < 0:
        return None
    return (incoming * Decimal(cost['input_per_million']) + outgoing * Decimal(cost['output_per_million'])) / Decimal(1_000_000)


async def admit(connection, *, tenant_id, workflow_id, policy=None):
    """Freeze before dispatch in the same transaction as document admission."""
    policy = policy or freeze_policy(mode=settings.provider_budget_mode,
        request_limit=settings.provider_budget_request_limit,
        month_request_limit=settings.provider_budget_tenant_month_request_limit,
        cost_contract=settings.provider_budget_cost_contract)
    await connection.execute(text('''INSERT INTO provider_budget_policies
        (tenant_id,workflow_run_id,policy,policy_hash) VALUES(:tenant,:run,CAST(:policy AS jsonb),:hash)
        ON CONFLICT(workflow_run_id) DO NOTHING'''),
        {'tenant': tenant_id, 'run': workflow_id, 'policy': json.dumps(policy), 'hash': canonical_sha256(policy)})


class PgProviderBudget:
    def __init__(self, job):
        self.job = job
        self.policy = None
        self.ordinal = 0

    def logical_call_id(self):
        self.ordinal += 1
        return f'completion-{self.ordinal}'

    def _transaction(self, *, fenced=True):
        return tenant_transaction(self.job['tenant_id'], self.job['principal_id'], fence_model_job=fenced)

    async def _fence(self, connection):
        await assert_model_execution_current(connection, ModelJobContext(self.job['id'], self.job['generation']))

    async def prepare_request(self, params, *, provider):
        async with self._transaction() as conn:
            await self._fence(conn)
            row = (await conn.execute(text('SELECT policy,policy_hash FROM provider_budget_policies '
                'WHERE workflow_run_id=:run'), {'run': self.job['workflow_run_id']})).mappings().one_or_none()
        if row is None:
            raise _error('provider_budget_policy_missing', 'Provider budget must be frozen before dispatch')
        if canonical_sha256(row['policy']) != row['policy_hash']:
            raise _error('provider_budget_policy_invalid', 'Frozen provider policy digest mismatch')
        self.policy = row['policy']
        return prepare_request(self.policy, params, provider=provider)

    async def reserve_attempt(self, *, logical_call_id, attempt, request_digest, provider, model):
        if self.policy is None:
            raise _error('provider_budget_policy_missing', 'Request must be prepared before reservation')
        identity = uuid5(self.job['id'], f"{self.job['generation']}:{logical_call_id}:{attempt}")
        policy, cost = self.policy, self.policy['cost']
        amount = Decimal(policy['reservation'])
        async with self._transaction() as conn:
            await self._fence(conn)
            existing = (await conn.execute(text('SELECT * FROM provider_budget_reservations WHERE id=:id FOR UPDATE'),
                                            {'id': identity})).mappings().one_or_none()
            if existing:
                if existing['request_digest'] != request_digest or existing['policy'] != policy:
                    raise _error('provider_budget_identity_conflict', 'Logical request changed during recovery')
                return dict(existing)
            # A worker may die after the receipt commits but before its Model
            # Job result does. Replay that exact logical request without a new
            # physical reservation. Unknown billing stays held even when its
            # usable response was received; an absent response is never reused.
            completed = (await conn.execute(text('''SELECT * FROM provider_budget_reservations
                WHERE model_job_id=:job AND generation<:generation AND logical_call_id=:logical
                  AND request_digest=:digest AND provider=:provider AND model=:model
                  AND policy=CAST(:policy AS jsonb) AND response IS NOT NULL
                  AND status IN ('settled','reconciled','unknown','dispatch_claimed')
                ORDER BY generation DESC,attempt DESC LIMIT 1'''),
                {'job': self.job['id'], 'generation': self.job['generation'], 'logical': logical_call_id,
                 'digest': request_digest, 'provider': provider, 'model': model,
                 'policy': json.dumps(policy)})).mappings().one_or_none()
            if completed:
                return dict(completed)
            month = await conn.scalar(text("SELECT to_char(now() AT TIME ZONE 'UTC','YYYY-MM')"))
            scopes = [(f"task:{self.job['workflow_run_id']}", policy['request_limit'], cost['task_limit'] if cost else None),
                      (f'month:{month}', policy['month_request_limit'], cost['month_limit'] if cost else None)]
            accounts = []
            # All transactions acquire shared accounts in the same UUID order.
            for scope, limit, money in sorted(scopes, key=lambda s: str(uuid5(self.job['tenant_id'], s[0]))):
                account = uuid5(self.job['tenant_id'], scope)
                values = {'id': account, 'tenant': self.job['tenant_id'], 'scope': scope, 'limit': limit,
                          'currency': cost['currency'] if cost else None, 'money': Decimal(money) if money else None,
                          'policy': json.dumps({'mode': policy['mode']})}
                await conn.execute(text('''INSERT INTO provider_budget_accounts
                    (id,tenant_id,scope,request_limit,currency,cost_limit,policy)
                    VALUES(:id,:tenant,:scope,:limit,:currency,:money,CAST(:policy AS jsonb))
                    ON CONFLICT(tenant_id,scope) DO NOTHING'''), values)
                row = (await conn.execute(text('SELECT * FROM provider_budget_accounts WHERE id=:id FOR UPDATE'), values)).mappings().one()
                if row['request_limit'] != limit or row['cost_limit'] != values['money'] or row['currency'] != values['currency']:
                    raise _error('provider_budget_policy_conflict', 'Existing account authorization cannot be changed by a new task')
                if row['frozen'] or row['requests_used'] >= limit or (money and row['settled'] + row['held'] + amount > Decimal(money)):
                    raise _error('provider_budget_exhausted', f'Provider budget exhausted ({scope})')
                accounts.append(account)
            from app.services.customer_billing import BillingError, check_provider_request
            try:
                await check_provider_request(conn, self.job['tenant_id'])
            except BillingError as exc:
                raise _error(exc.code, str(exc)) from exc
            values = {**self.job, 'id': identity, 'job': self.job['id'], 'logical': logical_call_id,
                      'attempt': attempt, 'digest': request_digest, 'provider': provider, 'model': model,
                      'policy': json.dumps(policy), 'amount': amount}
            row = (await conn.execute(text('''INSERT INTO provider_budget_reservations
                (id,tenant_id,workflow_run_id,model_job_id,generation,logical_call_id,attempt,request_digest,
                 provider,model,policy,reserved_amount,status)
                VALUES(:id,:tenant_id,:workflow_run_id,:job,:generation,:logical,:attempt,:digest,
                       :provider,:model,CAST(:policy AS jsonb),:amount,'reserved') RETURNING *'''), values)).mappings().one()
            for account in accounts:
                await conn.execute(text('UPDATE provider_budget_accounts SET requests_used=requests_used+1,held=held+:amount WHERE id=:account'),
                                   {'account': account, 'amount': amount})
                await conn.execute(text('INSERT INTO provider_budget_allocations VALUES(:id,:account,:tenant)'),
                                   {'id': identity, 'account': account, 'tenant': self.job['tenant_id']})
            await self._event(conn, identity, 'reserved', {'request_digest': request_digest, 'amount': str(amount)})
            return dict(row)

    async def _event(self, conn, identity, event, evidence):
        await conn.execute(text('''INSERT INTO provider_budget_events
            (reservation_id,tenant_id,event_type,evidence) VALUES(:id,:tenant,:event,CAST(:evidence AS jsonb))
            ON CONFLICT(reservation_id,event_type) DO NOTHING'''),
            {'id': identity, 'tenant': self.job['tenant_id'], 'event': event, 'evidence': json.dumps(evidence)})

    async def _reservation(self, conn, reservation, *, replay_response=False):
        return (await conn.execute(text('''SELECT * FROM provider_budget_reservations
            WHERE id=:id AND model_job_id=:job AND (generation=:generation
                OR (:replay AND generation<:generation AND response IS NOT NULL)) FOR UPDATE'''),
            {'id': reservation['id'], 'job': self.job['id'], 'generation': self.job['generation'],
             'replay': replay_response})).mappings().one()

    async def claim_dispatch(self, reservation):
        async with self._transaction() as conn:
            await self._fence(conn)
            row = await self._reservation(conn, reservation, replay_response=True)
            if row['response'] is not None:
                return row['response']
            if row['status'] != 'reserved':
                raise _error('provider_dispatch_ambiguous', 'A prior dispatch may have been billed; it cannot be sent again')
            await conn.execute(text("UPDATE provider_budget_reservations SET status='dispatch_claimed',updated_at=now() WHERE id=:id"), {'id': row['id']})
            await self._event(conn, row['id'], 'dispatch_claimed', {})
        return None

    async def mark_unknown(self, reservation, *, reason):
        async with self._transaction(fenced=False) as conn:
            row = await self._reservation(conn, reservation)
            if row['status'] not in {'dispatch_claimed', 'unknown'}:
                return
            await conn.execute(text("UPDATE provider_budget_reservations SET status='unknown',updated_at=now() WHERE id=:id"), {'id': row['id']})
            await self._event(conn, row['id'], 'unknown', {'reason': reason})

    async def _accounts(self, conn, identity):
        return (await conn.execute(text('''SELECT a.* FROM provider_budget_accounts a
            JOIN provider_budget_allocations l ON l.account_id=a.id
            WHERE l.reservation_id=:id ORDER BY a.id FOR UPDATE OF a'''), {'id': identity})).mappings().all()

    @staticmethod
    def _receipt_identity(row, provenance):
        if any(provenance.get(k) != row[v] for k, v in (
                ('request_hash', 'request_digest'), ('provider', 'provider'), ('request_model', 'model'))):
            raise _error('provider_receipt_conflict', 'Receipt belongs to a different provider request')

    async def record_receipt(self, reservation, *, provenance, response):
        """Commit the response and retry intent without touching account balances.

        This is the durable handoff to settlement. It is not best-effort logging
        and deliberately precedes both settlement and model job completion.
        """
        async with self._transaction(fenced=False) as conn:
            row = await self._reservation(conn, reservation)
            self._receipt_identity(row, provenance)
            if row['receipt'] is not None or row['response'] is not None:
                if row['receipt'] != provenance or row['response'] != response:
                    raise _error('provider_receipt_conflict', 'Attempt already has a different receipt')
                return
            if row['status'] not in {'dispatch_claimed', 'unknown'}:
                raise _error('provider_receipt_conflict', 'No dispatch claim exists for this receipt')
            await conn.execute(text('''UPDATE provider_budget_reservations SET receipt=CAST(:receipt AS jsonb),
                response=CAST(:response AS json),settlement_next_attempt_at=now(),updated_at=now() WHERE id=:id'''),
                {'id': row['id'], 'receipt': json.dumps(provenance, allow_nan=False),
                 'response': json.dumps(response, allow_nan=False)})
            await self._event(conn, row['id'], 'receipt_recorded', {'receipt_hash': canonical_sha256(provenance)})

    async def settle_recorded(self, reservation):
        async with self._transaction(fenced=False) as conn:
            row = await self._reservation(conn, reservation)
            if row['receipt'] is None or row['response'] is None:
                raise _error('provider_receipt_missing', 'Settlement requires a durable provider receipt')
        await self.settle(reservation, provenance=row['receipt'], response=row['response'])

    async def settle(self, reservation, *, provenance, response):
        # Billing evidence must survive cancellation/takeover, but is restricted
        # to this exact attempt. It cannot write a model result or advance Head.
        async with self._transaction(fenced=False) as conn:
            row = await self._reservation(conn, reservation)
            if row['status'] in {'settled', 'reconciled'}:
                if row['receipt'] != provenance or row['response'] != response:
                    raise _error('provider_receipt_conflict', 'Attempt already has a different receipt')
                return
            if row['status'] not in {'dispatch_claimed', 'unknown'}:
                raise _error('provider_receipt_conflict', 'No dispatch claim exists for this receipt')
            self._receipt_identity(row, provenance)
            if row['response'] is not None and (row['response'] != response or
                    (row['status'] != 'unknown' and row['receipt'] != provenance)):
                raise _error('provider_receipt_conflict', 'Attempt already has a different receipt')
            amount = receipt_amount(row['policy'], provenance)
            cost = row['policy']['cost']
            if cost is not None and (amount is None or provenance.get('model') != cost['model']):
                await conn.execute(text("UPDATE provider_budget_reservations SET status='unknown',receipt=CAST(:receipt AS jsonb),response=CAST(:response AS json),settlement_next_attempt_at=NULL,settlement_last_error=NULL,updated_at=now() WHERE id=:id"),
                    {'id': row['id'], 'receipt': json.dumps(provenance), 'response': json.dumps(response, allow_nan=False)})
                await self._event(conn, row['id'], 'unknown', {'reason': 'missing_usage_or_unpriced_model'})
                return
            usage = provenance.get('usage') or {}
            over_bound = cost is not None and (amount > row['reserved_amount']
                or usage['prompt_tokens'] > cost['input_bound'] or usage['completion_tokens'] > cost['output_bound'])
            for account in await self._accounts(conn, row['id']):
                await conn.execute(text('''UPDATE provider_budget_accounts SET held=held-:reserved,
                    settled=settled+:actual,frozen=frozen OR :over WHERE id=:id'''),
                    {'id': account['id'], 'reserved': row['reserved_amount'], 'actual': amount or Decimal(0), 'over': over_bound})
            status = 'reconciled' if row['status'] == 'unknown' else 'settled'
            await conn.execute(text('''UPDATE provider_budget_reservations SET status=:status,actual_amount=:actual,
                receipt=CAST(:receipt AS jsonb),response=CAST(:response AS json),settlement_next_attempt_at=NULL,
                settlement_last_error=NULL,updated_at=now() WHERE id=:id'''),
                {'id': row['id'], 'status': status, 'actual': amount, 'receipt': json.dumps(provenance),
                 'response': json.dumps(response, allow_nan=False)})
            await self._event(conn, row['id'], status, {'receipt_hash': canonical_sha256(provenance),
                'actual_amount': str(amount) if amount is not None else None, 'over_bound': over_bound})

    async def release_unsent(self, reservation):
        async with self._transaction(fenced=False) as conn:
            row = await self._reservation(conn, reservation)
            if row['status'] == 'released_unsent':
                return
            if row['status'] != 'reserved':
                raise _error('provider_dispatch_ambiguous', 'A dispatch claim prevents an unproven refund')
            for account in await self._accounts(conn, row['id']):
                await conn.execute(text('UPDATE provider_budget_accounts SET held=held-:amount WHERE id=:id'),
                                   {'id': account['id'], 'amount': row['reserved_amount']})
            await conn.execute(text("UPDATE provider_budget_reservations SET status='released_unsent',updated_at=now() WHERE id=:id"), {'id': row['id']})
            await self._event(conn, row['id'], 'released_unsent', {'proof': 'dispatch_never_claimed'})


async def recover_next_settlement():
    """Lease one durable receipt across all tenants, including completed jobs.

    A crash after claim leaves a scheduled retry. Concurrent or repeated
    delivery is safe because the reservation and accounts settle atomically.
    """
    async with get_database_engine().begin() as conn:
        await conn.execute(text('SET LOCAL ROLE cad_agent_model_dispatch'))
        row = (await conn.execute(text('''WITH due AS (
            SELECT id FROM provider_budget_reservations
            WHERE settlement_next_attempt_at<=now() AND status IN ('dispatch_claimed','unknown')
            ORDER BY settlement_next_attempt_at,id FOR UPDATE SKIP LOCKED LIMIT 1
        ) UPDATE provider_budget_reservations r SET settlement_attempts=settlement_attempts+1,
            settlement_next_attempt_at=now()+LEAST(300,30*power(2,LEAST(settlement_attempts,4)))*interval '1 second'
            FROM due,model_jobs j WHERE r.id=due.id AND j.id=r.model_job_id AND j.tenant_id=r.tenant_id
            RETURNING r.id AS reservation_id,r.model_job_id AS id,r.tenant_id,r.generation,
                      j.principal_id,j.workflow_run_id'''))).mappings().one_or_none()
    if row is None:
        return False
    port = PgProviderBudget(dict(row))
    try:
        await port.settle_recorded({'id': row['reservation_id']})
    except Exception as exc:
        logger.exception('Provider settlement retry failed; durable receipt remains queued')
        async with port._transaction(fenced=False) as conn:
            await conn.execute(text('''UPDATE provider_budget_reservations SET settlement_last_error=:error
                WHERE id=:id AND settlement_next_attempt_at IS NOT NULL'''),
                {'id': row['reservation_id'], 'error': type(exc).__name__})
    return True


async def run_provider_settlements():
    while True:
        try:
            recovered = await recover_next_settlement()
        except Exception:
            logger.exception('Provider settlement dispatcher unavailable; pending receipts remain durable')
            await asyncio.sleep(5)
        else:
            await asyncio.sleep(0 if recovered else 0.5)
