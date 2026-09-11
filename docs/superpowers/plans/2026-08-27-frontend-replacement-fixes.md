# Frontend Replacement Fixes Implementation Plan

> This plan executes the findings in `.gstack/qa-reports/frontend-replacement-20260827T092737-c3a7/qa-report.md`. The report and `/Users/wentao/Downloads/前端.html` are the approved design specification. No new audit, Git commit, push, API change, or business-logic rewrite is in scope.

## Goal

Remove the remaining old/new UI mixing, implement the reference responsive and bilingual behaviors, keep the QA harness aligned with production, and verify the existing CAD workflow still works end to end.

## Tasks

1. Add regression coverage for the exact reported failures: legacy palette use in target surfaces, account Escape handling, 1180/760 breakpoints, mobile preview overlay, language provider wiring, and QA harness production reuse.
2. Restyle `LoginPage`, auth bootstrap/session restore states, and `ErrorBoundary` with the existing workspace tokens and controls.
3. Restyle `SettingsDrawer`, `DFMRuleConfig`, `KnowledgeGraph`, `CapabilityCatalog`, `CapabilityRunner`, and `AccountPanel`; add account popover focus/Escape/outside-click behavior without changing API calls.
4. Keep the inspector in the desktop grid through 1181px, hide it only at 1180px and below, and make 760px and below Agent-first with the real workspace preview rendered as a dismissible overlay.
5. Add a React locale provider with persisted `zh`/`en` state and stable message keys. Translate application chrome and state copy while leaving user-authored prompts, generated model data, identifiers, and backend error messages untouched.
6. Replace the hand-built QA fixture shell with the production `EngineeringWorkspace` entry, confined to `frontend/qa`.
7. Verify each task with its regression test and live browser interaction. Then run desktop/tablet/mobile coverage, dialogs, DFM/settings/account, language switching, history restore, Agent, inspector, real CAD generation, ESLint, TypeScript, production build, all tests, and `git diff --check`.

## Verification commands

```bash
cd frontend
node --test tests/*.test.ts
npm run lint
npx tsc -b --pretty false
npm run build
cd ..
git diff --check
```

Browser verification must use the running local frontend and real backend. No mock responses or placeholder interactions are acceptable.
