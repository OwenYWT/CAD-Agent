import type { PromptSuggestion } from "../utils/suggestions";

interface SuggestionPillsProps {
  suggestions: PromptSuggestion[];
  disabled?: boolean;
  onSelect: (suggestion: PromptSuggestion) => void;
}

const INTENT_STYLES: Record<PromptSuggestion["intent"], string> = {
  generate: "border-[var(--agent-border)] bg-[var(--agent-soft)] text-[var(--agent)] hover:border-[var(--agent)]",
  clarify: "border-[var(--agent-border)] bg-[var(--agent-soft)] text-[var(--agent)] hover:border-[var(--agent)]",
  modify: "border-emerald-200 bg-emerald-50 text-emerald-700 hover:bg-emerald-100",
  repair: "border-amber-200 bg-amber-50 text-amber-700 hover:bg-amber-100",
  printability: "border-[var(--agent-border)] bg-[var(--agent-soft)] text-[var(--agent)] hover:border-[var(--agent)]",
  explain: "border-[var(--line)] bg-[var(--surface-soft)] text-[var(--muted)] hover:border-[var(--line-strong)] hover:text-[var(--ink)]",
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
          className={`shrink-0 rounded-full border px-3 py-1 type-control  transition-colors disabled:opacity-50 disabled:cursor-not-allowed ${INTENT_STYLES[suggestion.intent]}`}
        >
          {suggestion.label}
        </button>
      ))}
    </div>
  );
}
