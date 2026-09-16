# CAD-Agent Review: 2026-09-16 7e1aa85

## Conclusion

- Status: `FAIL`
- Review range: current `HEAD` (`7e1aa85`), focused behavioral investigation rather than a commit-range review
- Working tree state: repository-shared review Skill and documentation changes are present; no frontend business-code change was made by this review
- Business code modified by reviewer: `No`

The reported issue is confirmed. When the project sidebar is opened as a responsive overlay, asking the Agent does not consistently expose an Agent surface above or instead of that sidebar. The defect is caused by mismatched responsive behavior and by `askAgent` leaving the mobile sidebar open.

## Change scope

- Change class: existing responsive-layout regression
- User-described goal: check why the Ask Agent dialog is not visible while the left toolbar/sidebar is expanded
- Investigated paths:
  - `frontend/src/components/workspace/EngineeringWorkspace.tsx`
  - `frontend/src/components/workspace/WorkspaceShell.tsx`
  - `frontend/src/components/project/ProjectSidebar.tsx`
  - `frontend/src/components/project/WorkspaceHeader.tsx`
  - `frontend/src/components/agent/AgentDrawer.tsx`
  - `frontend/src/components/common/WorkspaceOverlay.tsx`
  - `frontend/src/index.css`
  - responsive and Agent frontend regression tests
- Functional inventory domains: `03.01-03.10` workspace/layout and `05.01-05.12` Agent collaboration

## Implementation trace

| Symbol or contract | Implementation path | Consumers | Impact judgment |
| --- | --- | --- | --- |
| `mobileSidebar` | `EngineeringWorkspace.tsx` | `ProjectSidebar.mobileOpen` | Opening the responsive sidebar creates a fixed overlay, but `askAgent` does not close it. |
| `askAgent` | `EngineeringWorkspace.tsx` | header, mechanical workspace, validation dialog, change-set dialog | It expands the embedded Agent pane and opens `AgentDrawer` only below 900 px; it never calls `setMobileSidebar(false)`. |
| responsive sidebar overlay | `index.css` `@media (max-width: 1023px)` | `ProjectSidebar` | The open sidebar is fixed at `z-index: 50` and covers the left side of the workspace. |
| `AgentDrawer` | `WorkspaceOverlay.tsx` | `AgentDrawer.tsx` | The drawer is `z-index: 70`, so it can appear over the sidebar only when `agentOpen` is actually set. |
| embedded Agent pane | `WorkspaceShell.tsx` | `AgentPanel` | It has no overlay z-index and remains underneath an open responsive sidebar. |

## Findings

### Medium — responsive sidebar can hide the requested Agent surface

- At viewport widths `<=1023px`, `.workspace-sidebar` becomes a fixed overlay with `z-index: 50`.
- `askAgent` opens `AgentDrawer` only for widths `<=899px` and returns to the embedded Agent-first mobile layout at `<=760px`.
- `askAgent` never closes `mobileSidebar`.
- Therefore:
  - At `900-1023px`, asking the Agent only expands the embedded left Agent pane, which remains behind the open sidebar.
  - At `<=760px`, the workspace returns to the full-screen embedded Agent pane, but the still-open sidebar remains above it.
  - At `761-899px`, the drawer normally appears above the sidebar because its z-index is higher; this range works by layering rather than by closing the conflicting navigation state.
- This explains why the Agent dialog/pane appears not to open while the left toolbar is expanded.

### Test coverage gap

Existing responsive tests verify pane widths, the `760px` Agent-first preview mode, and removal of the sidebar grid track. They do not exercise the state combination:

```text
viewport <= 1023px
mobileSidebar = open
askAgent()
Agent surface is visible and actionable
```

Consequently, all selected tests pass while the reported interaction remains broken.

### Encoding and fake implementation review

- No new business implementation was under review.
- No evidence indicates a fake Agent submission path; the failure occurs before interaction with the existing Agent composer becomes visible.
- Mojibake visible in some PowerShell-rendered output was terminal decoding behavior; repository files inspected through UTF-8 reads contain the intended Chinese strings.

### Compatibility review

- Frontend/backend contract: unaffected; the problem is frontend visibility/state coordination.
- WebSocket/event contract: unaffected before submission.
- Persistence/migration/RLS: not involved.
- Workflow/retry/cancellation: not involved.
- Revision/candidate/draft/artifact identity: not involved.
- Permissions and external boundaries: not involved.

## Test evidence

| Layer | Command or test | Result | Classification | Notes |
| --- | --- | --- | --- | --- |
| Logic | `node --test --experimental-strip-types tests/responsive-intermediate.regression-1.test.ts tests/frontend-replacement-fixes.regression-1.test.ts tests/agent-context-isolation.regression-1.test.ts` | PASS, 13/13 | `CONTRACT_ONLY` | Static regression tests pass but do not cover an open responsive sidebar plus `askAgent`. |
| Logic | `python .agents/skills/cad-agent-review/scripts/validate_feature_map.py` | PASS | `PASS` | 24 domains and 281 referenced paths validated. |
| Logic | `git diff --check` | PASS with line-ending warning | `PASS` | No whitespace error; warning concerns future LF-to-CRLF conversion of `docs/README.md`. |
| Product | Browser interaction at `<=1023px` with sidebar open | Not executed | `BLOCKED` | No already-running authenticated product fixture was used; the source-state conflict is sufficient to confirm the reported defect, but no screenshot evidence was produced. |

## Gaps and blocked prerequisites

- No real browser screenshot or DOM visibility assertion was produced in this review.
- A regression test should cover at least `1023px`, `900px`, `899px`, and `760px` with the sidebar already open before invoking Ask Agent.
- The implementation should establish one invariant: invoking Ask Agent must either close the responsive project sidebar or display an Agent overlay above it at every responsive breakpoint.
- This report does not prescribe or apply the business-code fix.

## Knowledge update

- Review report: `docs/qa/code-review/2026-09-16-7e1aa85-agent-sidebar-review.md`
- Review memory: `.agents/skills/cad-agent-review/references/review-memory/2026-09-16-7e1aa85-agent-sidebar.md`
- Stable map/invariant update: review-memory entry only
- Reason and evidence: the sidebar overlay breakpoint (`1023px`) and Agent presentation breakpoints (`899px` and `760px`) do not share a visibility invariant, and existing tests omit the combined state.
