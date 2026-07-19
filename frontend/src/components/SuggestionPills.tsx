import type { PromptSuggestion } from "../utils/suggestions";

interface SuggestionPillsProps {
  suggestions: PromptSuggestion[];
  disabled?: boolean;
  onSelect: (suggestion: PromptSuggestion) => void;
}

const INTENT_STYLES: Record<PromptSuggestion["intent"], string> = {
  generate: "border-indigo-200 bg-indigo-50 text-indigo-700 hover:bg-indigo-100",
  clarify: "border-blue-200 bg-blue-50 text-blue-700 hover:bg-blue-100",
  modify: "border-emerald-200 bg-emerald-50 text-emerald-700 hover:bg-emerald-100",
  repair: "border-amber-200 bg-amber-50 text-amber-700 hover:bg-amber-100",
  printability: "border-purple-200 bg-purple-50 text-purple-700 hover:bg-purple-100",
  explain: "border-gray-200 bg-gray-50 text-gray-700 hover:bg-gray-100",
};

export default function SuggestionPills({ suggestions, disabled, onSelect }: SuggestionPillsProps) {
  if (!suggestions.length) return null;

  return (
    <div className="flex gap-2 overflow-x-auto pb-2 [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
      {suggestions.map((suggestion) => (
        <button
          key={`${suggestion.intent}-${suggestion.label}`}
          type="button"
          disabled={disabled}
          onClick={() => onSelect(suggestion)}
          title={suggestion.prompt}
          className={`shrink-0 rounded-full border px-3 py-1 text-xs font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed ${INTENT_STYLES[suggestion.intent]}`}
        >
          {suggestion.label}
        </button>
      ))}
    </div>
  );
}
