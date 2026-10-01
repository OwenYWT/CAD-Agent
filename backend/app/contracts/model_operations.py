"""Durable wire operation names, independent of implementations and SDK metadata."""
from typing import Awaitable, Callable, Mapping, Any

ModelHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
ModelHandlers = Mapping[str, ModelHandler]

MODEL_OPERATIONS = frozenset({
    'agent_v2.requirements', 'agent_v2.decompose', 'agent_v2.generate_source',
    'agent_v2.repair_source', 'agent_v2.generate_operations', 'agent_v2.repair_operations',
    'agent_v2.judge_visual', 'agent_v2.repair_visual',
})

# The existing leased job transport also carries long CAD work. Keeping this
# separate preserves the model-operation contract used by retained histories.
CAD_OPERATIONS = frozenset({
    'agent_v2.execute_freecad', 'agent_v2.validate_geometry',
    'agent_v2.render_visual', 'agent_v2.validate_dfm', 'agent_v2.generate_bom',
})
DURABLE_OPERATIONS = MODEL_OPERATIONS | CAD_OPERATIONS
