"""Conservative per-call/run reservations plus a persistent monthly S3 ledger.

Missing limits/prices/credentials fail before any provider call. Reservations
survive interrupted runs. Prices are operator-configured, not inferred invoices.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
import os
import threading


class BudgetExceeded(ValueError):
    pass


@dataclass(frozen=True)
class Limits:
    currency: str
    run: Decimal
    month: Decimal
    input_rate: Decimal
    output_rate: Decimal
    calls: int
    context: int
    output: int
    model: str

    @classmethod
    def environment(cls):
        def amount(name):
            value = Decimal(os.environ[name])
            if not value.is_finite() or value <= 0:
                raise ValueError('positive finite budget/price required: ' + name)
            return value
        limits = cls(os.environ['CAD_EVAL_CURRENCY'], amount('CAD_EVAL_RUN_LIMIT'), amount('CAD_EVAL_MONTH_LIMIT'),
            amount('CAD_EVAL_INPUT_PRICE_PER_MILLION'), amount('CAD_EVAL_OUTPUT_PRICE_PER_MILLION'),
            int(os.environ['CAD_EVAL_MAX_CALLS']), int(os.environ['CAD_EVAL_CONTEXT_TOKENS']),
            int(os.environ['CAD_EVAL_OUTPUT_TOKENS']), os.environ['CAD_EVAL_MODEL'])
        if limits.currency not in ('CNY', 'USD') or not limits.model or min(limits.calls, limits.context, limits.output) <= 0 or limits.output > limits.context or limits.run > limits.month:
            raise ValueError('invalid live evaluation limits')
        return limits

    @property
    def reservation(self):
        return (self.context*self.input_rate + self.output*self.output_rate)/Decimal(1_000_000)


class RunBudget:
    def __init__(self, limits):
        self.limits = limits
        self.charged = Decimal(0)
        self.records = []
        self.lock = threading.Lock()

    def reserve(self, model, output_tokens):
        with self.lock:
            if model != self.limits.model or type(output_tokens) is not int or not 0 < output_tokens <= self.limits.output:
                raise BudgetExceeded('unapproved model or output token request')
            if len(self.records) >= self.limits.calls or self.charged+self.limits.reservation > self.limits.run:
                raise BudgetExceeded('provider call/run cost budget exhausted')
            self.charged += self.limits.reservation
            self.records.append({'call': len(self.records)+1, 'cost': str(self.limits.reservation), 'usage_verified': False})
            return len(self.records)-1

    def settle(self, index, usage):
        with self.lock:
            row = self.records[index]
            if row['usage_verified'] or row.get('request_sent') is False:
                raise ValueError('provider usage already settled')
            # No usage means retain the full reservation. No zero-cost success.
            if not isinstance(usage, dict) or 'prompt_tokens' not in usage or 'completion_tokens' not in usage:
                return
            incoming, outgoing = usage['prompt_tokens'], usage['completion_tokens']
            if type(incoming) is not int or type(outgoing) is not int or min(incoming, outgoing) < 0:
                raise ValueError('invalid provider usage')
            actual = (incoming*self.limits.input_rate + outgoing*self.limits.output_rate)/Decimal(1_000_000)
            self.charged += actual-Decimal(row['cost'])
            row.update(cost=str(actual), input_tokens=incoming, output_tokens=outgoing, usage_verified=True)
            if incoming > self.limits.context or outgoing > self.limits.output or self.charged > self.limits.run:
                raise BudgetExceeded('provider usage exceeded configured model bounds')

    def release_unsubmitted(self, index, error_type):
        """Only connection establishment failures prove no inference was sent.

        Read/write/protocol failures retain the full reservation. The attempt
        still counts against the request limit and never fabricates token usage.
        """
        with self.lock:
            row = self.records[index]
            if error_type not in {'ConnectError', 'ConnectTimeout', 'PoolTimeout'} or row['usage_verified'] or row.get('request_sent') is False:
                raise ValueError('no proof of an unsubmitted request')
            self.charged -= Decimal(row['cost'])
            row.update(cost='0', request_sent=False, reservation_released_before_send=True,
                transport_error=error_type)


class MonthLedger:
    def __init__(self, store, key, currency, limit):
        self.store, self.key, self.currency, self.limit = store, key, currency, limit

    def update(self, run_id, amount, *, settle=False):
        # Conditional object replacement prevents parallel runs from overspending.
        for _ in range(3):
            state, etag = self.store.read(self.key)
            if state is None:
                state = {'schema_version': 'cad-eval-budget.v1', 'currency': self.currency, 'runs': {}}
            if state['currency'] != self.currency:
                raise ValueError('monthly ledger currency changed')
            runs = state['runs']
            if settle:
                if run_id not in runs or 'charged' in runs[run_id] or amount > Decimal(runs[run_id]['reserved']) or amount < 0 or not amount.is_finite():
                    raise BudgetExceeded('settlement is not owned by this reservation')
                runs[run_id]['charged'] = str(amount)
            elif run_id in runs:
                raise BudgetExceeded('run identity already reserved; reruns need a new attempt')
            else:
                runs[run_id] = {'reserved': str(amount)}
            total = sum((Decimal(row.get('charged', row['reserved'])) for row in runs.values()), Decimal(0))
            if total > self.limit:
                raise BudgetExceeded('monthly evaluation budget exhausted')
            if self.store.replace(self.key, state, etag):
                return
        raise BudgetExceeded('monthly ledger contention; no provider call allowed')


class S3Store:
    def __init__(self):
        import boto3
        self.bucket = os.environ['CAD_EVAL_LEDGER_BUCKET']
        self.client = boto3.client('s3', endpoint_url=os.environ['CAD_EVAL_LEDGER_ENDPOINT'],
            aws_access_key_id=os.environ['CAD_EVAL_LEDGER_ACCESS_KEY'],
            aws_secret_access_key=os.environ['CAD_EVAL_LEDGER_SECRET_KEY'])

    def read(self, key):
        from botocore.exceptions import ClientError
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            return json.loads(response['Body'].read()), response['ETag']
        except ClientError as error:
            if error.response['Error']['Code'] in ('NoSuchKey', '404'):
                return None, None
            raise

    def replace(self, key, value, etag):
        from botocore.exceptions import ClientError
        condition = {'IfMatch': etag} if etag else {'IfNoneMatch': '*'}
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=json.dumps(value).encode(),
                ContentType='application/json', **condition)
            return True
        except ClientError as error:
            if error.response['Error']['Code'] in ('PreconditionFailed', '412', 'ConditionalRequestConflict'):
                return False
            raise
