import type { GenerationResult } from "../types";

export type SuggestionIntent =
  | "generate"
  | "clarify"
  | "modify"
  | "repair"
  | "printability"
  | "explain";

export interface PromptSuggestion {
  label: string;
  prompt: string;
  intent: SuggestionIntent;
}

interface BuildSuggestionsInput {
  isEmpty: boolean;
  isGenerating: boolean;
  result: GenerationResult | null;
  latestUserMessage?: string;
}

const STARTER_SUGGESTIONS: PromptSuggestion[] = [
  {
    label: "\u624b\u673a\u652f\u67b6",
    prompt: "\u8bbe\u8ba1\u4e00\u4e2a\u9002\u5408 3D \u6253\u5370\u7684\u53ef\u8c03\u8282\u624b\u673a\u652f\u67b6\uff0c\u5e26\u5706\u89d2\u3001\u7a33\u5b9a\u5e95\u5ea7\u548c\u5408\u7406\u9ed8\u8ba4\u5c3a\u5bf8\u3002",
    intent: "generate",
  },
  {
    label: "\u5b89\u88c5\u652f\u67b6",
    prompt: "\u8bbe\u8ba1\u4e00\u4e2a\u9002\u5408 3D \u6253\u5370\u7684 L \u5f62\u5b89\u88c5\u652f\u67b6\uff0c\u5305\u542b\u4e24\u4e2a\u87ba\u4e1d\u5b54\u3001\u5706\u89d2\u8fb9\u7f18\u548c\u53ef\u7f16\u8f91\u5c3a\u5bf8\u3002",
    intent: "generate",
  },
  {
    label: "\u7ebf\u7f06\u5361\u6263",
    prompt: "\u8bbe\u8ba1\u4e00\u4e2a\u5939\u5728\u684c\u8fb9\u7684 3D \u6253\u5370\u7ebf\u7f06\u5361\u6263\uff0c\u5e26\u5361\u69fd\u3001\u5706\u89d2\u8fb9\u7f18\u548c\u53ef\u7f16\u8f91\u7ebf\u5f84\u3002",
    intent: "generate",
  },
  {
    label: "\u53c2\u6570\u5316\u76d2\u5b50",
    prompt: "\u8bbe\u8ba1\u4e00\u4e2a\u53c2\u6570\u5316\u7535\u5b50\u5916\u58f3\uff0c\u5305\u542b\u76d6\u5b50\u3001\u58c1\u539a\u3001\u87ba\u4e1d\u67f1\u548c\u6563\u70ed\u5b54\u3002",
    intent: "generate",
  },
];

const SUCCESS_FOLLOWUPS: PromptSuggestion[] = [
  { label: "\u589e\u52a0\u5b54\u4f4d", prompt: "\u5728\u4fdd\u6301\u5f53\u524d\u5916\u5f62\u7684\u57fa\u7840\u4e0a\uff0c\u589e\u52a0\u53ef\u7f16\u8f91\u76f4\u5f84\u3001\u95f4\u8ddd\u548c\u6c89\u5b54\u9009\u9879\u7684\u5b89\u88c5\u5b54\u3002", intent: "modify" },
  { label: "\u8fb9\u7f18\u5012\u5706", prompt: "\u5c06\u5916\u9732\u5c16\u9510\u8fb9\u7f18\u505a\u5b89\u5168\u5706\u89d2\uff0c\u540c\u65f6\u4fdd\u7559\u6240\u6709\u529f\u80fd\u7279\u5f81\u3002", intent: "modify" },
  { label: "\u589e\u5f3a\u5f3a\u5ea6", prompt: "\u5728\u4e0d\u6539\u53d8\u4e3b\u8981\u5360\u7528\u7a7a\u95f4\u7684\u524d\u63d0\u4e0b\uff0c\u901a\u8fc7\u52a0\u539a\u58c1\u539a\u3001\u589e\u52a0\u52a0\u5f3a\u7b4b\u548c\u653e\u5927\u5706\u89d2\u6765\u63d0\u5347\u5f3a\u5ea6\u3002", intent: "modify" },
  { label: "\u8c03\u6574\u5c3a\u5bf8", prompt: "\u66f4\u65b0\u5173\u952e\u5c3a\u5bf8\uff0c\u540c\u65f6\u4fdd\u7559\u5f53\u524d\u8bbe\u8ba1\u610f\u56fe\u5e76\u4fdd\u6301\u6a21\u578b\u53c2\u6570\u5316\u3002", intent: "modify" },
];

function getDesignBrief(result: GenerationResult | null) {
  return result?.design_brief || result?.plan?.design_brief || null;
}

