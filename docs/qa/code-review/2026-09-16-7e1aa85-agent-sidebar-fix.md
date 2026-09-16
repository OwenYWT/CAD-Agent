# CAD-Agent Review: 2026-09-16 Agent Sidebar Fix

## Conclusion

- Status: `PASS_WITH_LIMITATIONS`
- Base revision: `7e1aa85`
- Change class: responsive frontend bug fix
- Business code modified: `Yes`, limited to the Agent visibility state transition

The responsive project sidebar is now closed before the Agent surface is exposed. This removes the `900-1023px` visibility gap and also prevents the sidebar from covering the Agent-first mobile pane at `<=760px`.

## Implementation

- `frontend/src/components/workspace/EngineeringWorkspace.tsx`
  - `askAgent` now calls `setMobileSidebar(false)` before expanding the Agent pane or selecting the responsive Agent presentation.
- `frontend/tests/responsive-intermediate.regression-1.test.ts`
  - Added a regression assertion that the responsive sidebar closes before `setAgentCollapsed(false)` exposes the composer.

No API, WebSocket, persistence, workflow, revision, artifact, or authorization contract changed.

## Test evidence

| Layer | Command | Result | Classification |
| --- | --- | --- | --- |
| Logic | focused responsive and Agent regression suite | PASS, 15/15 | `PASS` |
| Logic | full frontend `tests/*.test.ts` suite | PASS, 148/148 | `PASS` |
| Static | `npm.cmd run lint` | PASS | `PASS` |
| Build | `npm.cmd run build` | PASS | `PASS` |
| Static | `git diff --check` | PASS with existing line-ending warnings | `PASS` |
| Product | authenticated browser interaction at responsive widths | Not executed | `BLOCKED` |

## Limitation

No authenticated browser screenshot or focus/visibility assertion was executed. The change is closed at source, regression-test, lint, and production-build levels; a future browser acceptance should exercise `1023px`, `900px`, `899px`, and `760px` with the project sidebar open before Ask Agent is invoked.
