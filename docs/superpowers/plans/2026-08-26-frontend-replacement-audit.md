# Frontend Replacement Audit Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Determine from source-level dependency evidence and real browser execution whether `/Users/wentao/Downloads/前端.html` has fully replaced the former frontend, remove only proven-unused legacy frontend code/assets, and verify preserved routes, APIs, and interactions.

**Architecture:** Treat the reference HTML as the presentation contract and the current `frontend/` React application as the behavior contract. Build two independent evidence sets, a static reachability/import graph and a runtime desktop/mobile interaction matrix, then change only presentation files directly implicated by confirmed findings. Exclude `isearch-ai-pr-platform/` entirely and do not commit or push.

**Tech Stack:** React 19, TypeScript 6, Vite 8, Zustand, React Three Fiber/Three.js, Node test runner, gstack browse/Chromium.

---

## Evidence and verdict contract

- Capture the dirty-worktree baseline before edits: `git status --porcelain=v1 -- frontend`, scoped diffs, untracked files, and SHA-256 hashes for the reference, entry points, and main stylesheet. Save the exact baseline path/status/hash allowlist and compare it with the final tree so audit-introduced edits and failures are distinguishable from pre-existing user changes. Never stash, reset, clean, commit, or push.
- Keep two verdicts: **as found** and **after audit fixes**. Freeze initial screenshots, console/network logs, and static findings before changing files. Generate a UTC run ID plus random nonce, create `.gstack/qa-reports/frontend-replacement-<run-id>-<nonce>/` with a create-only preflight, and fail rather than overwrite an existing evidence directory.
- Use a traceability matrix. Every row records reference region/control, current behavior owner, route/state, preconditions, viewport, locale, action, DOM assertion, screenshot, console result, request/response evidence, and pass/fail/blocked status.
- A blocked or untested production behavior prevents a 100% verdict. Reference-only mock controls are explicitly classified as `presentation contract`, `real product equivalent`, or `intentionally not implemented because the reference itself labels them mock/future integration`.
- The application has no client router dependency. Prove this from `App.tsx` and package/import analysis, then test its actual entry-state router: auth bootstrap, login, owner/session restore, empty workspace, populated workspace, and invalid/expired authentication. Also test refresh, query/hash tolerance, and back/forward behavior.
- Render the reference HTML itself and capture paired screenshots with the application at 1440x900, 1280x720, 1181/1180/1179px, 768x1024, 761/760/759px, 375x812, and rotated mobile dimensions. Acceptance criteria are shell regions, hierarchy, typography/tokens, responsive panel strategy, and real-control equivalence, not pixel identity with mock CAD data.
- Static reachability starts from every production root found in `frontend/index.html`, Vite config, CSS `url()`, lazy/dynamic imports, workers/service workers, `import.meta.glob`, `public/`, and runtime filename lookup, not only `main.tsx`.
- Every deletion candidate gets a manifest with tracked/untracked status, pre-existing modification status, all-reference search, import-graph result, build-manifest result, observed runtime requests, and post-deletion tests/build. Never delete untracked or already-modified candidates.
- Use disposable local data only. Do not delete or restore an existing user project during destructive-flow checks. If isolated disposable data, credentials, or required services are unavailable, verify the disabled/error path and mark the real destructive success path blocked.

### Task 1: Establish the reference and application inventory

**Files:**
- Read: `/Users/wentao/Downloads/前端.html`
- Read: `frontend/src/App.tsx`
- Read: `frontend/src/main.tsx`
- Read: `frontend/src/index.css`
- Read: `frontend/src/components/**/*.tsx`
- Read: `frontend/package.json`

- [ ] **Step 1: Extract the reference DOM regions, design tokens, text, responsive rules, dialogs, forms, and scripted interactions.**

Run: `rg -n "<main|<aside|<header|class=|@media|addEventListener|function |const " '/Users/wentao/Downloads/前端.html'`

Expected: A concrete reference checklist, not an inference from filenames.

- [ ] **Step 2: Enumerate every application entry point, conditional page, overlay, drawer, dialog, and viewer state.**

Run: `rg --files frontend/src frontend/tests`

Expected: All reachable and hidden UI states have an owner file.

- [ ] **Step 3: Record the audit matrix under the create-only run directory as `qa-report.md`; never reuse a fixed report name.**

### Task 2: Prove static reachability and legacy residue

**Files:**
- Read: `frontend/src/**/*`
- Read: `frontend/index.html`
- Read: `frontend/vite.config.*`
- Read: `frontend/tsconfig*.json`
- Read: `frontend/public/**/*` when present
- Read: `frontend/qa/**/*`
- Read: `frontend/tests/**/*`

- [ ] **Step 1: Trace every production root, including HTML, CSS URLs, lazy/dynamic imports, workers, `public/`, and runtime filenames; identify unreachable files/assets.**

