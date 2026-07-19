# CAD-Agent Case Study Adaptation Report

## Purpose
Track how CAD-Agent borrows selected ideas from CADAM and forgecad-public-kit, with source attribution, implementation scope, verification, and follow-up decisions.

## Adaptation 1: CADAM-Style Parameter Interaction

### Source
- Project: CADAM
- Borrowed idea: generated CAD code exposes editable top-level parameters; UI parses parameter metadata and re-executes the model without another LLM call.
- Local reference files studied:
  - `../CADAM/src/server/aiChat.ts` parameter-generation prompt contract
  - `../CADAM/src/components/parameter/ParameterSection.tsx` grouped/debounced parameter UI
  - `../CADAM/src/components/parameter/ParameterInput.tsx` numeric/enum/boolean/string/color control selection
  - `../CADAM/src/utils/parameterUtils.ts` parameter value validation and code replacement

### CAD-Agent Implementation
- Added CadQuery/Python parameter convention: group comments, Chinese labels, numeric assignment, range comments.
- Added backend parser and code replacement utility.
- Added rich `parameters` response field while preserving legacy `params`.
- Upgraded frontend parameter panel to render grouped backend-driven sliders and re-execute existing code through WebSocket.

### Verification
- `python -m pytest backend\tests\test_parameters.py backend\tests\test_executor.py -q` passed.
- `cd frontend && npm.cmd run build` passed.
- User manually verified the feature works.

## Adaptation 2: ForgeCAD-Style Validation And Repair Loop

### Source
- Project: forgecad-public-kit
- Borrowed idea: Agent CAD output should follow an evidence-driven loop: edit model code, run it, inspect evidence, repair, and iterate.
- Local reference files studied:
  - `../forgecad-public-kit/README.md` AI workflow description
  - `../forgecad-public-kit/skills/forgecad-build-model/SKILL.md` build/validate workflow discipline
  - `../forgecad-public-kit/skills/forgecad-inspect-model/SKILL.md` inspection evidence workflow

### CAD-Agent Implementation
- Preserved the existing orchestrator repair behavior and made it visible through typed metadata.
- Added `RepairStep` and `repair_history` to generation responses.
- Recorded repair events for validation, static analysis, geometry, vision, and execution retry paths.
- Added WebSocket consistency for manual parameter/code execution results.
- Added a frontend repair timeline that appears only when automatic repair happened.

### Verification
- `python -m pytest backend\tests\test_repair_history.py backend\tests\test_parameters.py backend\tests\test_executor.py -q` passed.
- `cd frontend && npm.cmd run build` passed.

### Decision
- Implemented medium version B: visible repair loop with one or more existing retry attempts recorded, without introducing a new multi-agent architecture.

## Adaptation 3: ForgeCAD-Style Printable Example RAG

### Source
- Project: forgecad-public-kit
- Borrowed idea: use a curated library of runnable CAD-as-code examples as generation context, then retrieve examples that match the requested part and modeling approach.
- Local reference files studied:
  - `../forgecad-public-kit/README.md` runnable workflow around authoring and executing model code
  - `../forgecad-public-kit/examples/` example-library structure and reusable modeling patterns
  - `../forgecad-public-kit/skills/forgecad-build-model/SKILL.md` emphasis on examples, inspection, and printable outputs

### CAD-Agent Implementation
- Added metadata-aware retrieval for both TF-IDF and Chroma retrievers.
- Indexed `part_type`, `features_used`, `modeling_hints`, `manufacturing_notes`, `failure_modes`, and `print_profile` alongside descriptions and tags.
- Added deterministic metadata boosts so planner output can favor examples matching part type, required features, and modeling hints.
- Added 8 printable CadQuery examples: parametric box, ribbed L bracket, PCB enclosure, pipe adapter, D-shaft knob, hinge pin, snap-fit clip, and vent panel.
- Updated CodeGen example formatting so prompts include manufacturing notes and common failure modes as DFM guardrails.
- Changed `Orchestrator` to lazily initialize the retriever, avoiding unnecessary Chroma/ONNX loading in execution-only flows and tests.

### Verification
- `python -m pytest backend\tests\test_example_retriever_metadata.py backend\tests\test_parameters.py backend\tests\test_repair_history.py -q` passed: 10 tests.

### Decision
- Implemented medium version B: metadata-rich printable example library plus retrieval boost, without introducing a separate ForgeCAD DSL/runtime.

## Adaptation 4: ForgeCAD-Style Inspect Report

