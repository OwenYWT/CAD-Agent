# CAD-Agent Review: 2026-11-10 467031e codex-resource-cost

## Conclusion

- Status: `PASS_WITH_LIMITATIONS`
- Reviewer agent: Codex
- Review ID: `codex-resource-cost`
- Reviewed at: `2026-11-10T14:34:05+08:00`
- Review range: `HEAD` (`467031e`) plus the current working tree
- Repository revision: `467031e` on `cms_dev`, aligned with `origin/cms_dev`
- Working tree state: modified evaluation source/tests, two new evaluation modules, one prior QA report, and two unrelated pre-existing personal design documents
- Business code modified by reviewer: `No`

The developer-only live evaluation increment is internally consistent and no material regression, fake implementation, new encoding corruption, or business-state coupling was found. Resource cost is derived only from evidence already recorded by the real lifecycle path. Missing model usage and monetary pricing evidence remain explicitly unavailable instead of being represented as zero or estimated values.

The remaining limitation is product evidence: the latest retained real report predates model-usage projection. A new observe-mode flow with both the API and Temporal worker running the current code is required to prove that provider usage is persisted in a real report. Historical report replay validates resource aggregation but is not current product acceptance for token capture.

## Change scope

- Change class: developer-only lifecycle observability and report aggregation
- User-described goal: add quantitative lifecycle resource reporting without affecting ordinary users or business behavior
- Changed paths: `backend/app/evaluation/human_report.py`, `backend/app/evaluation/integration.py`, `backend/app/evaluation/store.py`, `backend/app/evaluation/repair_gain.py`, `backend/app/evaluation/resource_cost.py`, `backend/tests/test_live_evaluation_integration.py`, `backend/tests/test_live_evaluation_store.py`
- Excluded paths: prior QA evidence and the two unrelated `2026-09-25-personal-composite-tools` design documents
- Functional inventory domains: `05` Agent modeling collaboration, `12` inspection/validation/evidence, `24` reliability/deployment/operations

## Implementation trace

| Changed symbol or contract | Implementation path | Consumers | Impact judgment |
| --- | --- | --- | --- |
| `build_resource_cost` | `backend/app/evaluation/resource_cost.py` | `FileEvaluationStore.finish`, JSON and Markdown reports | Read-only aggregation over recorded calls and timestamps; no workflow decision input |
| `_safe_model_usage` | `backend/app/evaluation/integration.py` | `_activity_tool_call`, `record_activity_result` | Projects only numeric provider usage fields; provider/model identity and arbitrary provenance are excluded |
| `FileEvaluationStore.finish` | `backend/app/evaluation/store.py` | workflow terminal and Change Set terminal observers | Finalizes report status/time, then derives repair and resource summaries; caller-side exceptions remain isolated from business execution |
| `_render_resource_cost` | `backend/app/evaluation/human_report.py` | developer Markdown report | Developer-readable projection only; no frontend or business contract consumes it |
| `build_repair_gain` | `backend/app/evaluation/repair_gain.py` | `FileEvaluationStore.finish` | Uses existing validation timeline/evidence and reports unknown when comparison evidence is absent |

## Findings

No material findings.

### Encoding and fake implementation review

- Scanner result: three leads across ten changed/untracked paths.
- Confirmed findings: none.
- False-positive lead: `status: completed` is a test input used to verify real provenance projection, not a production static-success branch.
- False-positive leads: fixture/placeholder words are in unrelated, pre-existing personal planning documents and are outside this review scope.
- New report text uses Unicode escapes and UTF-8 output. No new mojibake was detected in changed production lines.

### Compatibility review

- Frontend/backend contract: no frontend, API response, WebSocket payload, or ordinary user-visible state contract changed.
- WebSocket/event contract: lifecycle events remain unchanged; the new summaries are derived after events and calls are recorded.
- Persistence/migration/RLS: no database schema, repository, migration, tenant boundary, or RLS path changed. Reports remain local developer artifacts under an ignored directory.
- Workflow/retry/cancellation: resource aggregation reads attempt/replay metadata but does not alter retry, cancellation, timeout, or terminal decisions.
- Revision/candidate/draft/artifact identity: existing identities remain authoritative; resource fields do not advance Head or modify candidate/Change Set state.
- Permissions and external boundaries: no provider call, FreeCAD execution, validator rerun, billing lookup, or price estimation was added.
- Failure isolation: activity and terminal evaluation recording catch report failures; evaluator errors cannot replace the real operation result.
- Sensitive data: only numeric usage is projected under `input`, `output`, `total`, and `reasoning`; provider/model metadata, credentials, request content, and raw provenance are not stored in the new field.

## Test evidence

| Layer | Command or test | Result | Classification | Notes |
| --- | --- | --- | --- | --- |
| Static | `scan_review_risks.py --base HEAD --head HEAD --worktree` | PASS after manual triage | REVIEW | Three false-positive leads; no confirmed fake implementation or encoding issue |
| Static | `validate_feature_map.py` | PASS | CONTRACT | 24 domains and 424 referenced paths valid |
| Static | `git diff --check` | PASS | REVIEW | Only Windows LF/CRLF notices |
| Logic | focused live store/integration/writer tests | `54 passed` | LOGIC | Covers resource aggregation, Markdown rendering, safe usage projection, and report writing |
| Logic | all `test_live_evaluation_*` and `test_cad_evaluation_*` tests | `143 passed, 1 warning` | LOGIC | Warning is the existing Pydantic `model_kind` namespace warning |
| Service-adjacent | Agent worker, Change Set API, evidence, retry, and terminal tests | `25 passed, 3 skipped, 1 warning` | LOGIC/SERVICE | Skips are existing externally conditioned integration cases, not failures |
| Runtime evidence replay | newest retained live report `live_live-02136a54-d3df-4de1-9bbb-b66962442367.json` | PASS | HISTORICAL_RUNTIME | 21 calls, 15 measured activities, 64,526 ms activity time, 2 repairs / 22,875 ms; token usage truthfully unavailable |
| Product | new observe-mode flow on current code | NOT RUN | PRODUCT | Required to validate real token-usage persistence after API and worker restart |

## Gaps and blocked prerequisites

- Restart the API and Temporal worker with current code and execute one normal lifecycle flow in observe mode.
- Confirm the newest JSON and Markdown reports contain `resource_cost`.
- Confirm `model_usage` is `measured` or `partial` when the real provider returns usage, otherwise `unavailable` with the explicit evidence reason.
- Confirm `monetary_cost` remains `unavailable` until versioned pricing evidence is intentionally introduced.
- The separately observed single-dimension geometry drift is explicitly deferred by the user and was not modified or reclassified by this review.

## Knowledge update

- Review report: `docs/qa/code-review/2026-11-10-467031e-codex-resource-cost.md`
- Review memory: none; current observations are task-specific and the product acceptance boundary remains open
- Stable map/invariant update: none
- Reason and evidence: implementation and automated tests are stable, but real token persistence on the current worker code still requires one fresh workflow
