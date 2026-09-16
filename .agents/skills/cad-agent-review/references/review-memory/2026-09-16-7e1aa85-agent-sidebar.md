# Agent visibility with the responsive project sidebar

## Established fact

The project sidebar becomes a fixed overlay at `max-width: 1023px`. Any action that exposes the embedded Agent pane must coordinate with `mobileSidebar`; otherwise the sidebar can remain above the Agent pane and make the requested Agent UI appear missing.

At the reviewed `7e1aa85` implementation, `askAgent`:

- expands the embedded Agent pane;
- switches back from mobile preview at `max-width: 760px`;
- opens `AgentDrawer` only at `max-width: 899px`;
- does not close `mobileSidebar`.

## Future review gate

When responsive layout, project navigation, Ask Agent entry points, `WorkspaceDrawer`, or Agent pane behavior changes, verify this state matrix:

| Viewport | Sidebar state before Ask Agent | Required evidence |
| --- | --- | --- |
| `1023px` | open | Agent composer is visible and focusable after the action. |
| `900px` | open | No uncovered gap between sidebar-overlay and drawer breakpoints. |
| `899px` | open | Drawer or replacement Agent surface appears above/after navigation. |
| `760px` | open | Agent-first mobile pane is not left underneath the sidebar. |

Static CSS tests alone are insufficient. Prefer a browser/DOM interaction test that opens the sidebar, invokes Ask Agent, and asserts the visible dialog/composer plus focus target.

## Resolution on 2026-09-16

The implementation was updated so `askAgent` calls `setMobileSidebar(false)` before exposing the Agent pane. A regression test now protects the ordering. The focused suite passed 15/15, the complete frontend test suite passed 148/148, lint passed, and the production build passed.

This resolution has logic/build evidence but not a current authenticated browser screenshot or focus assertion. Keep the viewport matrix above as a product-acceptance gate.