### Source
- Project: forgecad-public-kit
- Borrowed idea: CAD generation should produce inspectable evidence from the run/inspect loop, not only final files.
- Local reference files studied:
  - `../forgecad-public-kit/README.md`
  - `../forgecad-public-kit/skills/forgecad-build-model/SKILL.md`
  - `../forgecad-public-kit/skills/forgecad-inspect-model/SKILL.md`

### CAD-Agent Implementation
- Extended `InspectReport` with export availability, repair attempt count, and evidence source.
- Ensured generated models and direct code execution expose printability evidence through the response contract.
- Passed `inspect_report` through the WebSocket manual execution payload.
- Added a frontend printability inspection panel in the analysis tab.
- Preserved raw validation, repair history, and download fields for compatibility.

### Verification
- `python -m pytest backend\tests\test_inspect_report.py backend\tests\test_repair_history.py backend\tests\test_e2e_pipeline.py::test_execute_code_direct_no_llm backend\tests\test_deploy_websocket.py::test_execute_code_returns_generation_result -q` passed.
- `cd frontend && npm.cmd run build` passed.

### Decision
- Implemented recommended version B: structured inspect report plus UI panel, without adding a separate ForgeCAD runtime or slicer.

## Adaptation 5: CADAM-Style Version Snapshots

### Source
- Project: CADAM
- Borrowed idea: durable conversation/message state makes prior creative outputs recoverable instead of overwriting the latest model state.
- Local reference files studied:
  - `../CADAM/src/contexts/ConversationContext.tsx`
  - `../CADAM/src/services/messageService.ts`
  - `../CADAM/src/services/conversationService.ts`
  - `../CADAM/src/components/Sidebar.tsx`
  - `../CADAM/src/components/history/ConversationCard.tsx`

### CAD-Agent Implementation
- Added panel-level `model_snapshots` storage with monotonically increasing versions.
- Saved successful model-producing WebSocket operations as snapshots.
- Added history endpoints for listing, reading, and restoring snapshots.
- Added a frontend version history panel with status badges and one-click restore.
- Preserved a `parent_snapshot_id` field for future branching while keeping the current UI linear.

### Verification
- `python -m pytest backend\tests\test_model_snapshots.py backend\tests\test_deploy_websocket.py backend\tests\test_deploy_persistence.py -q` passed.
- `cd frontend && npm.cmd run build` passed.

### Decision
- Implemented recommended version B: structured model snapshots and restore, without a full branch tree or geometry diff UI.

## Adaptation 6: CADAM / ForgeCAD-Style Agent Run Timeline

### Source

- CADAM: `../CADAM/src/components/chat/ChatReasoning.tsx`, `../CADAM/src/hooks/useLoadingProgress.tsx`, `../CADAM/src/constants/spinnerVerbs.ts`
- forgecad-public-kit: `../forgecad-public-kit/skills/forgecad-build-model/SKILL.md`, `../forgecad-public-kit/skills/forgecad-inspect-model/SKILL.md`

### Borrowed Idea

CADAM shows agent work as visible progress rather than an opaque wait state. ForgeCAD makes build, inspect, repair, and retry stages explicit. CAD-Agent adapts both ideas into a timeline that explains what the agent did, which stages warned or failed, and which recovery actions are safe.

### CAD-Agent Changes

- Extended backend `StepUpdate` with additive lifecycle metadata: `status`, `stage_id`, `attempt`, `started_at`, `duration_ms`, and `detail`.
- Added lightweight backend timeline helpers in `backend/app/agent/run_steps.py`.
- Added legacy step normalization so older `StepUpdate(step, message)` emissions still produce status, stage id, and timestamp metadata.
- Extended frontend timeline types and preserved completed run history after generation results.
- Added `AgentRunTimeline` in the analysis tab with status badges, timing, evidence summaries, `Retry Prompt`, and `Re-run Current Code` actions.
- Allowed failed generation results to keep the analysis panel visible so recovery actions can be reached.

### Non-Goals Kept

- No true backend cancellation was added.
- No pause/resume workflow was added.
- No hidden chain-of-thought or private model reasoning was exposed.
- No arbitrary mid-coroutine partial retry was added.

### Verification

- `python -m pytest backend\tests\test_agent_run_timeline.py backend\tests\test_deploy_websocket.py backend\tests\test_repair_history.py backend\tests\test_inspect_report.py -q` -> 34 passed
- `cd frontend && npm.cmd run build` -> passed with existing Vite chunk-size warning

## Adaptation 7: ForgeCAD-Style Engineering Brief

### Source

- forgecad-public-kit: `../forgecad-public-kit/skills/forgecad-design-spec/SKILL.md`
- forgecad-public-kit: `../forgecad-public-kit/skills/forgecad-design-spec/references/default-profiles.md`
- forgecad-public-kit: `../forgecad-public-kit/skills/forgecad-build-model/SKILL.md`

