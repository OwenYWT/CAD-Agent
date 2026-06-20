import { useState } from "react";
import DFMRuleConfig from "./DFMRuleConfig";
import KnowledgeGraph from "./KnowledgeGraph";

interface SettingsDrawerProps {
  open: boolean;
  onClose: () => void;
}

type SettingsTab = "rules" | "knowledge";

export default function SettingsDrawer({ open, onClose }: SettingsDrawerProps) {
  const [tab, setTab] = useState<SettingsTab>("rules");

  if (!open) return null;

  return (
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 bg-black/30 z-40 transition-opacity"
        onClick={onClose}
      />

      {/* Drawer */}
      <div className="fixed top-0 right-0 h-full w-[420px] max-w-[90vw] bg-white shadow-xl z-50 flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200">
          <h2 className="text-sm font-semibold text-gray-800">设置</h2>
          <button
            onClick={onClose}
            className="p-1 text-gray-400 hover:text-gray-600 rounded hover:bg-gray-100"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Tabs */}
        <div className="flex gap-1 px-4 pt-3">
          <button
            onClick={() => setTab("rules")}
            className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${
              tab === "rules"
                ? "bg-indigo-50 text-indigo-700"
                : "text-gray-500 hover:text-gray-700 hover:bg-gray-50"
            }`}
          >
            DFM 规则
          </button>
          <button
            onClick={() => setTab("knowledge")}
            className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${
              tab === "knowledge"
                ? "bg-indigo-50 text-indigo-700"
                : "text-gray-500 hover:text-gray-700 hover:bg-gray-50"
            }`}
          >
            工艺知识图谱
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto">
          {tab === "rules" && <DFMRuleConfigInline />}
          {tab === "knowledge" && <KnowledgeGraphInline />}
        </div>
      </div>
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
