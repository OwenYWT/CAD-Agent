"""Pure shared operation allowlist; safe to import in Temporal's sandbox."""
MODEL_OPERATIONS = frozenset({
    'agent_v2.requirements', 'agent_v2.decompose', 'agent_v2.generate_source',
    'agent_v2.repair_source', 'agent_v2.generate_operations', 'agent_v2.repair_operations',
    'agent_v2.judge_visual', 'agent_v2.repair_visual',
})