function makeClarification(question: string, index: number): PromptSuggestion {
  return {
    label: index === 0 ? "\u56de\u7b54\u5f85\u786e\u8ba4\u9879" : "\u8865\u5145\u7ec6\u8282",
    prompt: `\u9488\u5bf9\u8bbe\u8ba1\u7b80\u62a5\u4e2d\u7684\u95ee\u9898\u8865\u5145\u8bf4\u660e\uff1a${question} \u6211\u7684\u56de\u7b54\u662f\uff1a`,
    intent: "clarify",
  };
}

function hasInspectionWarning(result: GenerationResult) {
  return result.inspect_report?.verdict === "warn" || result.inspect_report?.verdict === "fail";
}

function hasPrintWarning(result: GenerationResult) {
  return Boolean(result.validation?.print_warnings?.length || result.inspect_report?.print_warnings?.length);
}

function pushUnique(items: PromptSuggestion[], item: PromptSuggestion) {
  if (!items.some((existing) => existing.label === item.label)) items.push(item);
}

export function buildPromptSuggestions({ isEmpty, isGenerating, result }: BuildSuggestionsInput): PromptSuggestion[] {
  if (isGenerating) return [];
  if (isEmpty) return STARTER_SUGGESTIONS;

  const suggestions: PromptSuggestion[] = [];
  const brief = getDesignBrief(result);
  brief?.open_questions?.slice(0, 2).forEach((question, index) => pushUnique(suggestions, makeClarification(question, index)));

  if (result && hasInspectionWarning(result)) {
    pushUnique(suggestions, { label: "\u4f18\u5316\u53ef\u6253\u5370\u6027", prompt: "\u6309 FDM 3D \u6253\u5370\u4f18\u5316\u6a21\u578b\uff1a\u58c1\u539a\u81f3\u5c11 2.4mm\uff0c\u5916\u9732\u8fb9\u7f18\u5012\u5706\uff0c\u5e76\u51cf\u5c11\u65e0\u652f\u6491\u60ac\u5782\u3002", intent: "printability" });
    pushUnique(suggestions, { label: "\u7b80\u5316\u51e0\u4f55", prompt: "\u7528\u66f4\u7a33\u5065\u3001\u66f4\u7b80\u5355\u7684\u51e0\u4f55\u65b9\u5f0f\u91cd\u65b0\u751f\u6210\uff0c\u540c\u65f6\u4fdd\u7559\u6838\u5fc3\u529f\u80fd\u548c\u5173\u952e\u5c3a\u5bf8\u3002", intent: "repair" });
  }

  if (result && hasPrintWarning(result)) {
    pushUnique(suggestions, { label: "\u6539\u5584\u6253\u5370\u98ce\u9669", prompt: "\u6839\u636e\u5f53\u524d\u68c0\u67e5\u8b66\u544a\u6539\u5584\u6a21\u578b\u7684 3D \u6253\u5370\u9002\u914d\u6027\uff0c\u540c\u65f6\u4fdd\u7559\u4e3b\u8981\u8bbe\u8ba1\u610f\u56fe\u3002", intent: "printability" });
  }

  if (result?.repair_history?.length) {
    pushUnique(suggestions, { label: "\u6362\u7b80\u5355\u65b9\u6848", prompt: "\u7528\u66f4\u7b80\u5355\u7684\u5efa\u6a21\u65b9\u5f0f\u91cd\u65b0\u751f\u6210\u8be5\u6a21\u578b\uff0c\u540c\u65f6\u4fdd\u7559\u6838\u5fc3\u529f\u80fd\u548c\u5173\u952e\u5c3a\u5bf8\u3002", intent: "repair" });
    pushUnique(suggestions, { label: "\u89e3\u91ca\u5931\u8d25\u539f\u56e0", prompt: "\u89e3\u91ca\u4e0a\u4e00\u6b21\u751f\u6210\u4e3a\u4ec0\u4e48\u9700\u8981\u4fee\u590d\uff0c\u5e76\u5efa\u8bae\u6700\u5c0f\u7684\u63d0\u793a\u8bcd\u4fee\u6539\u65b9\u5f0f\u3002", intent: "explain" });
  }

  if (result?.success && result.code) SUCCESS_FOLLOWUPS.forEach((item) => pushUnique(suggestions, item));

  if (!suggestions.length) {
    pushUnique(suggestions, { label: "\u8865\u5145\u7ea6\u675f", prompt: "\u8bf7\u628a\u8fd9\u4e2a\u6a21\u578b\u7684\u5173\u952e\u5c3a\u5bf8\u3001\u88c5\u914d\u65b9\u5f0f\u548c 3D \u6253\u5370\u7ea6\u675f\u6574\u7406\u6210\u66f4\u660e\u786e\u7684\u8bbe\u8ba1\u8981\u6c42\u3002", intent: "clarify" });
  }

  return suggestions.slice(0, 6);
}
