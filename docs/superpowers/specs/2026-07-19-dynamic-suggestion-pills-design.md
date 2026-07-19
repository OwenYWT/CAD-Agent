# CADAM-Style Dynamic Suggestion Pills Design

## Summary

Add dynamic prompt suggestion pills to CAD-Agent. The UI should guide users toward clearer CAD requests and safer follow-up actions by deriving short clickable suggestions from the current generation state, engineering brief, inspection report, and repair history.

This adapts CADAM's lightweight `SuggestionPills` pattern into CAD-Agent while keeping the current chat-first workflow.

## Source Attribution

- Source project: `CADAM`
- Borrowed idea: show lightweight, horizontally scrollable suggestion pills that users can click to continue or refine a conversation.
- Local references studied:
  - `../CADAM/src/components/chat/SuggestionPills.tsx`
  - `../CADAM/src/components/chat/stuckToolRecovery.ts`
  - `../CADAM/src/components/chat/stuckToolRecovery.test.ts`

CADAM emphasizes these principles:

- Suggestions should be small, visible, and easy to ignore.
- Suggestions should help users recover or continue without needing to know exact technical phrasing.
- Recovery logic should be pure and testable where possible.
- Clicking a suggestion should remain user-controlled rather than silently performing hidden complex actions.

## Current CAD-Agent State

CAD-Agent already has useful signals for dynamic suggestions:

- `frontend/src/components/ChatPanel.tsx` owns the chat input and empty-state example prompts.
- `frontend/src/stores/sessionStore.ts` exposes active panel messages, generation status, result, timeline history, and current result payload.
- `GenerationResult` can include `design_brief`, `inspect_report`, `repair_history`, `validation`, `code`, `plan`, and `attempts`.
- `AgentRunTimeline` already exposes safe recovery actions for retrying the latest prompt and re-running current code.
- `DesignBriefPanel` exposes assumptions, open questions, critical dimensions, printability targets, and acceptance criteria.

Current limitations:

- Empty-state examples help only before the first prompt.
- After generation, users must manually decide how to improve unclear requirements, dimensions, printability, or failed inspections.
- Open questions from `design_brief` are visible but not directly actionable.
- Inspection warnings and repair history are visible but not translated into easy follow-up prompts.
- Users who do not know CAD terminology may struggle to phrase useful modifications.

## Goal

Add CADAM-style dynamic suggestion pills that improve prompt quality and recovery speed.

The feature should let users:

- Start faster from high-quality CAD prompt patterns.
- Add missing dimensions or constraints from `design_brief.open_questions`.
- Improve printability from `inspect_report` or `validation` warnings.
- Continue after a failure with simpler or more explicit prompts.
- Use suggestions without losing control of the chat input.

## Non-Goals

- Do not add a full prompt wizard in this phase.
- Do not send suggestions automatically without a user click.
- Do not call the LLM to generate suggestions in this phase.
- Do not add backend APIs.
- Do not replace existing empty-state examples.
- Do not duplicate `AgentRunTimeline` recovery buttons; suggestions should complement them with prompt text.

## Proposed Scope: B, Recommended Version

### Pure Suggestion Builder

Create a pure frontend utility, likely `frontend/src/utils/suggestions.ts`, that turns current panel/result state into short suggestion objects.

Suggested type:

```ts
export type SuggestionIntent = "generate" | "clarify" | "modify" | "repair" | "printability" | "explain";

export interface PromptSuggestion {
  label: string;
  prompt: string;
  intent: SuggestionIntent;
}
```

Inputs:

```ts
interface BuildSuggestionsInput {
  isEmpty: boolean;
  isGenerating: boolean;
  result: GenerationResult | null;
  latestUserMessage?: string;
}
```

Behavior:

- Return no suggestions while `isGenerating` is true.
- For empty chat, return starter prompts such as:
  - `Phone stand`
  - `Mounting bracket`
  - `Cable clip`
  - `Parametric box`
- If `design_brief.open_questions` exists, convert up to two questions into clarification prompts.
- If `inspect_report.verdict` is `warn` or `fail`, suggest printability or simplification follow-ups.
- If `validation.print_warnings` exists, suggest improving wall thickness, build volume fit, or edge treatment.
- If `repair_history` has entries, suggest simplifying geometry or explaining the failure.
- If generation succeeded and has code, suggest common modifications such as adding holes, rounding edges, strengthening walls, or changing dimensions.
- Deduplicate labels and cap output to 6 pills.

### Suggestion UI Component

Create `frontend/src/components/SuggestionPills.tsx` inspired by CADAM.

Component responsibilities:

- Accept `suggestions`, `disabled`, and `onSelect`.
- Render a horizontal scrollable row of rounded buttons.
- Return `null` when there are no suggestions.
- Keep styling consistent with CAD-Agent's Tailwind style.
- Use English labels to avoid Windows console encoding issues.