### Borrowed Idea

ForgeCAD treats the design brief as the source of truth before CAD code is produced. CAD-Agent adapts this by returning a visible `design_brief` with intent, manufacturing posture, assumptions, critical dimensions, requirements, printability targets, acceptance criteria, and open questions.

### CAD-Agent Changes

- Added `CriticalDimension` and `DesignBrief` schemas.
- Added fallback brief generation from `CADPlan` when planner output is missing or partial.
- Extended planner instructions to produce user-readable engineering briefs.
- Included brief context in code-generation prompts.
- Returned `design_brief` in generation results and preserved it through snapshots.
- Added `DesignBriefPanel` in the frontend analysis tab.

### Non-Goals Kept

- No blocking pre-generation approval step was added.
- No production certification or simulation claims were added.
- No hidden chain-of-thought was exposed.
- No external services were introduced.

### Verification

- `python -m pytest backend\tests\test_design_brief.py backend\tests\test_deploy_websocket.py backend\tests\test_model_snapshots.py -q` -> 27 passed
- `cd frontend && npm.cmd run build` -> passed with existing Vite chunk-size warning

## Adaptation 8: CADAM-Style Dynamic Suggestion Pills

### Source

- CADAM: `../CADAM/src/components/chat/SuggestionPills.tsx`
- CADAM: `../CADAM/src/components/chat/stuckToolRecovery.ts`
- CADAM: `../CADAM/src/components/chat/stuckToolRecovery.test.ts`

### Borrowed Idea

CADAM uses lightweight suggestion pills to help users continue, refine, or recover a CAD chat without needing exact technical phrasing. CAD-Agent adapts this into deterministic frontend prompt suggestions based on empty state, engineering brief open questions, inspect warnings, validation warnings, repair history, and successful generation results.

### CAD-Agent Changes

- Added pure frontend suggestion builder in `frontend/src/utils/suggestions.ts`.
- Added `SuggestionPills` UI component.
- Wired suggestions into the chat input area.
- Suggestions fill the input for user review and do not auto-send.
- Suggestions are hidden while generation is running.

### Non-Goals Kept

- No backend API was added.
- No LLM-generated suggestions were added.
- No prompt wizard was added.
- No suggestion auto-send behavior was added.

### Verification

- `cd frontend && npm.cmd run build` -> passed.

## Adaptation 9: CADAM-Style Engineering Parameter Panel

### Source

- CADAM: `../CADAM/shared/parseParameters.ts`
- CADAM: `../CADAM/src/components/parameter/ParameterSection.tsx`
- CADAM: `../CADAM/src/components/parameter/ParameterInput.tsx`

### Borrowed Idea

CADAM treats top-level CAD parameters as the public editing API of a generated model. CAD-Agent adapts this by making engineering-critical dimensions visible first, connecting generated `design_brief.critical_dimensions` with parsed `CADParameter` entries so users can adjust important model dimensions without asking the LLM to regenerate the whole design.

### CAD-Agent Changes

- Added `splitEngineeringParameters` in `frontend/src/utils/parameterMapping.ts` to match parsed CAD parameters against design-brief critical dimensions.
- Added focused Node tests in `frontend/tests/parameterMapping.test.ts` for critical-dimension matching and no-brief fallback behavior.
- Extended `ParameterPanel` with a `Critical Dimensions` section above standard parameter groups.
- Marked matched parameters with a `Critical` badge and displayed design-brief rationale where available.
- Passed `design_brief` from `App` into the parameter panel while keeping the existing `execute_code` parameter edit flow.

### Non-Goals Kept

- No new backend parameter-edit API was added.
- No enum, boolean, or color parameter types were added.
- No database schema changes were added.
- No automatic LLM regeneration was triggered by parameter edits.

### Verification

- `node --test --experimental-strip-types tests/parameterMapping.test.ts` -> 2 passed.
- `cd frontend && npm.cmd run build` -> passed with existing Vite chunk-size warning.

## Adaptation 10: ForgeCAD-Style Design Confirmation Gate

### Source

- forgecad-public-kit: `../forgecad-public-kit/skills/forgecad-design-spec/SKILL.md`
- forgecad-public-kit: `../forgecad-public-kit/skills/forgecad-design-spec/references/default-profiles.md`

### Borrowed Idea

ForgeCAD separates design understanding from model building so ambiguous requirements can be reviewed before CAD generation. CAD-Agent adapts this as a conservative confirmation gate: only briefs with open questions pause generation, while clear prompts continue through the existing generation pipeline.

