# Vertical Conversations and Typography Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert engineering conversations to a functional vertical list and normalize production frontend typography without changing any other UI or business behavior.

**Architecture:** Keep `PanelTabs` connected to the existing Zustand session store and replace only its layout/semantics with an existing-sidebar vertical navigation. Define one semantic typography scale in `index.css`, then migrate production component size utilities and raw CSS declarations to those tokens while preserving all non-typographic styles.

**Tech Stack:** React 19, TypeScript 6, Zustand, Tailwind CSS 3, Vite 8, Node test runner

**Constraints:** Do not modify `isearch-ai-pr-platform`; do not commit or push; do not change API, route, store, CAD, Agent, inspector, DFM, settings, modal, form, color, spacing, component dimension, or breakpoint behavior outside the approved vertical conversation layout and typography properties.

---

## File Map

- Create `frontend/tests/vertical-conversations-typography.test.ts`: static regression coverage for vertical-list structure and the typography allowlist.
- Modify `frontend/src/components/PanelTabs.tsx`: preserve session actions while rendering the vertical conversation navigation and keeping the active/new row visible.
- Modify `frontend/src/index.css`: add semantic typography tokens/utilities, add conversation-list layout styles, and map existing raw font declarations to the approved scale.
- Modify the production component files listed in Task 4: replace only font-size/line-height/weight utilities with semantic typography classes.
- Modify `frontend/src/i18n/I18nProvider.tsx`: reuse its existing `工程线程 → Engineering threads` and `新建对话 → New conversation` entries, and add the missing rename-input accessible label pair required by the vertical list.
- Exclude only the Drei `<Html>` annotation label inside `frontend/src/components/ModelAnnotations.tsx` from the DOM typography migration because it is rendered CAD-canvas content; continue scanning all viewer DOM chrome.

## Task 1: Add Regression Coverage

**Files:**
- Create: `frontend/tests/vertical-conversations-typography.test.ts`

- [ ] **Step 1: Add a failing vertical-conversation structure test**

  Assert that `PanelTabs.tsx` uses the existing `useI18n` translation mechanism for labelled navigation/list semantics, renders the new-conversation action before the row collection, marks the active row with `aria-current`, and no longer contains `overflow-x-auto`, `shrink-0`, or inline horizontal `flex ... items-center` tab layout. Also assert that `ProjectSidebar.tsx` keeps `<PanelTabs />` inside the existing `.ww-sidebar-section` immediately following the engineering-thread heading.

  ```ts
  test("engineering conversations render as a vertical navigation", () => {
    const panelTabs = read("src/components/PanelTabs.tsx");
    assert.match(panelTabs, /useI18n/);
    assert.match(panelTabs, /aria-label=\{translate\("工程线程"\)\}/);
    assert.match(panelTabs, /ww-conversation-list/);
    assert.match(panelTabs, /aria-current=\{isActive \? "page" : undefined\}/);
    assert.doesNotMatch(panelTabs, /overflow-x-auto/);
  });
  ```

- [ ] **Step 2: Add a failing CSS behavior test**

  Assert that `.ww-conversation-list` is column-oriented, capped at `min(240px, 32vh)`, uses vertical auto overflow, and hides horizontal overflow.

  Also assert that `I18nProvider.tsx` contains the existing exact pairs `["工程线程", "Engineering threads"]` and `["新建对话", "New conversation"]`, and that `PanelTabs` routes both strings through `translate(...)`.

- [ ] **Step 3: Add a failing typography allowlist test**

  Recursively scan production-owned `frontend/src/**/*.tsx` and `frontend/src/**/*.css`. In TSX, reject Tailwind size, line-height, and weight utilities other than the font-family-only `font-mono`; reject arbitrary size/line/weight utilities and React inline `fontSize`, `lineHeight`, or `fontWeight`. Exclude only `ModelAnnotations.tsx` inline font properties, which belong to the Drei annotation rendered inside the CAD canvas; do not exclude `Viewer2D.tsx`, `Viewer3D.tsx`, or any viewer DOM chrome. In CSS, reject `font` shorthand and require every `font-size`, `line-height`, and `font-weight` declaration to reference an approved semantic variable, except the semantic token definitions themselves. This verifies complete semantic triplets, not just pixel size.

- [ ] **Step 4: Run the new test and verify failure**

  Run:

  ```bash
  cd frontend
  node --experimental-strip-types --test tests/vertical-conversations-typography.test.ts
  ```

  Expected: FAIL because `PanelTabs` is horizontal and current production files still contain ad hoc typography sizes.

## Task 2: Convert Conversations to a Vertical List

**Files:**
- Modify: `frontend/src/components/PanelTabs.tsx`
- Modify: `frontend/src/index.css`
- Test: `frontend/tests/vertical-conversations-typography.test.ts`

