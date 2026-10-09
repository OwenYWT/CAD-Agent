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
  const [narrowInspector, setNarrowInspector] = useState(() => window.matchMedia("(max-width: 760px)").matches);
  const inspectorRef = useRef<HTMLElement>(null);
  const inspectorCloseRef = useRef<HTMLButtonElement>(null);
  const closeInspectorRef = useRef(onInspectorOverlayClose);
  useEffect(() => { closeInspectorRef.current = onInspectorOverlayClose; }, [onInspectorOverlayClose]);
  useEffect(() => {
    const query = window.matchMedia("(max-width: 760px)");
    const update = (event: MediaQueryListEvent) => setNarrowInspector(event.matches);
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  const inspectorOverlayActive = narrowInspector && inspectorOverlayOpen;
  useEffect(() => {
    if (inspectorCollapsed) return;
    const overlayElement = inspectorRef.current;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const frame = window.requestAnimationFrame(() => inspectorCloseRef.current?.focus());
    const handleKey = (event: globalThis.KeyboardEvent) => {
      const inspectorElement = inspectorRef.current;
      if (!inspectorElement) return;
      const otherModal = Array.from(document.querySelectorAll<HTMLElement>('[aria-modal="true"]'))
        .some(element => element !== inspectorElement && !inspectorElement.contains(element) && element.getClientRects().length > 0);
      if (otherModal) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopImmediatePropagation();
        closeInspectorRef.current?.();
      }
      if (event.key !== "Tab" || !inspectorOverlayActive) return;
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
  }, [inspectorOverlayActive, inspectorCollapsed]);
  const [agentWidth, setAgentWidth] = useState(()=>{
    const saved=Number(localStorage.getItem('cad-agent-panel-width'));
    return Number.isFinite(saved) && saved>=300 && saved<=480 ? saved : 340;
  });
  useEffect(()=>{localStorage.setItem('cad-agent-panel-width',String(agentWidth));},[agentWidth]);
  const resizeAgent = useCallback((delta: number) => {
    setAgentWidth((width) => clamp(width + delta, 300, 480));
  }, []);
  const drawerOpen = !inspectorCollapsed;
  const collapsed = agentCollapsed && !drawerOpen;
  const style = { "--ww-right-width": collapsed ? "44px" : `${agentWidth}px` } as CSSProperties;

  return (
    <div className="ww-app-shell">
      {sidebar}
      <div className="ww-app-main">
        {header}
        <div className="ww-workspace" style={style}>
          <main className={`ww-primary-pane ${mobilePreviewOpen ? "ww-primary-pane--mobile-open" : ""}`}>{children}</main>
          {!collapsed ? <ResizeHandle direction={-1} kind="agent" label="调整右侧面板宽度" onResize={resizeAgent} /> : <div />}
          <div className={`ww-right-pane ${mobilePreviewOpen && !drawerOpen ? "ww-right-pane--mobile-hidden" : ""}`}>
            <aside className={`ww-agent-pane ${collapsed ? "ww-pane--collapsed" : ""}`} hidden={drawerOpen}>
              {collapsed ? <button aria-label="展开 Agent" className="ww-collapsed-pane-button" onClick={onAgentCollapse} type="button"><Icon name="message" size={16} /><span>Agent</span></button> : null}
              <div className="ww-pane-host" hidden={collapsed}>{agent}</div>
            </aside>
            {inspectorOverlayActive ? <button aria-label="返回模型" className="ww-inspector-overlay-backdrop" onClick={onInspectorOverlayClose} tabIndex={-1} type="button" /> : null}
            <aside aria-label="工程信息" aria-modal={inspectorOverlayActive ? true : undefined} className={`ww-inspector-pane ${inspectorOverlayActive ? "ww-inspector-pane--overlay" : ""}`} hidden={!drawerOpen} ref={inspectorRef} role={inspectorOverlayActive ? "dialog" : undefined}>
              <header className="ww-inspector-overlay-header"><button className="workspace-button" onClick={onInspectorOverlayClose || onInspectorCollapse} ref={inspectorCloseRef} type="button"><Icon name="message" size={15} />返回 Agent</button><button aria-label="关闭工程信息" className="workspace-icon-button" onClick={onInspectorOverlayClose || onInspectorCollapse} type="button"><Icon name="x" size={17} /></button></header>
              <div className="ww-pane-host">{inspector}</div>
            </aside>
          </div>
        </div>
      </div>
    </div>
  );
}
