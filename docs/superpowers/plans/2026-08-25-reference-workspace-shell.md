# Reference Workspace Shell Migration Implementation Plan

> **For Codex:** Execute this plan with the `executing-plans` workflow. The reference HTML controls presentation; existing React code controls behavior.

**Goal:** Incrementally replace the current project workspace presentation with the reference HTML's compact application shell, global sidebar, header, and three-column workspace while preserving every existing backend, API, WebSocket, CAD viewer, authentication, and state-management path.

**Architecture:** Keep `EngineeringWorkspace` as the orchestration boundary and keep the current service/store/hooks unchanged. Add presentational shell and agent panel components, restyle the sidebar/header, and mount the existing project flow, chat behavior, `Viewer3D`/`Viewer2D`, history, and dialogs inside the new layout. Responsive behavior is CSS-driven; panel collapse state remains local UI state.

**Tech Stack:** React 19, TypeScript, Vite, Tailwind CSS, Zustand, React Three Fiber / Three.js.

**Reference:** `/Users/wentao/Downloads/前端.html`

## Non-negotiable boundaries

- Do not modify backend code or API schemas.
- Do not rewrite `useWebSocket`, CAD viewer implementation, service/adapters, auth, store schema, or business types.
- Do not add mock responses, placeholder data, static CAD output, or fake interactions from the reference HTML.
- Preserve all real actions: history restore/delete, Agent send/confirm, checks, change sets, export, settings, parameter execution, version restore, and project navigation.
- Run typecheck and production build after every implementation phase; fix failures before continuing.

### Task 1: Add the presentational application shell

**Files:**
- Create: `frontend/src/components/workspace/WorkspaceShell.tsx`
- Modify: `frontend/src/index.css`
- Modify: `frontend/src/components/workspace/EngineeringWorkspace.tsx`

**Steps:**
1. Add a pure `WorkspaceShell` component with slots for sidebar, header, Agent pane, primary workspace, and inspector pane.
2. Implement desktop three-column sizing, keyboard-accessible drag handles, and local collapse controls without changing business state.
3. Add the reference-derived neutral tokens, borders, compact spacing, and responsive shell CSS.
4. Mount existing overview content and version history in the new shell without altering callbacks or data sources.
5. Run `npx tsc --noEmit -p tsconfig.app.json` and `npm run build` in `frontend/`; fix all failures.

### Task 2: Restyle the global sidebar and compact header

**Files:**
- Modify: `frontend/src/components/project/ProjectSidebar.tsx`
- Modify: `frontend/src/components/project/WorkspaceHeader.tsx`
- Modify: `frontend/src/components/workspace/EngineeringWorkspace.tsx`
- Modify: `frontend/src/index.css`

**Steps:**
1. Match the reference navigation hierarchy using only current real routes/domains and history data.
2. Keep loading, empty, error, retry, restore, and delete behavior intact.
3. Compact the header while retaining current Agent, changes, checks, export, settings, account, connection, branch, and back actions.
4. Verify keyboard labels and mobile navigation overlay.
5. Run typecheck and build; fix all failures.

### Task 3: Extract and embed the existing Agent interaction

**Files:**
- Create: `frontend/src/components/agent/AgentPanel.tsx`
- Modify: `frontend/src/components/agent/AgentDrawer.tsx`
- Modify: `frontend/src/components/workspace/EngineeringWorkspace.tsx`
- Modify: `frontend/src/index.css`

**Steps:**
1. Move the existing message, suggestion, confirmation, progress, artifact, and error UI into a reusable embedded panel without changing send behavior.
2. Keep the drawer wrapper for narrow screens and suggested prompts.
3. Embed the panel in the left workspace column on desktop, using the same `sendMessage` function and Zustand session state.
4. Verify connection states, disabled states, plan confirmation, and current-domain context are unchanged.
5. Run typecheck and build; fix all failures.

### Task 4: Embed the existing CAD viewer and real inspector content

**Files:**
- Modify: `frontend/src/components/viewer/MechanicalWorkspace.tsx`
- Modify: `frontend/src/components/workspace/EngineeringWorkspace.tsx`
- Modify: `frontend/src/index.css`

**Steps:**
1. Place the unchanged lazy `Viewer3D` and existing `Viewer2D` in the primary center pane.
2. Restyle only the surrounding toolbar, status, loading, and empty/error surfaces.
3. Map the inspector to real current-project data only: parameters open the existing parameter editor, versions use `VersionHistoryPanel`, generated code comes from `GenerationResult.code`, and available files/artifacts come from the current result/store. Provide the same actions through the mobile drawer; render honest empty states when a real source is absent.
4. Do not expose fake viewer tools or reference HTML static CAD data.
5. Run typecheck and build; fix all failures.

### Task 5: Responsive and regression verification

**Files:**
- Modify as needed only within the presentation files above.

**Steps:**
1. Verify desktop widths (1440, 1280), laptop, tablet, and mobile CSS behavior in a real browser.
2. Exercise the browser-visible paths for Agent request review/confirmation, parameter entry, version history/restore availability, checks, changes, export, navigation, and mobile panel access. Where a live backend prerequisite is unavailable, verify the real error/disabled path and never substitute mock data.
3. Run `npm run lint`, the existing frontend Node test suite, typecheck, and production build.
4. Inspect the final diff to confirm no protected business files or backend files changed.
5. Report completed phases, verification evidence, changed files, and remaining UI-only follow-ups.