- [ ] **Step 1: Replace only the `PanelTabs` presentation structure**

  Keep `panels`, `activePanelId`, `switchPanel`, `addPanel`, `removePanel`, and `renamePanel` unchanged. Add the existing `useI18n()` hook and use `translate(...)` for every new visible, title, and ARIA string. Render this structure:

  ```tsx
  <nav aria-label={translate("工程线程")} className="ww-conversation-navigation">
    <button className="ww-conversation-create type-control" onClick={() => addPanel()} type="button">
      <span>{translate("新建对话")}</span>
      <Icon name="plus" size={16} />
    </button>
    <div className="ww-conversation-list" role="list">
      {panels.map((panel) => (
        <div className={`ww-conversation-row group ${isActive ? "is-active" : ""}`} role="listitem">
          {/* Preserve the existing rename input, generation spinner, and close action. */}
        </div>
      ))}
    </div>
  </nav>
  ```

- [ ] **Step 2: Preserve keyboard and pointer behavior**

  Keep double-click rename, Enter/blur commit, Escape cancel, click switch, and click close. Add `aria-current={isActive ? "page" : undefined}` to the switch button and an explicit rename input label. Use native Tab/Enter/Space behavior; do not add arrow-key handlers.

  Confirm the dictionary pairs already present in `I18nProvider.tsx`; do not duplicate them. The focused test must verify the pairs and browser verification must confirm the rendered Chinese and English labels after using the real language switch.

- [ ] **Step 3: Keep the active conversation visible**

  Attach refs to the row collection and active row. When `activePanelId` or `panels.length` changes, compare the active row's top/bottom offsets with the collection's `scrollTop` and `clientHeight`, then update only `collection.scrollTop` enough to reveal it. Do not call `scrollIntoView`, scroll an ancestor, alter store state, or change conversation ordering.

- [ ] **Step 4: Add scoped conversation CSS**

  Add only `.ww-conversation-*` rules: full-width create action; one-column rows; `max-height: min(240px, 32vh)`; `overflow-y: auto`; `overflow-x: hidden`; existing token colors; active soft-purple treatment; generation and close affordances. Do not alter sidebar width, breakpoints, surrounding navigation, or history layout.

- [ ] **Step 5: Run the focused vertical-list test**

  Run the Task 1 Node command. Expected: conversation assertions PASS; typography allowlist remains FAIL until Tasks 3–4.

## Task 3: Define and Apply the Typography Tokens in CSS

**Files:**
- Modify: `frontend/src/index.css`
- Test: `frontend/tests/vertical-conversations-typography.test.ts`

- [ ] **Step 1: Define the approved tokens**

  Add the following variables to the existing root token block:

  ```css
  --type-caption-size: 11px;
  --type-caption-line: 16px;
  --type-caption-weight: 500;
  --type-body-size: 13px;
  --type-body-line: 20px;
  --type-body-weight: 400;
  --type-control-weight: 550;
  --type-section-size: 15px;
  --type-section-line: 22px;
  --type-section-weight: 650;
  --type-page-size: 18px;
  --type-page-line: 26px;
  --type-page-weight: 650;
  --type-display-size: 22px;
  --type-display-line: 30px;
  --type-display-weight: 650;
  ```

- [ ] **Step 2: Define six semantic utilities**

  Add `.type-caption`, `.type-body`, `.type-control`, `.type-section-heading`, `.type-page-heading`, and `.type-display-heading`. Each utility must set its complete size, line-height, and weight triplet through the variables above.

- [ ] **Step 3: Normalize raw CSS typography declarations**

  For every existing `font-size`, `line-height`, and `font-weight` in `frontend/src/index.css`, select the semantic role and set the complete matching size/line-height/weight triplet. Replace any `font` shorthand with explicit family/style properties plus the semantic triplet. Do not modify any non-typographic declaration. Keep the existing system/PingFang font stack and monospace families.

- [ ] **Step 4: Run the allowlist test**

  Expected: raw CSS assertions PASS; TSX allowlist assertions remain FAIL until Task 4.

## Task 4: Migrate Production Component Typography

**Files:**
- Modify: `frontend/src/components/AccountPanel.tsx`
- Modify: `frontend/src/components/AgentRunTimeline.tsx`
- Modify: `frontend/src/components/CapabilityCatalog.tsx`
- Modify: `frontend/src/components/CapabilityRunner.tsx`
- Modify: `frontend/src/components/DFMRuleConfig.tsx`
- Modify: `frontend/src/components/DesignBriefPanel.tsx`
- Modify: `frontend/src/components/InspectReportPanel.tsx`
- Modify: `frontend/src/components/LoginPage.tsx`
- Modify: `frontend/src/components/RepairHistory.tsx`
- Modify: `frontend/src/components/SuggestionPills.tsx`
- Modify: `frontend/src/components/VersionHistoryPanel.tsx`
- Modify: `frontend/src/components/Viewer2D.tsx`
- Modify: `frontend/src/components/Viewer3D.tsx`
- Modify: `frontend/src/components/changes/ChangeSetDialog.tsx`
- Modify: `frontend/src/components/common/BrandMark.tsx`
- Modify: `frontend/src/components/common/WorkspaceOverlay.tsx`
- Modify: `frontend/src/components/export/ExportDialog.tsx`
- Modify: `frontend/src/components/parameters/ParameterDrawer.tsx`
- Modify: `frontend/src/components/project/ProjectFlow.tsx`
- Modify: `frontend/src/components/project/ProjectSidebar.tsx`
- Modify: `frontend/src/components/project/ProjectStart.tsx`
- Modify: `frontend/src/components/validation/ValidationDialog.tsx`
- Modify: `frontend/src/components/viewer/MechanicalWorkspace.tsx`
- Modify: `frontend/src/components/workspace/EngineeringWorkspace.tsx`
- Modify: `frontend/src/components/KnowledgeGraph.tsx`
- Test: `frontend/tests/vertical-conversations-typography.test.ts`

