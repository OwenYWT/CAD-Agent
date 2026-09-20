"""Durable wire operation names, independent of implementations and SDK metadata."""
from typing import Awaitable, Callable, Mapping, Any

ModelHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
ModelHandlers = Mapping[str, ModelHandler]

MODEL_OPERATIONS = frozenset({
    'agent_v2.requirements', 'agent_v2.decompose', 'agent_v2.generate_source',
    'agent_v2.repair_source', 'agent_v2.generate_operations', 'agent_v2.repair_operations',
    'agent_v2.judge_visual', 'agent_v2.repair_visual',
})
