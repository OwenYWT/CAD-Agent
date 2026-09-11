import { useCallback, useState, type CSSProperties, type KeyboardEvent, type PointerEvent, type ReactNode } from "react";
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
}: WorkspaceShellProps) {
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
            ) : agent}
          </aside>
          {!agentCollapsed ? <ResizeHandle direction={1} kind="agent" label="调整 Agent 面板宽度" onResize={resizeAgent} /> : null}

          <main className={`ww-primary-pane ${mobilePreviewOpen ? "ww-primary-pane--mobile-open" : ""}`}>{children}</main>

          {!inspectorCollapsed ? <ResizeHandle direction={-1} kind="inspector" label="调整检查器宽度" onResize={resizeInspector} /> : null}
          <aside className={`ww-inspector-pane ${inspectorCollapsed ? "ww-pane--collapsed" : ""}`}>
            {inspectorCollapsed ? (
              <button aria-label="展开检查器" className="ww-collapsed-pane-button" onClick={onInspectorCollapse} type="button">
                <Icon name="sliders" size={16} />
                <span>检查器</span>
              </button>
            ) : inspector}
          </aside>
        </div>
      </div>
    </div>
  );
}
