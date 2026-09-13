import { useCallback, useEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent, type ReactNode } from "react";
import { Icon } from "../ui/Icon";

interface WorkspaceShellProps {
  sidebar: ReactNode;
  header: ReactNode;
  agent: ReactNode;
  inspector: ReactNode;
  children: ReactNode;
  agentCollapsed: boolean;
  inspectorCollapsed: boolean;
  onAgentCollapse: () => void;
  onInspectorCollapse: () => void;
  mobilePreviewOpen: boolean;
  inspectorOverlayOpen?: boolean;
  onInspectorOverlayClose?: () => void;
}

interface ResizeHandleProps {
  label: string;
  direction: 1 | -1;
  onResize: (delta: number) => void;
  kind: "agent" | "inspector";
}

function ResizeHandle({ label, direction, onResize, kind }: ResizeHandleProps) {
  const startResize = (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    const startX = event.clientX;
    let lastDelta = 0;

    const move = (moveEvent: globalThis.PointerEvent) => {
      const nextDelta = (moveEvent.clientX - startX) * direction;
      onResize(nextDelta - lastDelta);
      lastDelta = nextDelta;
    };
    const stop = () => {
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", stop);
      document.body.classList.remove("ww-is-resizing");
    };

    document.body.classList.add("ww-is-resizing");
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", stop, { once: true });
  };

  const resizeWithKeyboard = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const movement = event.key === "ArrowRight" ? 16 : -16;
    onResize(movement * direction);
  };

  return (
    <div
      aria-label={label}
      aria-orientation="vertical"
      className={`ww-resize-handle ww-resize-handle--${kind}`}
      onKeyDown={resizeWithKeyboard}
      onPointerDown={startResize}
      role="separator"
      tabIndex={0}
    />
  );
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

export default function WorkspaceShell({
  sidebar,
  header,
  agent,
  inspector,
  children,
  agentCollapsed,
  inspectorCollapsed,
  onAgentCollapse,
  onInspectorCollapse,
  mobilePreviewOpen,
  inspectorOverlayOpen = false,
  onInspectorOverlayClose,
}: WorkspaceShellProps) {
  const [narrowInspector, setNarrowInspector] = useState(() => window.matchMedia("(max-width: 1180px)").matches);
  const inspectorRef = useRef<HTMLElement>(null);
  const inspectorCloseRef = useRef<HTMLButtonElement>(null);
  const closeInspectorRef = useRef(onInspectorOverlayClose);
  useEffect(() => { closeInspectorRef.current = onInspectorOverlayClose; }, [onInspectorOverlayClose]);
  useEffect(() => {
    const query = window.matchMedia("(max-width: 1180px)");
    const update = (event: MediaQueryListEvent) => setNarrowInspector(event.matches);
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  const inspectorOverlayActive = narrowInspector && inspectorOverlayOpen;
  useEffect(() => {
    if (!inspectorOverlayActive) return;
    const overlayElement = inspectorRef.current;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const frame = window.requestAnimationFrame(() => inspectorCloseRef.current?.focus());
    const handleKey = (event: globalThis.KeyboardEvent) => {
      const inspectorElement = inspectorRef.current;
      if (!inspectorElement) return;
      if (event.key === "Escape" && inspectorElement.contains(document.activeElement)) {
        event.preventDefault();
        event.stopImmediatePropagation();
        closeInspectorRef.current?.();
      }
      if (event.key !== "Tab") return;
      const controls = Array.from(inspectorElement.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], summary, [tabindex]:not([tabindex="-1"])',
      )).filter(element => element.tabIndex >= 0 && element.getClientRects().length > 0);
      const first = controls[0], last = controls.at(-1);
      if (!first || !last) return;
      if (!inspectorElement.contains(document.activeElement) || event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", handleKey, true);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("keydown", handleKey, true);
      if (previousFocus?.isConnected && overlayElement?.contains(document.activeElement)) previousFocus.focus();
    };
  }, [inspectorOverlayActive]);
  const [agentWidth, setAgentWidth] = useState(() => window.innerWidth < 1360 ? 340 : 390);
  const [inspectorWidth, setInspectorWidth] = useState(() => window.innerWidth < 1360 ? 280 : 300);
  const resizeAgent = useCallback((delta: number) => {
    setAgentWidth((width) => clamp(width + delta, 300, 520));
  }, []);
  const resizeInspector = useCallback((delta: number) => {
    setInspectorWidth((width) => clamp(width + delta, 280, 440));
  }, []);
  const style = {
    "--ww-agent-width": agentCollapsed ? "44px" : `${agentWidth}px`,
    "--ww-inspector-width": inspectorCollapsed ? "44px" : `${inspectorWidth}px`,
  } as CSSProperties;

  return (
    <div className="ww-app-shell">
      {sidebar}
      <div className="ww-app-main">
        {header}
        <div className="ww-workspace" style={style}>
          <aside className={`ww-agent-pane ${agentCollapsed ? "ww-pane--collapsed" : ""}`}>
            {agentCollapsed ? (
              <button aria-label="展开 Agent" className="ww-collapsed-pane-button" onClick={onAgentCollapse} type="button">
                <Icon name="message" size={16} />
                <span>Agent</span>
              </button>
            ) : null}
            <div className="ww-pane-host" hidden={agentCollapsed}>{agent}</div>
          </aside>
          {!agentCollapsed ? <ResizeHandle direction={1} kind="agent" label="调整 Agent 面板宽度" onResize={resizeAgent} /> : null}

          <main className={`ww-primary-pane ${mobilePreviewOpen ? "ww-primary-pane--mobile-open" : ""}`}>{children}</main>

          {!inspectorCollapsed ? <ResizeHandle direction={-1} kind="inspector" label="调整检查器宽度" onResize={resizeInspector} /> : null}
          {inspectorOverlayActive ? <button aria-label="关闭机械设计检查器遮罩" className="ww-inspector-overlay-backdrop" onClick={onInspectorOverlayClose} tabIndex={-1} type="button" /> : null}
          <aside aria-label="机械设计检查器" aria-modal={inspectorOverlayActive ? true : undefined} className={`ww-inspector-pane ${inspectorCollapsed && !inspectorOverlayActive ? "ww-pane--collapsed" : ""} ${inspectorOverlayActive ? "ww-inspector-pane--overlay" : ""}`} ref={inspectorRef} role={inspectorOverlayActive ? "dialog" : undefined}>
            {inspectorOverlayActive ? <header className="ww-inspector-overlay-header"><strong>机械设计检查器</strong><button aria-label="关闭机械设计检查器" className="workspace-icon-button" onClick={onInspectorOverlayClose} ref={inspectorCloseRef} type="button"><Icon name="x" size={17} /></button></header> : null}
            {inspectorCollapsed && !inspectorOverlayActive ? (
              <button aria-label="展开检查器" className="ww-collapsed-pane-button" onClick={onInspectorCollapse} type="button">
                <Icon name="sliders" size={16} />
                <span>检查器</span>
              </button>
            ) : null}
            <div className="ww-pane-host" hidden={inspectorCollapsed && !inspectorOverlayActive}>{inspector}</div>
          </aside>
        </div>
      </div>
    </div>
  );
}