### CAD-Agent Changes

- Added `needs_confirmation` to backend and frontend generation result schemas.
- Added an orchestrator gate after planning and `ensure_design_brief`: if `design_brief.open_questions` is non-empty, CAD generation stops before retrieval, codegen, sandbox execution, snapshots, or exports.
- Returned the full `plan` and `design_brief` with a `NeedsConfirmation` error payload so the frontend can show a reviewable engineering brief.
- Preserved pending brief context so the user's follow-up answer is combined with the prior brief and open questions on the next planning pass.
- Updated WebSocket and frontend chat copy to show confirmation as a yellow pending state instead of a red generation failure.
- Avoided duplicate frontend terminal timeline entries by treating `design_confirmation` as a terminal warning state.

### Non-Goals Kept

- No global confirmation step was added for clear/simple requests.
- No database schema changes were added.
- No new REST confirmation endpoint was added.
- No frontend approval workflow beyond chat follow-up was added.
- No automatic manufacturing certification claim was added.

### Verification

- `python -m pytest backend\tests\test_design_confirmation_gate.py backend\tests\test_design_brief.py backend\tests\test_deploy_websocket.py -q` -> 23 passed.
- `node --test --experimental-strip-types tests/parameterMapping.test.ts` -> 2 passed.
- `cd frontend && npm.cmd run build` -> passed.

## Adaptation 11: ForgeCAD-Style Lightweight Artifact Manifest

### Source

- forgecad-public-kit: `../forgecad-public-kit/skills/forgecad-build-model/SKILL.md`
- forgecad-public-kit: `../forgecad-public-kit/skills/forgecad-inspect-model/SKILL.md`

### Borrowed Idea

ForgeCAD treats CAD delivery as an engineering artifact package rather than a single mesh export. CAD-Agent adapts this by letting users download a lightweight manifest that preserves the generated files list, source code, parameters, design brief, validation result, inspection report, repair history, and provenance metadata.

### CAD-Agent Changes

- Added `buildArtifactManifest`, `manifestFilename`, and `manifestJson` in `frontend/src/utils/artifactManifest.ts`.
- Added focused Node tests in `frontend/tests/artifactManifest.test.ts` for manifest content and filename behavior.
- Added a `Manifest JSON` download action to `DownloadPanel`.
- Passed the active `GenerationResult` and latest user prompt from `App` into the download panel.
- Kept manifest generation fully frontend-side using browser `Blob` download.

### Non-Goals Kept

- No backend artifact package endpoint was added.
- No zip generation was added.
- No binary STL/STEP embedding was added.
- No database or snapshot schema changes were added.
- No external package dependency was added.

### Verification

- `node --test --experimental-strip-types tests/artifactManifest.test.ts tests/parameterMapping.test.ts` -> 4 passed.
- `cd frontend && npm.cmd run build` -> passed with existing Vite chunk-size warning.

## Adaptation 12: ForgeCAD-Style Manufacturing Profile

### Source

- forgecad-public-kit: `../forgecad-public-kit/skills/forgecad-design-spec/references/default-profiles.md`
- forgecad-public-kit: `../forgecad-public-kit/skills/forgecad-build-model/SKILL.md`

### Borrowed Idea

ForgeCAD makes the manufacturing context explicit before design and build work. CAD-Agent adapts this by carrying a structured manufacturing profile through user input, planning, design brief posture, code-generation context, DFM hints, generation results, and artifact manifests.

### CAD-Agent Changes

- Added backend `ManufacturingProfile` schema with process, material, nozzle diameter, layer height, and build volume fields.
- Added optional `manufacturing_profile` to `CADPlan`, `GenerateRequest`, `GenerateResponse`, and `GenerationResult`.
- Injected manufacturing profile context before planner calls and reflected it into `design_brief.manufacturing_posture` plus printability targets.
- Passed manufacturing profile through WebSocket `user_message`, REST generate, and batch generation paths while preserving compatibility for existing tests and callers.
- Added frontend manufacturing profile presets for `FDM PLA`, `FDM PETG`, `SLA Resin`, and `Generic`.
- Added a lightweight profile selector in the chat input area; messages and examples send the selected profile.
- Added manufacturing profile data to the downloadable artifact manifest.

### Non-Goals Kept

- No DFM rule-engine threshold rewrite was added.
- No material database was added.
- No account-level persistent printer settings were added.
- No advanced printer configuration page was added.
- No manufacturing certification or safety guarantee was added.

### Verification

