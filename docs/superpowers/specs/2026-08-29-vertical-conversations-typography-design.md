# Vertical Conversations and Typography Design

## Goal

Make only two frontend changes:

1. Replace the horizontal engineering-conversation tabs with a compact vertical conversation list where new conversations are added downward.
2. Replace inconsistent frontend typography with one semantic type scale.

No other visual, behavioral, architectural, routing, API, or business-logic changes are in scope.

## Approved Visual Direction

### Conversation list

- Use the approved compact vertical-list direction.
- Render the list inside the existing `ProjectSidebar` engineering-thread section (`.ww-sidebar-section`): the existing 228 px desktop sidebar and existing mobile drawer remain its only hosts. “Full width” means the current content width of that section; no new rail or workspace region is introduced.
- Keep the existing `PanelTabs` data source and actions: add, switch, rename, close, and generation status.
- Put a full-width “new conversation” action above the conversation rows.
- Render each conversation as one full-width row and append newly created conversations below existing rows.
- Show the active conversation with the existing agent-purple soft background and text treatment.
- Keep the existing generation spinner, double-click rename behavior, and close action.
- Limit the row collection to `min(240px, 32vh)` and use vertical overflow inside that collection. The heading and new-conversation action remain visible; horizontal overflow is disabled.
- Use the same vertical behavior in the existing mobile sidebar. Do not change sidebar breakpoints, widths, overlay behavior, or surrounding navigation.
- Expose the section as a labelled navigation/list rather than ARIA tabs. The active row uses `aria-current`; native Tab, Enter, and Space behavior remains. Do not add custom arrow-key interaction. Rename keeps its current autofocus, Enter commit, Escape cancel, and blur commit behavior.

### Typography

- Apply the approved balanced type system throughout the frontend:
  - Caption: 11 px / 16 px line height / weight 500 for timestamps, helper text, and secondary metadata.
  - Body: 13 px / 20 px line height / weight 400 for body copy, list content, empty states, and errors.
  - Control: 13 px / 20 px line height / weight 550 for navigation labels, buttons, inputs, and other interactive labels.
  - Section heading: 15 px / 22 px line height / weight 650 for card, panel, drawer, modal, and section headings.
  - Page heading: 18 px / 26 px line height / weight 650.
  - Display heading: 22 px / 30 px line height / weight 650, only where the interface already has a display-level heading.
- Use those exact line heights and weights instead of preserving arbitrary component-local values.
- Use the existing system sans-serif stack with PingFang SC preferred for Chinese.
- Preserve the existing monospace family for code, CAD parameters, dimensions, and technical output, but map its size, line height, and weight to the semantic scale above. The exception is font family only.
- Migrate existing frontend components from ad hoc sizes such as 9, 10, and 12 px according to their semantic role.
- Do not change colors, spacing, borders, radii, shadows, component dimensions, layout structure, or responsive breakpoints as part of typography migration.
- Preserve existing truncation and wrapping rules. If inspection finds that typography alone clips text inside an existing fixed-height control, choose the smaller applicable approved semantic level; do not resize the control.

## Implementation Boundaries

- Reuse the current session store and existing component behavior.
- Do not change API calls, route definitions, authentication, history restoration, CAD generation, Agent, inspector, DFM, settings, modal, or form logic.
- Do not introduce mocks, placeholders, simulated state, or new interactions.
- Do not perform unrelated refactors or rename/restructure components unless required to implement the two approved changes.
- Typography migration covers production-reachable UI owned by `frontend/src`. Tests, QA/dev-only harness markup, generated output, vendored/third-party UI, and rendered CAD canvas content are excluded; a harness that renders production components inherits their production styles without test-only overrides.
- Do not modify `isearch-ai-pr-platform`.
- Do not commit or push changes.

## Error and State Handling

- Existing loading, error, empty, disabled, and generation states keep their current logic and content.
- Typography changes apply to those states using the same semantic scale.
- Conversation creation, removal, rename cancellation, and generation indicators retain current behavior.

## Verification

- Start the real frontend and inspect the vertical list at desktop, 1181–1279 px, 760 px, and mobile widths.
- Verify new conversations append downward and that switching, double-click rename, Escape rename cancellation, close, and generation state still work. Create enough conversations to overflow the collection, then verify vertical scrolling, absence of horizontal scrolling, and visibility of the newest row.
- Inspect representative pages and states across login/auth, workspace, Agent, inspector, DFM, settings, dialogs, forms, errors, loading, and empty states for consistent semantic typography, clipping, unintended wrapping, and layout regressions. Include Chinese and English plus a long conversation name.
- Statically scan production TSX/CSS font-size declarations and typography classes. Outside documented font-family-only monospace exceptions, production UI sizes must resolve to the 11, 13, 15, 18, or 22 px allowlist.
- Run the existing relevant automated tests, ESLint, TypeScript checks, production build, and `git diff --check`.

## Acceptance Criteria

- No horizontal engineering-conversation tab row or horizontal conversation scrolling remains.
- Adding a conversation creates a new vertical row without changing business behavior.
- All production-owned frontend UI text, including technical monospace output, maps to the approved semantic size/line-height scale; technical output preserves only its monospace family.
- No unrelated visual or functional changes are introduced.
- Existing production behavior, routes, API calls, and core interactions continue to pass verification.
