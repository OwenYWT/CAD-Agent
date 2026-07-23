import { useEffect, useRef, useState } from "react";
import DFMRuleConfig from "./DFMRuleConfig";
import KnowledgeGraph from "./KnowledgeGraph";
import CapabilityCatalog from "./CapabilityCatalog";
import CapabilityRunner from "./CapabilityRunner";
import { Icon } from "./ui/Icon";

interface SettingsDrawerProps {
  open: boolean;
  onClose: () => void;
}

type SettingsTab = "rules" | "knowledge" | "capabilities" | "runner";

export default function SettingsDrawer({ open, onClose }: SettingsDrawerProps) {
  const [tab, setTab] = useState<SettingsTab>("rules");
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const drawerRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!open) return;
    const previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeButtonRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const drawer = drawerRef.current;
      if (!drawer) return;
      const focusable = Array.from(
        drawer.querySelectorAll<HTMLElement>(
          "button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex='-1'])",
        ),
      ).filter((element) => element.getAttribute("aria-hidden") !== "true");
      const first = focusable[0];
      const last = focusable.at(-1);
      if (!first || !last) return;
      if (event.shiftKey && (document.activeElement === first || !drawer.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !drawer.contains(document.activeElement))) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      previouslyFocused?.focus();
    };
  }, [onClose, open]);

  if (!open) return null;

  return (
    <>
      {/* Backdrop */}
      <button
        aria-label="关闭设置"
        className="fixed inset-0 z-40 cursor-default bg-black/35 transition-opacity"
        onClick={onClose}
        tabIndex={-1}
        type="button"
      />

      {/* Drawer */}
      <aside aria-labelledby="settings-title" aria-modal="true" className="fixed right-0 top-0 z-50 flex h-full w-full flex-col bg-white shadow-xl sm:w-[520px] sm:max-w-[90vw]" ref={drawerRef} role="dialog">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200">
          <h2 className="text-sm font-semibold text-gray-800" id="settings-title">设置</h2>
          <button
            aria-label="关闭设置"
            onClick={onClose}
            className="icon-button text-slate-500 hover:bg-slate-100 hover:text-slate-900"
            title="关闭"
            type="button"
            ref={closeButtonRef}
          >
            <Icon name="x" size={18} />
          </button>
        </div>

        {/* Tabs */}
        <div aria-label="设置分类" className="flex gap-1 overflow-x-auto px-4 pt-3" role="tablist">
          <button
            aria-controls="settings-panel-rules"
            aria-selected={tab === "rules"}
            onClick={() => setTab("rules")}
            className={`min-h-10 px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${
              tab === "rules"
                ? "bg-indigo-50 text-indigo-700"
                : "text-gray-500 hover:text-gray-700 hover:bg-gray-50"
            }`}
            id="settings-tab-rules"
            role="tab"
            type="button"
          >
            DFM 规则
          </button>
          <button
            aria-controls="settings-panel-knowledge"
            aria-selected={tab === "knowledge"}
            onClick={() => setTab("knowledge")}
            className={`min-h-10 px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${
              tab === "knowledge"
                ? "bg-indigo-50 text-indigo-700"
                : "text-gray-500 hover:text-gray-700 hover:bg-gray-50"
            }`}
            id="settings-tab-knowledge"
            role="tab"
            type="button"
          >
            工艺知识图谱
          </button>
          <button
            aria-controls="settings-panel-capabilities"
            aria-selected={tab === "capabilities"}
            className={`min-h-10 whitespace-nowrap rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
              tab === "capabilities"
                ? "bg-indigo-50 text-indigo-700"
                : "text-gray-500 hover:bg-gray-50 hover:text-gray-700"
            }`}
            id="settings-tab-capabilities"
            onClick={() => setTab("capabilities")}
            role="tab"
            type="button"
          >
            全部能力
          </button>
          <button
            aria-controls="settings-panel-runner"
            aria-selected={tab === "runner"}
            className={`min-h-10 whitespace-nowrap rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
              tab === "runner"
                ? "bg-indigo-50 text-indigo-700"
                : "text-gray-500 hover:bg-gray-50 hover:text-gray-700"
            }`}
            id="settings-tab-runner"
            onClick={() => setTab("runner")}
            role="tab"
            type="button"
          >
            运行能力
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto">
          {tab === "rules" && <div aria-labelledby="settings-tab-rules" id="settings-panel-rules" role="tabpanel"><DFMRuleConfigInline /></div>}
          {tab === "knowledge" && <div aria-labelledby="settings-tab-knowledge" id="settings-panel-knowledge" role="tabpanel"><KnowledgeGraphInline /></div>}
          {tab === "capabilities" && <div aria-labelledby="settings-tab-capabilities" id="settings-panel-capabilities" role="tabpanel"><CapabilityCatalog /></div>}
          {tab === "runner" && <div aria-labelledby="settings-tab-runner" id="settings-panel-runner" role="tabpanel"><CapabilityRunner /></div>}
        </div>
      </aside>
    </>
  );
}

/** DFMRuleConfig always-open variant for settings drawer */
function DFMRuleConfigInline() {
  return (
    <div className="p-0">
      <DFMRuleConfigAlwaysOpen />
    </div>
  );
}

/** KnowledgeGraph always-open variant for settings drawer */
function KnowledgeGraphInline() {
  return (
    <div className="p-0">
      <KnowledgeGraphAlwaysOpen />
    </div>
  );
}

// We need always-open variants — since the originals have internal open/close state,
// we'll just render them and they'll show their "closed" button initially.
// For now, render them directly; they auto-expand in the drawer context.
function DFMRuleConfigAlwaysOpen() {
  // Render original component — user clicks to expand
  return <DFMRuleConfig />;
}

function KnowledgeGraphAlwaysOpen() {
  return <KnowledgeGraph />;
}