- [ ] **Step 1: Replace caption and metadata sizes**

  Replace 8–11 px arbitrary utilities used by timestamps, badges, hints, labels, IDs, and secondary metadata with `type-caption`. Remove conflicting `leading-*` and `font-*` utilities except `font-mono`. Preserve color, spacing, tracking, case, and truncation classes.

- [ ] **Step 2: Replace body and control sizes**

  Replace `text-xs`, `text-sm`, and applicable 12–14 px arbitrary utilities with `type-body` for copy/state text and `type-control` for buttons, labels, inputs, selects, navigation rows, and other interaction text. Remove conflicting line-height and weight utilities so every role resolves to the complete semantic triplet.

- [ ] **Step 3: Replace heading sizes**

  Apply `type-section-heading` to panel/card/modal headings, `type-page-heading` to page headings, and `type-display-heading` only to the existing login/product display heading. Remove all conflicting size/line-height/weight utilities.

- [ ] **Step 4: Preserve technical font families**

  Keep every existing `font-mono` or equivalent family class, while applying the semantic size utility appropriate to code, CAD parameter, dimension, request ID, JSON, and technical output roles.

- [ ] **Step 5: Audit production typography declarations**

  Run:

  ```bash
  rg -n --glob 'src/**/*.{tsx,ts,css}' --glob '!src/components/ModelAnnotations.tsx' '(font(?:-size|-weight)?\s*:|line-height\s*:|text-(xs|sm|base|lg|xl|2xl|3xl)|text-\[[^]]+\]|leading-|font-(thin|extralight|light|normal|medium|semibold|bold|extrabold|black)|font(Size|Weight)|lineHeight)' src
  ```

  Expected: only approved semantic variable definitions/usages and permitted `font-mono` family classes remain; no component-local ad hoc size, line-height, or weight utilities/styles remain. The command deliberately excludes only `ModelAnnotations.tsx`, matching the automated test's CAD-canvas annotation exception; manually confirm that no other file is excluded.

- [ ] **Step 6: Run the focused regression test**

  Run the Task 1 Node command. Expected: PASS.

## Task 5: Automated Regression

**Files:**
- Verify only; no planned production changes

- [ ] **Step 1: Run frontend Node tests**

  Run:

  ```bash
  cd frontend
  node --experimental-strip-types --test tests/*.test.ts
  ```

  Expected: all tests PASS.

- [ ] **Step 2: Run ESLint**

  Run `npm run lint`. Expected: exit 0.

- [ ] **Step 3: Run TypeScript and production build**

  Run `npm run build`. Expected: `tsc -b` and Vite build exit 0.

- [ ] **Step 4: Check whitespace and unintended files**

  Run `git diff --check` and inspect `git diff -- frontend/src frontend/tests`. Expected: no whitespace errors and only typography plus vertical-conversation changes.

## Task 6: Real Browser Verification

**Files:**
- Verify only; production fixes are allowed only for failures directly caused by the two approved changes

- [ ] **Step 1: Start the real local frontend and backend configuration already used by the project**

  Use the existing environment and real application shell; do not add mock/test-only production branches.

- [ ] **Step 2: Verify the conversation interactions**

  At desktop and mobile sidebar widths, add enough real conversations to overflow the collection. Verify downward append, newest-row visibility, vertical scroll, no horizontal scroll, switch, double-click rename, Enter/blur commit, Escape cancel, close, and visible generation state.

- [ ] **Step 3: Verify typography at representative widths and surfaces**

  Inspect 1440 px, 1279 px, 1181 px, 760 px, and 390 px in Chinese and English. Cover login/auth, project start/workspace, Agent, inspector, DFM, settings, account/dialog/form, loading/error/empty states, and a long conversation name. Verify type hierarchy, clipping, unintended wrapping, and that non-typographic visual properties remain unchanged.

  In both locales, explicitly verify the rendered engineering-thread navigation label, new-conversation button text/title/ARIA label, and close/rename accessible names. Expected English output includes “Engineering threads” and “New conversation”.

- [ ] **Step 4: Check browser console and network behavior**

  Exercise the existing primary workflow far enough to confirm routes and API requests behave as before. Expected: no new console errors, failed frontend assets, duplicate requests, or route regressions caused by these changes.

- [ ] **Step 5: Record final evidence**

  Record commands, pass/fail counts, tested viewport widths, and any environment limitation. Do not claim complete verification for a flow that could not be exercised with real services.
