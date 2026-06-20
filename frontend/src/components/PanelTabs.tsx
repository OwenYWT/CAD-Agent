import { useState } from "react";
import { useSessionStore } from "../stores/sessionStore";

/** Inline panel tabs — designed to sit inside the header bar. */
export default function PanelTabs() {
  const { panels, activePanelId, switchPanel, addPanel, removePanel, renamePanel } =
    useSessionStore();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");

  const startRename = (id: string, currentTitle: string) => {
    setEditingId(id);
    setEditTitle(currentTitle);
  };

  const commitRename = () => {
    if (editingId && editTitle.trim()) {
      renamePanel(editingId, editTitle.trim());
    }
    setEditingId(null);
  };

  return (
    <div className="flex items-center gap-1 overflow-x-auto">
      {panels.map((panel) => {
        const isActive = panel.id === activePanelId;
        const isEditing = editingId === panel.id;

        return (
          <div
            key={panel.id}
            className={`group flex items-center gap-1 px-2.5 py-1 rounded text-xs cursor-pointer select-none transition-colors ${
              isActive
                ? "bg-indigo-50 text-indigo-700 font-medium"
                : "text-gray-500 hover:bg-gray-100 hover:text-gray-700"
            }`}
            onClick={() => switchPanel(panel.id)}
            onDoubleClick={() => startRename(panel.id, panel.title)}
          >
            {isEditing ? (
              <input
                type="text"
                value={editTitle}
                onChange={(e) => setEditTitle(e.target.value)}
                onBlur={commitRename}
                onKeyDown={(e) => {
                  if (e.key === "Enter") commitRename();
                  if (e.key === "Escape") setEditingId(null);
                }}
                className="w-20 text-xs border-none outline-none bg-transparent"
                autoFocus
                onClick={(e) => e.stopPropagation()}
              />
            ) : (
              <span className="truncate max-w-[100px]">{panel.title}</span>
            )}
            {panel.isGenerating && (
              <span className="inline-block w-2 h-2 border border-indigo-400 border-t-transparent rounded-full animate-spin" />
            )}
            {panels.length > 1 && (
              <button
                className="opacity-0 group-hover:opacity-60 hover:!opacity-100 ml-0.5 text-gray-400 hover:text-red-500"
                onClick={(e) => {
                  e.stopPropagation();
                  removePanel(panel.id);
                }}
                title="关闭"
              >
                x
              </button>
            )}
          </div>
        );
      })}

      <button
        className="px-1.5 py-1 text-xs text-gray-400 hover:text-indigo-500 rounded"
        onClick={() => addPanel()}
        title="新建对话"
      >
        +
      </button>
    </div>
  );
}
