import { useState } from "react";
import { useSessionStore } from "../stores/sessionStore";
import { Icon } from "./ui/Icon";

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
    <div className="flex min-w-0 items-center gap-1 overflow-x-auto">
      {panels.map((panel) => {
        const isActive = panel.id === activePanelId;
        const isEditing = editingId === panel.id;

        return (
          <div
            key={panel.id}
            className={`group flex min-h-9 shrink-0 items-center rounded-md text-xs select-none transition-colors ${
              isActive
                ? "bg-sky-50 text-sky-700 font-medium"
                : "text-slate-500 hover:bg-slate-100 hover:text-slate-700"
            }`}
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
              <button className="min-h-9 max-w-[96px] truncate px-2 text-left" onClick={() => switchPanel(panel.id)} onDoubleClick={() => startRename(panel.id, panel.title)} type="button">{panel.title}</button>
            )}
            {panel.isGenerating && (
              <span className="inline-block w-2 h-2 border border-indigo-400 border-t-transparent rounded-full animate-spin" />
            )}
            {panels.length > 1 && (
              <button
                aria-label={`关闭${panel.title}`}
                className="flex h-8 w-8 items-center justify-center text-slate-400 hover:bg-red-50 hover:text-red-500 sm:opacity-0 sm:group-hover:opacity-100"
                onClick={(e) => {
                  e.stopPropagation();
                  removePanel(panel.id);
                }}
                title="关闭"
                type="button"
              >
                <Icon name="x" size={14} />
              </button>
            )}
          </div>
        );
      })}

      <button
        aria-label="新建对话"
        className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md text-slate-400 hover:bg-sky-50 hover:text-sky-700"
        onClick={() => addPanel()}
        title="新建对话"
        type="button"
      >
        <Icon name="plus" size={16} />
      </button>
    </div>
  );
}