- `python -m pytest backend\tests\test_manufacturing_profile.py backend\tests\test_design_confirmation_gate.py backend\tests\test_design_brief.py backend\tests\test_deploy_input_validation.py backend\tests\test_deploy_websocket.py -q` -> 71 passed.
- `node --test --experimental-strip-types tests/artifactManifest.test.ts tests/manufacturingProfiles.test.ts tests/parameterMapping.test.ts` -> 6 passed.
- `cd frontend && npm.cmd run build` -> passed with existing Vite chunk-size warning.

## Adaptation 13: CADAM-Style Structured Recovery Actions

### Source

- CADAM: `../CADAM/src/components/chat/stuckToolRecovery.ts`
- CADAM: `../CADAM/src/components/chat/stuckToolRecovery.test.ts`

### Borrowed Idea

CADAM rewrites stuck or incomplete tool states into explicit recoverable errors instead of leaving users in an ambiguous broken state. CAD-Agent adapts this into deterministic structured recovery actions that turn confirmation waits, failed generation, repair attempts, and inspect warnings into user-clickable next steps.

### CAD-Agent Changes

- Added backend `RecoveryAction` schema with `label`, `prompt`, `reason`, and `action_type`.
- Added deterministic recovery action builder in `backend/app/agent/recovery_actions.py`.
- Added recovery actions for `needs_confirmation`, failed generation/execution, repair history, inspect failures, and printability warnings.
- Attached `recovery_actions` to `GenerateResponse` and `GenerationResult` across generation, modification, direct code execution, and WebSocket result payloads.
- Added frontend `RecoveryAction` types and rendered action buttons in chat result cards.
- Recovery action buttons fill the input for user review and do not auto-send.
- Added `recovery_actions` to the lightweight artifact manifest.

### Non-Goals Kept

- No automatic retry was added.
- No additional LLM call is used to generate recovery actions.
- No repair loop retry budget was changed.
- No hidden reasoning or chain-of-thought is exposed.
- No new database schema was added.

### Verification

- `python -m pytest backend\tests\test_recovery_actions.py backend\tests\test_design_confirmation_gate.py backend\tests\test_manufacturing_profile.py backend\tests\test_design_brief.py backend\tests\test_deploy_input_validation.py backend\tests\test_deploy_websocket.py -q` -> 76 passed.
- `node --test --experimental-strip-types tests/artifactManifest.test.ts tests/manufacturingProfiles.test.ts tests/parameterMapping.test.ts` -> 6 passed.
- `cd frontend && npm.cmd run build` -> passed.

## Adaptation 14: CADAM-Style Reference Attachments

### Source

- CADAM: `../CADAM/shared/imageRefs.ts`
- CADAM: `../CADAM/shared/chatAi.ts`
- CADAM: `../CADAM/src/server/messageUtils.ts`
- CADAM: `../CADAM/src/components/chat/ChatSession.tsx`

### Borrowed Idea

CADAM keeps user-provided visual references connected to the chat request so the model can preserve intent, proportion, and context across the generation flow. CAD-Agent adapts this as a lightweight browser-side reference attachment layer: users can select local sketch/image/CAD reference files, and the app records metadata in the generated prompt and artifact manifest without uploading or storing binary content.

### CAD-Agent Changes

- Added `frontend/src/utils/referenceAttachments.ts` for supported attachment detection, category inference, size formatting, prompt formatting, and prompt parsing.
- Added focused Node tests in `frontend/tests/referenceAttachments.test.ts` for image metadata, CAD extension fallback, prompt formatting, and prompt parsing.
- Added a `Reference` file picker in `frontend/src/components/ChatPanel.tsx` accepting `png`, `jpg`, `jpeg`, `webp`, `stl`, `step`, and `stp` files.
- Added removable attachment chips in the chat input area with category labels and metadata tooltips.
- Appended a structured `Reference attachment:` prompt block to user messages before sending them to the backend.
- Extended `frontend/src/utils/artifactManifest.ts` to parse the latest prompt and record `reference_attachments` in downloaded manifest JSON.
- Extended `frontend/tests/artifactManifest.test.ts` to verify manifests include reference attachment metadata.

### Non-Goals Kept

- No backend upload endpoint was added.
- No binary image, STL, STEP, or STP content is transferred or stored.
- No vision model call was added.
- No STL/STEP geometry parsing was added.
- No new frontend dependency was added.

### Verification

- `node --test --experimental-strip-types tests/referenceAttachments.test.ts tests/artifactManifest.test.ts tests/manufacturingProfiles.test.ts tests/parameterMapping.test.ts` -> 10 passed.
- `cd frontend && npm.cmd run build` -> passed.

