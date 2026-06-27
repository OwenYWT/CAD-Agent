import { useState } from "react";
import type { DesignAnalysis as DesignAnalysisType, DFMIssue, RuleViolation, Annotation3D } from "../types";

import { authFetch } from "../auth";
interface DesignAnalysisProps {
  requestId: string | null;
  code: string | null;
  description: string;
  hasResult: boolean;
  is2D: boolean;
  onAnnotationsReady?: (annotations: Annotation3D[]) => void;
  selectedAnnotation?: string | null;
  onSelectAnnotation?: (id: string | null) => void;
}

const API_BASE = import.meta.env.VITE_API_BASE || "";

const SEVERITY_ORDER: Record<string, number> = { critical: 0, warning: 1, info: 2 };
const SEVERITY_STYLE: Record<string, string> = {
  critical: "bg-red-50 text-red-700 border-red-200",
  warning: "bg-amber-50 text-amber-700 border-amber-200",
  info: "bg-blue-50 text-blue-700 border-blue-200",
};
const SEVERITY_ICON: Record<string, string> = {
  critical: "\u26D4",
  warning: "\u26A0\uFE0F",
  info: "\u2139\uFE0F",
};
const COMPAT_STYLE: Record<string, string> = {
  "\u9002\u5408": "text-emerald-700 bg-emerald-50 border-emerald-200",
  "\u9700\u4FEE\u6539": "text-amber-700 bg-amber-50 border-amber-200",
  "\u4E0D\u9002\u5408": "text-red-700 bg-red-50 border-red-200",
};
const DIFFICULTY_LABEL: Record<string, string> = {
  easy: "\u7B80\u5355",
  medium: "\u4E2D\u7B49",
  hard: "\u56F0\u96BE",
};

type SubTab = "issues" | "compat" | "data";

function ScoreRing({ score }: { score: number }) {
  const color = score >= 80 ? "text-emerald-600" : score >= 60 ? "text-amber-500" : "text-red-500";
  const bg = score >= 80 ? "bg-emerald-50" : score >= 60 ? "bg-amber-50" : "bg-red-50";
  return (
    <div className={`flex items-center justify-center w-10 h-10 rounded-full ${bg}`}>
      <span className={`text-lg font-bold ${color}`}>{score}</span>
    </div>
  );
}

function IssueCard({
  issue,
  annotationId,
  isSelected,
  onLocate,
}: {
  issue: DFMIssue;
  annotationId?: string;
  isSelected?: boolean;
  onLocate?: (id: string) => void;
}) {
  const style = SEVERITY_STYLE[issue.severity] || SEVERITY_STYLE.info;
  const icon = SEVERITY_ICON[issue.severity] || "";
  return (
    <div className={`rounded-lg border p-2.5 text-xs ${style} ${isSelected ? "ring-2 ring-indigo-400" : ""}`}>
      <div className="font-medium flex items-center justify-between">
        <span>
          {icon} {issue.category} — {issue.description}
        </span>
        {annotationId && onLocate && (
          <button
            onClick={() => onLocate(annotationId)}
            className="ml-1 shrink-0 text-[10px] bg-white/50 hover:bg-white/80 rounded px-1.5 py-0.5"
            title="在 3D 视图中定位"
          >
            定位
          </button>
        )}
      </div>
      {issue.suggestion && (
        <div className="mt-1 opacity-80">
          建议: {issue.suggestion}
        </div>
      )}
      {issue.location && (
        <div className="mt-0.5 opacity-60">位置: {issue.location}</div>
      )}
    </div>
  );
}