Run: execute the checked audit script saved for this run, starting from every `<script type="module">` and stylesheet root in `frontend/index.html`; resolve relative imports plus Vite/TypeScript aliases, `React.lazy`, dynamic `import()`, `import.meta.glob`, worker/service-worker constructors, CSS `@import`/`url()`, and `public/` runtime paths. Save the sorted reachable/unreachable sets and detected roots to `<run-dir>/logs/import-graph.json`. Save the CSS selector/reference cross-check to `<run-dir>/logs/css-reachability.json`.

Expected: the report links the exact command/script and immutable output; no candidate is classified from an ad-hoc `rg` result alone.

- [ ] **Step 2: Search for reference-era and legacy-era tokens, class names, copy, CSS blocks, placeholder assets, duplicated shell components, and direct imports.**

Run: `rg -n "legacy|old|deprecated|react\.svg|vite\.svg|hero\.png|ProjectStart|EngineeringWorkspace|WorkspaceShell" frontend --glob '!node_modules/**' --glob '!dist/**'`

- [ ] **Step 3: Create a deletion-candidate manifest and require tracked, unmodified, no-reference, no-build-manifest, no-runtime-request, and post-deletion regression evidence before editing.**

Expected: No file is deleted solely because its name or appearance looks old.

### Task 3: Execute the runtime browser matrix

**Files:**
- Read: `docker-compose.yml`
- Read: `docs/development.md`
- Create: `.gstack/qa-reports/screenshots/frontend-replacement-*`

- [ ] **Step 1: Start the documented frontend/backend dependencies without mock success responses.**

Run from `frontend/`: `npm run dev -- --host 127.0.0.1 --port 5173`

Run from repository root when configured: `docker compose up -d postgres minio minio-init temporal migrate backend workflow-worker`

URLs: application `http://127.0.0.1:5173`; reference `file:///Users/wentao/Downloads/前端.html`; backend health `http://127.0.0.1:8000/health`. Record existing-process detection and tear down only processes started by this audit.

- [ ] **Step 2: Test auth bootstrap, unauthenticated/authenticated entry, expired session, owner restore, empty workspace, populated workspace, refresh, query/hash, browser back/forward, loading, REST failure, 403/404-equivalent service states, and real error-boundary activation.**

- [ ] **Step 3: Exercise Agent Enter/Shift+Enter/shortcut, disabled/error/confirmation states, artifact preview/use-version equivalent, project history, viewer controls, inspector tabs, parameter success/failure/reset, clipboard denial, every export format, settings, account, and every dialog/drawer close path.**

- [ ] **Step 4: Repeat paired reference/application checks at 1440x900, 1280x720, 1181/1180/1179px, 768x1024, 761/760/759px, 375x812, and 812x375. Test collapse controls, splitter min/max, keyboard resizing, focus restoration, outside-click/Escape close, mobile navigation/Agent/inspector drawers, and live resize/orientation.**

- [ ] **Step 5: Disconnect and reconnect the real WebSocket and verify reconnect/replay, project-switch stale-response isolation, and active-artifact consistency during generation. Capture standardized screenshot, DOM, console, and request evidence for every matrix row.**

Expected: Every conclusion is backed by observed DOM, interaction, console, or network behavior.

### Task 4: Fix confirmed findings and remove proven dead legacy files

**Files:**
- Modify: Only `frontend/` files directly tied to a reproduced issue.
- Test: Add new regression tests under `frontend/tests/` only for behavioral regressions.

- [ ] **Step 1: Reproduce each issue twice, save immutable as-found evidence, and publish the as-found verdict before editing.**

- [ ] **Step 2: Apply the smallest fix with `apply_patch`, preserving service, adapter, store, WebSocket, route, and backend contracts.**

- [ ] **Step 3: Delete a legacy file/resource only when its candidate manifest passes every evidence gate and the file was tracked and unmodified at baseline.**

- [ ] **Step 4: Re-run the affected browser path and save after evidence.**

Expected: No commits or pushes; the working tree remains reviewable as the user's redesign plus narrowly scoped audit fixes.

### Task 5: Run final regression gates and report the verdict

**Files:**
- Create: `<run-dir>/qa-report.md`
- Create: `<run-dir>/baseline.json`
- Create: `<run-dir>/final-worktree.json`

- [ ] **Step 1: Run frontend tests.**

Run from `frontend/`: `node --test --experimental-strip-types tests/*.test.ts`

Expected: All tests pass.

- [ ] **Step 2: Run lint, TypeScript/build, and diff hygiene.**

Run from `frontend/`: `npm run lint && npx tsc --noEmit -p tsconfig.app.json && npm run build`

Run from repository root: `git diff --check -- frontend`

Expected: All commands exit 0.

- [ ] **Step 3: Reconcile the final frontend worktree with `baseline.json`.**

Record every final modified/untracked/deleted path and hash. Require each delta not present at baseline to be either an explicitly documented audit fix/deletion or generated evidence under the unique run directory. Label any check that already failed before audit edits as pre-existing; label any new failure as audit-introduced and resolve it before completion.

- [ ] **Step 4: Repeat all affected runtime paths on desktop and mobile, with zero unexplained console errors or failed first-party requests.**

- [ ] **Step 5: State `100% complete` only if every static and runtime matrix item passes; otherwise name the exact blockers and return a non-100% verdict.**
