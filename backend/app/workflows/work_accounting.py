"""Deterministic workflow counters, reconstructed from history during replay.

These limit logical work. Physical provider attempts and money need the separate
transactional provider ledger; these counters make no billing guarantees.
"""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from temporalio.exceptions import ApplicationError


class WorkBudgetPolicy(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    schema_version: Literal['agent-work-budget.v1'] = 'agent-work-budget.v1'
    max_work_units: int = Field(default=128, ge=1, le=4096)
    max_repair_attempts: int = Field(default=16, ge=1, le=128)
    max_checkpoint_turns: int = Field(default=8, ge=1, le=64)
    max_normal_tool_calls: int = Field(default=16, ge=1, le=64)


class WorkAccounting:
    def __init__(self, policy: WorkBudgetPolicy):
        self.policy = policy
        self.work: set[str] = set()
        self.checkpoints: set[str] = set()
        self.repairs: dict[str, int] = {}
        self.gates: dict[str, dict[str, int]] = {}

    def used(self, scope: str, stage: str) -> int:
        return self.gates.get(scope, {}).get(stage, 0)

    def _check(self, name: str, used: int, limit: int) -> None:
        if used >= limit:
            raise ApplicationError(
                f'任务预算 {name} 已耗尽（{used}/{limit}），保留当前模型与原始失败证据。',
                {'budget_name': name, 'budget_used': used, 'budget_limit': limit,
                 'work_accounting': self.snapshot()},
                type='agent_work_budget_exhausted', non_retryable=True)

    def reserve_work(self, key: str) -> None:
        if key not in self.work:
            self._check('total_work_units', len(self.work), self.policy.max_work_units)
            self.work.add(key)

    def reserve_checkpoint(self, key: str) -> None:
        if key not in self.checkpoints:
            self._check('checkpoint_turns', len(self.checkpoints), self.policy.max_checkpoint_turns)
            self.checkpoints.add(key)

    def reserve_repair(self, key: str, scope: str, stage: str) -> int:
        if key not in self.repairs:
            self._check('total_repair_attempts', len(self.repairs), self.policy.max_repair_attempts)
            self.repairs[key] = len(self.repairs) + 1
            self.gates.setdefault(scope, {})[stage] = self.used(scope, stage) + 1
        return self.repairs[key]

    def snapshot(self) -> dict:
        return {'schema_version': 'repair-accounting.v2',
                'repair_sequence': len(self.repairs), 'work_units': len(self.work),
                'checkpoint_turns': len(self.checkpoints),
                'gate_used': {key: dict(value) for key, value in self.gates.items()},
                'policy': self.policy.model_dump(mode='json')}