/* === Sub-tab: Issues === */
function IssuesTab({
  analysis,
  selectedAnnotation,
  onSelectAnnotation,
}: {
  analysis: DesignAnalysisType;
  selectedAnnotation?: string | null;
  onSelectAnnotation?: (id: string | null) => void;
}) {
  const sortedIssues = [...analysis.dfm_issues].sort(
    (a, b) => (SEVERITY_ORDER[a.severity] ?? 9) - (SEVERITY_ORDER[b.severity] ?? 9),
  );

  return (
    <div className="space-y-3">
      {/* DFM Issues */}
      {sortedIssues.length > 0 ? (
        <div className="space-y-1.5">
          {sortedIssues.map((issue, i) => {
            const matchingAnn = (analysis.annotations || []).find(
              (a) => a.category === issue.category,
            );
            return (
              <IssueCard
                key={i}
                issue={issue}
                annotationId={matchingAnn?.id}
                isSelected={!!matchingAnn && selectedAnnotation === matchingAnn.id}
                onLocate={onSelectAnnotation || undefined}
              />
            );
          })}
        </div>
      ) : (
        <p className="text-xs text-gray-400">未发现 DFM 问题</p>
      )}

      {/* Structural issues */}
      {analysis.structural_issues.length > 0 && (
        <div>
          <h4 className="text-xs font-medium text-gray-500 mb-1">结构问题</h4>
          <ul className="text-xs text-gray-600 space-y-0.5 list-disc pl-4">
            {analysis.structural_issues.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Rule violations */}
      {analysis.rule_violations && analysis.rule_violations.length > 0 && (
        <div>
          <h4 className="text-xs font-medium text-gray-500 mb-1">规则引擎详情 ({analysis.rule_violations.length})</h4>
          <div className="space-y-1">
            {analysis.rule_violations.map((rv: RuleViolation, i: number) => (
              <div key={i} className="text-xs border border-gray-100 rounded p-1.5">
                <div className="flex items-center gap-1">
                  <span className={`px-1 rounded text-[10px] ${
                    rv.source === "geometric"
                      ? "bg-emerald-50 text-emerald-700"
                      : "bg-purple-50 text-purple-700"
                  }`}>
                    {rv.source === "geometric" ? "\u7CBE\u786E" : "AI"}
                  </span>
                  <span className="text-gray-500">{rv.process}</span>
                  <span className="text-gray-300">\u00B7</span>
                  <span className="text-gray-600">{rv.message}</span>
                </div>
                {rv.suggestion && (
                  <div className="text-gray-400 mt-0.5 pl-1">{rv.suggestion}</div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/* === Sub-tab: Compatibility === */
function CompatTab({ analysis }: { analysis: DesignAnalysisType }) {
  return (
    <div className="space-y-3">
      {/* Process compatibility */}
      {Object.keys(analysis.process_compatibility).length > 0 && (
        <div>
          <h4 className="text-xs font-medium text-gray-500 mb-2">工艺兼容性</h4>
          <div className="grid grid-cols-2 gap-1.5">
            {Object.entries(analysis.process_compatibility).map(([proc, compat]) => (
              <div
                key={proc}
                className={`text-xs px-2.5 py-1.5 rounded-lg border ${COMPAT_STYLE[compat] || "bg-gray-50 text-gray-600 border-gray-200"}`}
              >
                <div className="font-medium">{proc}</div>
                <div className="opacity-75">{compat}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Functional notes */}
      {analysis.functional_notes.length > 0 && (
        <div>
          <h4 className="text-xs font-medium text-gray-500 mb-1">功能备注</h4>
          <ul className="text-xs text-gray-600 space-y-0.5 list-disc pl-4">
            {analysis.functional_notes.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

/* === Sub-tab: Data === */
function DataTab({ analysis }: { analysis: DesignAnalysisType }) {
  const geo = analysis.geometry as Record<string, unknown>;

  return (
    <div className="space-y-3">
      {/* STEP Precision Analysis */}
      {analysis.step_analysis?.available && (
        <div>
          <h4 className="text-xs font-medium text-gray-500 mb-2">STEP 精确分析</h4>
          <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-gray-600 bg-gray-50 rounded-lg p-2.5">
            {analysis.step_analysis.min_wall_thickness != null && (
              <>
                <span>精确最小壁厚</span>
                <span className="font-medium">{analysis.step_analysis.min_wall_thickness.toFixed(3)} mm</span>
              </>
            )}
            {analysis.step_analysis.min_fillet_radius != null && (
              <>
                <span>最小圆角半径</span>
                <span className="font-medium">{analysis.step_analysis.min_fillet_radius.toFixed(2)} mm</span>
              </>
            )}
            {analysis.step_analysis.min_hole_diameter != null && (
              <>
                <span>最小孔径</span>
                <span className="font-medium">{analysis.step_analysis.min_hole_diameter.toFixed(2)} mm</span>
              </>
            )}
            {analysis.step_analysis.min_draft_angle != null && (
              <>
                <span>最小拔模角</span>
                <span className="font-medium">{analysis.step_analysis.min_draft_angle.toFixed(1)}&deg;</span>
              </>
            )}
            <span>B-rep 面数</span>
            <span className="font-medium">{analysis.step_analysis.face_count}</span>
            <span>B-rep 边数</span>
            <span className="font-medium">{analysis.step_analysis.edge_count}</span>
            <span>识别特征数</span>
            <span className="font-medium">{analysis.step_analysis.feature_count}</span>
          </div>
          {analysis.step_analysis.features.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1">
              {analysis.step_analysis.features.map((f, i) => (
                <span key={i} className="text-xs bg-emerald-50 text-emerald-700 px-1.5 py-0.5 rounded">
                  {f.type}
                  {f.dimensions.diameter != null && ` \u00D8${f.dimensions.diameter}`}
                  {f.dimensions.count != null && Number(f.dimensions.count) > 1 && ` \u00D7${f.dimensions.count}`}
                  {f.dimensions.radii != null && ` R${(f.dimensions.radii as number[]).join("/")}`}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Geometry data */}
      <div>
        <h4 className="text-xs font-medium text-gray-500 mb-2">几何数据</h4>
        <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-gray-600 bg-gray-50 rounded-lg p-2.5">
          {geo.min_wall_thickness !== undefined && (
            <>
              <span>最薄壁厚</span>
              <span className="font-medium">{String(geo.min_wall_thickness)} mm</span>
            </>
          )}
          {geo.material_ratio !== undefined && (
            <>
              <span>实心率</span>
              <span className="font-medium">{(Number(geo.material_ratio) * 100).toFixed(1)}%</span>
            </>
          )}
          {geo.surface_area !== undefined && (
            <>
              <span>表面积</span>
              <span className="font-medium">{Number(geo.surface_area).toLocaleString()} mm\u00B2</span>
            </>
          )}
          {geo.volume !== undefined && Number(geo.volume) > 0 && (
            <>
              <span>体积</span>
              <span className="font-medium">{Number(geo.volume).toLocaleString()} mm\u00B3</span>
            </>
          )}
          {geo.face_count !== undefined && (
            <>
              <span>面片数</span>
              <span className="font-medium">{Number(geo.face_count).toLocaleString()}</span>
            </>
          )}
          {geo.overhang_ratio !== undefined && (
            <>
              <span>悬臂占比</span>
              <span className="font-medium">{(Number(geo.overhang_ratio) * 100).toFixed(1)}%</span>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

export default function DesignAnalysis({
  requestId,
  code,
  description,
  hasResult,
  is2D,
  onAnnotationsReady,
  selectedAnnotation,
  onSelectAnnotation,
}: DesignAnalysisProps) {
  const [analysis, setAnalysis] = useState<DesignAnalysisType | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [subTab, setSubTab] = useState<SubTab>("issues");

  if (!hasResult || !requestId || is2D) return null;

  const handleAnalyze = async () => {
    setLoading(true);
    setError(null);
    setAnalysis(null);
    try {
      const res = await authFetch(`${API_BASE}/api/analyze/${requestId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: code || "", description }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `HTTP ${res.status}`);
      }
      const data: DesignAnalysisType = await res.json();
      setAnalysis(data);
      if (data.annotations && onAnnotationsReady) {
        onAnnotationsReady(data.annotations);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "分析失败");
    } finally {
      setLoading(false);
    }
  };

  // Not yet analyzed
  if (!analysis && !loading) {
    return (
      <div className="p-4">
        <button
          onClick={handleAnalyze}
          className="w-full py-2.5 text-sm bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50 font-medium transition-colors"
        >
          AI 设计审查 + DFM 分析
        </button>
        {error && <p className="text-xs text-red-500 mt-2">{error}</p>}
      </div>
    );
  }

  // Loading
  if (loading) {
    return (
      <div className="p-4">
        <div className="flex items-center gap-2 text-sm text-gray-500">
          <span className="animate-spin inline-block w-4 h-4 border-2 border-indigo-500 border-t-transparent rounded-full" />
          正在分析设计和可制造性...
        </div>
      </div>
    );
  }

  if (!analysis) return null;

  const issueCount = analysis.dfm_issues.length + analysis.structural_issues.length;

  return (
    <div className="flex flex-col h-full">
      {/* Fixed header: score + summary */}
      <div className="p-4 space-y-2 shrink-0">
        <div className="flex items-center gap-3">
          <ScoreRing score={analysis.design_score} />
          <div className="flex-1 min-w-0">
            <p className="text-sm text-gray-700 leading-snug">{analysis.design_summary}</p>
            <div className="flex flex-wrap gap-1.5 mt-1">
              {analysis.recommended_process && (
                <span className="text-[10px] bg-indigo-50 text-indigo-700 px-1.5 py-0.5 rounded">
                  推荐: {analysis.recommended_process}
                </span>
              )}
              <span className="text-[10px] bg-gray-100 text-gray-500 px-1.5 py-0.5 rounded">
                难度: {DIFFICULTY_LABEL[analysis.estimated_difficulty] || analysis.estimated_difficulty}
              </span>
            </div>
          </div>
          <button
            onClick={handleAnalyze}
            className="p-1.5 text-gray-400 hover:text-indigo-600 hover:bg-indigo-50 rounded transition-colors shrink-0"
            title="重新分析"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
          </button>
        </div>

        {/* Sub-tabs */}
        <div className="flex gap-0.5 border-b border-gray-100 -mx-4 px-4">
          {([
            { key: "issues" as SubTab, label: "问题清单", badge: issueCount || undefined },
            { key: "compat" as SubTab, label: "工艺兼容" },
            { key: "data" as SubTab, label: "详细数据" },
          ]).map((t) => (
            <button
              key={t.key}
              onClick={() => setSubTab(t.key)}
              className={`px-3 py-1.5 text-xs font-medium border-b-2 transition-colors ${
                subTab === t.key
                  ? "border-indigo-600 text-indigo-700"
                  : "border-transparent text-gray-400 hover:text-gray-600"
              }`}
            >
              {t.label}
              {t.badge !== undefined && t.badge > 0 && (
                <span className="ml-1 text-[10px] bg-red-100 text-red-600 px-1 rounded-full">{t.badge}</span>
              )}
            </button>
          ))}
        </div>
      </div>

      {/* Sub-tab content (scrollable) */}
      <div className="flex-1 overflow-y-auto px-4 pb-4">
        {subTab === "issues" && (
          <IssuesTab
            analysis={analysis}
            selectedAnnotation={selectedAnnotation}
            onSelectAnnotation={onSelectAnnotation}
          />
        )}
        {subTab === "compat" && <CompatTab analysis={analysis} />}
        {subTab === "data" && <DataTab analysis={analysis} />}
      </div>
    </div>
  );
}