### ChatPanel Integration

Update `frontend/src/components/ChatPanel.tsx`:

- Build dynamic suggestions from active panel state.
- Show suggestions above the text input.
- Keep existing empty-state example cards.
- Clicking a suggestion should place its `prompt` into the input by default.
- For clearly safe follow-up suggestions, optionally send immediately only if the suggestion is explicitly worded as an action and the implementation keeps the behavior consistent. Recommended first pass: fill the input, do not auto-send.
- Disable suggestions while generation is running.

### Suggested Prompt Text

Starter examples:

- Label: `Phone stand`
  - Prompt: `Create a 3D-printable adjustable phone stand with rounded edges, stable base, and sensible default dimensions.`
- Label: `Mounting bracket`
  - Prompt: `Create a 3D-printable L-shaped mounting bracket with two screw holes, filleted edges, and editable dimensions.`
- Label: `Cable clip`
  - Prompt: `Create a 3D-printable cable clip for a desk edge with a snap slot, rounded edges, and editable cable diameter.`
- Label: `Parametric box`
  - Prompt: `Create a parametric electronics enclosure with lid, wall thickness, screw bosses, and ventilation slots.`

Follow-up examples:

- Label: `Add dimensions`
  - Prompt: `Use these dimensions: length 80 mm, width 40 mm, height 20 mm, wall thickness 2.4 mm.`
- Label: `Make printable`
  - Prompt: `Revise the model for easier FDM 3D printing: keep walls at least 2.4 mm, soften exposed edges, and reduce unsupported overhangs.`
- Label: `Add holes`
  - Prompt: `Add mounting holes with editable diameter, spacing, and countersink options while preserving the current shape.`
- Label: `Round edges`
  - Prompt: `Round sharp exposed edges with safe fillets while preserving all functional features.`
- Label: `Make stronger`
  - Prompt: `Strengthen the model with thicker walls, ribs, and larger fillets without changing the main footprint.`
- Label: `Simplify geometry`
  - Prompt: `Regenerate using simpler robust geometry while preserving the core function and important dimensions.`
- Label: `Explain failure`
  - Prompt: `Explain why the previous generation failed and suggest the smallest prompt change to fix it.`

### Traceability

Add an `Adaptation 8` section to `docs/case-study-adaptations/CAD-Agent-adaptation-report.md` with:

- CADAM source references.
- Borrowed idea.
- Frontend utility/component changes.
- Integration behavior.
- Verification commands and results.
- Non-goals kept.

## Acceptance Criteria

- Empty chat still shows existing example cards and also offers lightweight suggestion pills near the input.
- Suggestions are hidden or disabled during generation.
- After a result with `design_brief.open_questions`, at least one suggestion helps answer or clarify those questions.
- After `inspect_report.warn` or `inspect_report.fail`, suggestions include printability or simplification follow-ups.
- After a successful result with code, suggestions include common CAD edits such as holes, rounded edges, strength, or dimensions.
- Clicking a suggestion fills the chat input with the suggestion prompt and does not auto-send in the first implementation.
- The suggestion builder is pure and covered by frontend-friendly unit tests if a test runner is available; otherwise it is covered by TypeScript build and simple pure-function tests added only if the project already supports them.
- `npm.cmd run build` passes.
- The adaptation report records the CADAM source and implementation details.

## Test Plan

Frontend:

- Add pure unit tests only if the project has a test runner. Current `frontend/package.json` has build and lint scripts but no test script, so do not introduce a new test framework just for this feature.
- Use `npm.cmd run build` as the required type/integration check.
- Manually verify the UI states:
  - Empty chat suggestions render.
  - Suggestions fill the input rather than auto-send.
  - Suggestions disable while generating.
  - Open questions generate clarification prompts.
  - Inspect warnings generate printability prompts.

Backend:

- No backend changes are expected.
- No backend tests are required for this frontend-only adaptation.

Expected verification command:

- `cd frontend && npm.cmd run build`

## Risks And Mitigations

- Risk: suggestions feel noisy.
  - Mitigation: cap to 6 pills and make them horizontally scrollable and easy to ignore.
- Risk: suggestions send unwanted prompts.
  - Mitigation: first implementation fills the input but does not auto-send.
- Risk: suggestions duplicate existing buttons.
  - Mitigation: timeline actions remain operational buttons; suggestion pills remain editable prompt text.
- Risk: frontend grows tangled if logic lives in `ChatPanel`.
  - Mitigation: keep suggestion derivation in a pure utility and UI in a small component.
- Risk: labels do not fit all CAD domains.
  - Mitigation: use general suggestions and derive contextual prompts from result fields when possible.

## Open Design Decisions

- Auto-send suggestions: deferred; first pass fills input only.
- Dedicated test runner: deferred; do not add frontend test framework unless already present.
- Backend-generated suggestions: deferred; first pass uses deterministic frontend rules.
