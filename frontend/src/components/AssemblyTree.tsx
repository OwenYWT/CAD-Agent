import { useState } from "react";
import type { AssemblyPartInfo } from "../types";

interface AssemblyTreeProps {
  parts: AssemblyPartInfo[] | null;
  onModifyPart: (partName: string, instruction: string) => void;
  isGenerating: boolean;
}

const STATUS_ICON: Record<string, string> = {
  success: "\u2705",
  failed: "\u274C",
};

export default function AssemblyTree({
  parts,
  onModifyPart,
  isGenerating,
}: AssemblyTreeProps) {
  const [expandedPart, setExpandedPart] = useState<string | null>(null);
  const [editingPart, setEditingPart] = useState<string | null>(null);
  const [editInstruction, setEditInstruction] = useState("");

  if (!parts || parts.length === 0) return null;

  const handleSubmitEdit = (partName: string) => {
    const text = editInstruction.trim();
    if (!text) return;
    onModifyPart(partName, text);
    setEditInstruction("");
    setEditingPart(null);
  };

  return (
    <div className="bg-white p-3">
      <h3 className="text-sm font-medium text-gray-700 mb-2">
        装配体零件 ({parts.length})
      </h3>
      <div className="space-y-1">
        {parts.map((part) => {
          const isExpanded = expandedPart === part.name;
          const isEditing = editingPart === part.name;

          return (
            <div key={part.name} className="border border-gray-100 rounded">
              {/* Part header */}
              <div
                className="flex items-center gap-2 px-2 py-1.5 cursor-pointer hover:bg-gray-50"
                onClick={() =>
                  setExpandedPart(isExpanded ? null : part.name)
                }
              >
                <span className="text-xs">{isExpanded ? "\u25BC" : "\u25B6"}</span>
                <span
                  className="w-3 h-3 rounded-full shrink-0"
                  style={{ backgroundColor: part.color }}
                />
                <span className="flex-1 text-xs font-medium text-gray-700 truncate">
                  {part.name}
                </span>
                <span className="text-xs" title={part.status}>
                  {STATUS_ICON[part.status] || "\u2753"}
                </span>
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    setEditingPart(isEditing ? null : part.name);
                    setEditInstruction("");
                    if (!isExpanded) setExpandedPart(part.name);
                  }}
                  disabled={isGenerating}
                  className="text-xs text-indigo-500 hover:text-indigo-700 disabled:opacity-40 px-1"
                  title="修改此零件"
                >
                  编辑
                </button>
              </div>

              {/* Expanded content */}
              {isExpanded && (
                <div className="px-2 pb-2 space-y-2">
                  {/* Description */}
                  {part.description && (
                    <p className="text-xs text-gray-500">{part.description}</p>
                  )}

                  {/* Code preview */}
                  <pre className="text-xs bg-gray-800 text-green-300 rounded p-2 overflow-x-auto max-h-40 overflow-y-auto">
                    {part.code}
                  </pre>

                  {/* Edit form */}
                  {isEditing && (
                    <div className="flex gap-1">
                      <input
                        type="text"
                        value={editInstruction}
                        onChange={(e) => setEditInstruction(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") handleSubmitEdit(part.name);
                          if (e.key === "Escape") setEditingPart(null);
                        }}
                        placeholder={`描述对 ${part.name} 的修改...`}
                        disabled={isGenerating}
                        className="flex-1 border border-gray-300 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-indigo-400 disabled:opacity-50"
                        autoFocus
                      />
                      <button
                        onClick={() => handleSubmitEdit(part.name)}
                        disabled={isGenerating || !editInstruction.trim()}
                        className="bg-indigo-600 text-white rounded px-2 py-1 text-xs hover:bg-indigo-700 disabled:opacity-50"
                      >
                        修改
                      </button>
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
