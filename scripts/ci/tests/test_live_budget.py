"""Offline budget protocol tests; none of these calls a paid provider."""
from copy import deepcopy
from decimal import Decimal
import os
from unittest.mock import patch
import unittest

from scripts.ci.live_budget import BudgetExceeded, Limits, MonthLedger, RunBudget


class MemoryStore:
    def __init__(self):
        self.value, self.version = None, 0
    def read(self, key):
        return deepcopy(self.value), str(self.version) if self.value is not None else None
    def replace(self, key, value, etag):
        if etag != (str(self.version) if self.value is not None else None):
            return False
        self.value, self.version = deepcopy(value), self.version+1
        return True


class LiveBudgetContract(unittest.TestCase):
    def limits(self, cap='1', calls=2):
        return Limits('CNY', Decimal(cap), Decimal('10'), Decimal('1'), Decimal('2'), calls, 1000, 1000, 'approved')

    def test_missing_configuration_fails_before_provider(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(KeyError):
            Limits.environment()

    def test_call_cap_rejects_before_third_request(self):
        budget=RunBudget(self.limits())
        budget.reserve('approved', 1000); budget.reserve('approved', 1000)
        with self.assertRaises(BudgetExceeded): budget.reserve('approved', 1000)
        self.assertEqual(len(budget.records), 2)

    def test_cost_reservation_rejects_before_over_budget_call(self):
        budget=RunBudget(self.limits('0.005'))
        budget.reserve('approved', 1000)
        with self.assertRaises(BudgetExceeded): budget.reserve('approved', 1000)

    def test_model_and_token_bounds_are_not_changed_to_pass(self):
        budget=RunBudget(self.limits())
        for model, output in [('other', 1000), ('approved', 1001)]:
            with self.assertRaises(BudgetExceeded): budget.reserve(model, output)
        self.assertEqual(budget.records, [])

    def test_verified_usage_releases_only_unused_reservation(self):
        budget=RunBudget(self.limits())
        slot=budget.reserve('approved', 1000)
        budget.settle(slot, {'prompt_tokens': 100, 'completion_tokens': 100})
        self.assertEqual(budget.charged, Decimal('0.0003'))
        with self.assertRaises(ValueError): budget.settle(slot, {})

    def test_missing_usage_and_interruption_keep_reservation(self):
        budget=RunBudget(self.limits())
        slot=budget.reserve('approved', 1000); budget.settle(slot, None)
        self.assertEqual(budget.charged, Decimal('0.003'))
        store=MemoryStore(); ledger=MonthLedger(store, 'month', 'CNY', Decimal('1'))
        ledger.update('interrupted', Decimal('0.8'))
        with self.assertRaises(BudgetExceeded): ledger.update('next', Decimal('0.3'))

    def test_monthly_settlement_and_run_identity_are_owned(self):
        store=MemoryStore(); ledger=MonthLedger(store, 'month', 'CNY', Decimal('1'))
        ledger.update('one', Decimal('0.8')); ledger.update('one', Decimal('0.2'), settle=True)
        ledger.update('two', Decimal('0.8'))
        with self.assertRaises(BudgetExceeded): ledger.update('two', Decimal('0.1'))
        with self.assertRaises(BudgetExceeded): ledger.update('missing', Decimal('0.1'), settle=True)

    def test_contended_ledger_fails_closed(self):
        class BusyStore(MemoryStore):
            def replace(self, key, value, etag): return False
        with self.assertRaises(BudgetExceeded): MonthLedger(BusyStore(), 'month', 'CNY', Decimal('1')).update('one', Decimal('0.1'))
