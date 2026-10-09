"""Protocol-only regression: no provider calls or substituted CAD results."""
from decimal import Decimal
import unittest

from scripts.ci.live_budget import BudgetExceeded, Limits, RunBudget
from scripts.ci.live_gateway import StreamUsage


class LiveStreamContract(unittest.TestCase):
    def stream(self, lines):
        parser = StreamUsage()
        for line in lines:
            parser.feed(line)
        return parser

    def test_complete_stream_retains_usage_and_ignores_heartbeat(self):
        parser = self.stream([': heartbeat', '', 'data: {"choices":[{"delta":{"content":"CAD"}}]}', '',
            'data: {"choices":[],"usage":{"prompt_tokens":20,"completion_tokens":3}}', '', 'data: [DONE]', ''])
        self.assertEqual(parser.completed_usage(), {'prompt_tokens':20,'completion_tokens':3})

    def test_truncated_stream_cannot_release_reservation(self):
        for tail in [[], ['data: [DONE]']]:
            with self.subTest(tail=tail):
                parser=self.stream(['data: {"usage":{"prompt_tokens":20,"completion_tokens":3}}','',*tail])
                with self.assertRaises(ValueError): parser.completed_usage()

    def test_complete_stream_without_usage_fails_closed(self):
        parser=self.stream(['data: {"choices":[]}', '', 'data: [DONE]', ''])
        with self.assertRaises(ValueError): parser.completed_usage()

    def test_duplicate_usage_error_and_trailing_data_are_rejected(self):
        for lines in [
            ['data: {"usage":{}}','','data: {"usage":{}}',''],
            ['data: {"error":{"message":"failed"}}',''],
            ['data: [DONE]','','data: {"choices":[]}'],
        ]:
            with self.subTest(lines=lines), self.assertRaises(ValueError): self.stream(lines)

    def test_invalid_output_bounds_do_not_reserve_a_call(self):
        budget=RunBudget(Limits('CNY',Decimal('5'),Decimal('5'),Decimal('6.5'),Decimal('27'),8,262144,32768,'approved'))
        for value in [None,0,-1,True,3.5,'32768',32769]:
            with self.subTest(value=value), self.assertRaises(BudgetExceeded): budget.reserve('approved',value)
        self.assertEqual(budget.records,[])

    def test_only_pre_send_connection_failure_releases_cost_not_attempt(self):
        budget=RunBudget(Limits('CNY',Decimal('5'),Decimal('5'),Decimal('6.5'),Decimal('27'),8,262144,32768,'approved'))
        slot=budget.reserve('approved',32768)
        for error in ['ReadError','WriteError','ReadTimeout','RemoteProtocolError','']:
            with self.subTest(error=error), self.assertRaises(ValueError):budget.release_unsubmitted(slot,error)
        self.assertEqual(budget.charged,budget.limits.reservation)
        budget.release_unsubmitted(slot,'ConnectError')
        self.assertEqual(budget.charged,0)
        self.assertEqual(len(budget.records),1)
        self.assertFalse(budget.records[slot]['usage_verified'])
        self.assertFalse(budget.records[slot]['request_sent'])
        with self.assertRaises(ValueError):budget.release_unsubmitted(slot,'ConnectError')
        with self.assertRaises(ValueError):budget.settle(slot,{'prompt_tokens':0,'completion_tokens':0})
