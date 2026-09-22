# Frozen release histories

Captured on 2026-09-22 by running unmodified source and tests from commits
`3dee44b18370203563bdf5d27788c83570004eb0` (before Model Jobs) and
`62084d5c84ead7bcd88030695fdfcd61112280ea` (after Model Jobs) in isolated databases
and Temporal namespaces. These are **not exports of historical customer traffic**.
No workflow patch override was used. Model responses are controlled test inputs;
Temporal, PostgreSQL, native geometry and Worker process recovery are real.

Sources: `test_native_visual_negative_budget_with_real_kernel`,
`test_agent_v2_plan_waits_before_candidate_source_or_execution`,
`test_worker_process_crash_retries_with_new_fenced_attempt` in
`tests/integration/test_temporal_mcadd_workflow.py`, and
`test_real_worker_returns_persisted_result`, `test_real_temporal_wait_and_cancellation`
in `tests/postgres/test_model_jobs.py` (the latter two only on 62084d5).

The manifest records source commit/tree, original and sanitized SHA-256, event
count and scenarios. Credentials, signed URL authentication and local user paths
are redacted in JSON payloads. Replay never executes activities or calls providers.
CI verifies digests and required scenarios before replaying with current code.
Existing dynamic replay remains complementary coverage. Add new fixtures with
provenance; never regenerate these files with current code to make replay pass.
