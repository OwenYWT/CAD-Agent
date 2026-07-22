import { useEffect, useState } from "react";
import type { GenerationResult } from "../types";
import { authFetch } from "../auth";
import { buildArtifactManifest, manifestFilename, manifestJson } from "../utils/artifactManifest";

const API_BASE = import.meta.env.VITE_API_BASE || "";

interface DownloadPanelProps {
  files: Record<string, string> | null;
  requestId: string | null;
  hasResult: boolean;
  result?: GenerationResult | null;
  prompt?: string | null;
}

interface OnshapeLink {
  request_id: string;
  status: string;
  onshape_url: string;
  document_id: string;
  workspace_id: string;
  element_id?: string | null;
  translation_id?: string | null;
  document_name?: string;
  step_filename?: string;
  mode?: string;
  created_at?: string;
  updated_at?: string;
}

const FILE_ICONS: Record<string, string> = {
  step: "STEP",
  stl: "STL",
  dxf: "DXF",
  svg: "SVG",
  png: "PNG",
};

const FILE_LABELS: Record<string, string> = {
  step: "STEP 3D 模型",
  stl: "STL 预览/打印",
  dxf: "DXF 2D 图纸",
  svg: "SVG 2D 预览",
  png: "PNG 渲染图",
};

const FILE_DESCRIPTIONS: Record<string, string> = {
  step: "用于机械 CAD、装配和后续编辑",
  stl: "用于网页预览、切片和 3D 打印",
  dxf: "用于 2D CAD、激光切割和工程图",
  svg: "用于浏览器查看和文档展示",
  png: "用于快速分享和方案汇报",
};

function downloadTextFile(filename: string, text: string, mimeType: string) {
  const blob = new Blob([text], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

export default function DownloadPanel({ files, requestId, hasResult, result, prompt }: DownloadPanelProps) {
  const [onshapeLink, setOnshapeLink] = useState<OnshapeLink | null>(null);
  const [isPublishing, setIsPublishing] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [publishError, setPublishError] = useState<string | null>(null);
  const [publishMessage, setPublishMessage] = useState<string | null>(null);

  useEffect(() => {
    setOnshapeLink(null);
    setPublishError(null);
    setPublishMessage(null);
    if (!requestId) return;

    const currentRequestId = requestId;
    let cancelled = false;

    async function loadOnshapeLinks() {
      try {
        const response = await authFetch(`${API_BASE}/api/onshape/links/${encodeURIComponent(currentRequestId)}`);
        if (!response.ok) return;
        const links = (await response.json()) as OnshapeLink[];
        if (!cancelled) setOnshapeLink(links[0] || null);
      } catch {
        if (!cancelled) setPublishMessage(null);
      }
    }

    loadOnshapeLinks();
    return () => {
      cancelled = true;
    };
  }, [requestId]);

  if (!hasResult) {
    return (
      <div className="p-4 text-sm text-gray-500">
        {"生成模型后，可在这里下载 STEP、STL、DXF、SVG 等网页端产物。"}
      </div>
    );
  }

  const fileEntries = files ? Object.entries(files).filter(([, url]) => !!url) : [];
  const stepUrl = files?.step || files?.stp || null;
  const canPublishToOnshape = Boolean(requestId && stepUrl);

  const handleManifestDownload = () => {
    if (!result) return;
    const manifest = buildArtifactManifest(result, { prompt });
    downloadTextFile(
      manifestFilename(result.request_id || requestId),
      manifestJson(manifest),
      "application/json",
    );
  };

  const refreshOnshapeStatus = async () => {
    if (!requestId || !onshapeLink?.translation_id) return;
    setIsRefreshing(true);
    setPublishError(null);
    try {
      const response = await authFetch(`${API_BASE}/api/onshape/links/${encodeURIComponent(requestId)}/refresh`, {
        method: "POST",
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        const detail = data?.detail ?? data;
        throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail || "刷新失败"));
      }
      setOnshapeLink(data as OnshapeLink);
      setPublishMessage("已刷新 Onshape 导入状态。");
    } catch (error) {
      setPublishError(error instanceof Error ? error.message : "刷新 Onshape 状态失败");
    } finally {
      setIsRefreshing(false);
    }
  };

  const publishToOnshape = async () => {
    if (!requestId || !stepUrl) return;
    setIsPublishing(true);
    setPublishError(null);
    setPublishMessage("正在提交 STEP 到 Onshape...");
    try {
      const stepFilename = decodeURIComponent(stepUrl.split("/").pop()?.split("?")[0] || "");
      const response = await authFetch(`${API_BASE}/api/onshape/publish`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          request_id: requestId,
          step_filename: stepFilename || undefined,
          wait_for_completion: false,
        }),
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        const detail = data?.detail ?? data;
        throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail || "发布失败"));
      }
      setOnshapeLink(data as OnshapeLink);
      setPublishMessage("已提交到 Onshape，导入任务可能仍在后台处理中，可稍后刷新状态。");
    } catch (error) {
      setPublishError(error instanceof Error ? error.message : "发布到 Onshape 失败");
      setPublishMessage(null);
    } finally {
      setIsPublishing(false);
    }
  };

  return (
    <div className="p-4 space-y-4">
      <div>
        <h4 className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-2">
          {"模型文件"}
        </h4>
        {fileEntries.length > 0 ? (
          <div className="space-y-2">
            {fileEntries.map(([format, url]) => (
              <a
                key={format}
                href={url}
                download
                className="flex items-center gap-3 px-3 py-2.5 bg-white border border-gray-200 rounded-lg hover:border-indigo-300 hover:bg-indigo-50 transition-colors group"
              >
                <span className="text-xs font-semibold text-indigo-500 w-10">{FILE_ICONS[format] || "FILE"}</span>
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-medium text-gray-700 group-hover:text-indigo-700">
                    {FILE_LABELS[format] || format.toUpperCase()}
                  </div>
                  <div className="text-[11px] text-gray-400 truncate">
                    {FILE_DESCRIPTIONS[format] || "点击下载生成文件"}
                  </div>
                </div>
                <span className="text-xs text-indigo-500">{"下载"}</span>
              </a>
            ))}
          </div>
        ) : (
          <div className="rounded-lg border border-dashed border-gray-200 bg-white p-3 text-sm text-gray-500">
            {"当前结果没有可下载文件。"}
          </div>
        )}
      </div>

      <div>
        <h4 className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-2">
          {"工程交付包"}
        </h4>
        <button
          type="button"
          onClick={handleManifestDownload}
          disabled={!result}
          className="w-full flex items-center gap-3 px-3 py-2.5 bg-white border border-gray-200 rounded-lg hover:border-purple-300 hover:bg-purple-50 transition-colors group disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <span className="text-xs font-semibold text-purple-500 w-10">JSON</span>
          <div className="min-w-0 flex-1 text-left">
            <div className="text-sm font-medium text-gray-700 group-hover:text-purple-700">
              {"Manifest 清单 JSON"}
            </div>
            <div className="text-[11px] text-gray-400 truncate">
              {"保存来源、参数、简报、检查结果、修复记录和参考附件"}
            </div>
          </div>
          <span className="text-xs text-purple-500">{"下载"}</span>
        </button>
      </div>

      <div className="rounded-lg bg-indigo-50 border border-indigo-100 p-3 text-xs text-indigo-700 leading-5">
        <div className="font-medium mb-1">Web {"版工作流"}</div>
        <p>{"在浏览器完成生成、预览、参数调整和下载；不再依赖桌面 CAD 插件。"}</p>
        {requestId && <p className="mt-1 text-indigo-500">{"请求 ID："}{requestId}</p>}
      </div>

      <div className="rounded-lg bg-sky-50 border border-sky-100 p-3 text-xs text-sky-800 leading-5">
        <div className="flex items-center justify-between gap-3 mb-2">
          <div>
            <div className="font-medium">Onshape 云端 CAD</div>
            <p className="text-sky-600">将 STEP 文件发布到 Onshape 文档，用于浏览器查看、分享和协作。</p>
          </div>
          <button
            type="button"
            onClick={publishToOnshape}
            disabled={!canPublishToOnshape || isPublishing}
            className={`px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${
              canPublishToOnshape && !isPublishing
                ? "bg-sky-600 text-white hover:bg-sky-700"
                : "bg-gray-200 text-gray-400 cursor-not-allowed"
            }`}
          >
            {isPublishing ? "发布中..." : "发布到 Onshape"}
          </button>
        </div>

        {!stepUrl && <p className="text-amber-600">当前结果没有 STEP 文件，无法发布到 Onshape。</p>}
        {publishMessage && <p className="text-sky-600">{publishMessage}</p>}
        {publishError && <p className="text-red-600 break-words">{publishError}</p>}
        {onshapeLink && (
          <div className="mt-2 rounded-md bg-white border border-sky-100 p-2">
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="font-medium text-sky-700 truncate">{onshapeLink.document_name || "Onshape 文档"}</div>
                <div className="text-[11px] text-sky-500 truncate">
                  状态：{onshapeLink.status} {onshapeLink.translation_id ? `· Translation：${onshapeLink.translation_id}` : ""}
                </div>
              </div>
              <a
                href={onshapeLink.onshape_url}
                target="_blank"
                rel="noreferrer"
                className="shrink-0 text-xs font-medium text-sky-600 hover:text-sky-800"
              >
                打开 Onshape
              </a>
            </div>
            {onshapeLink.translation_id && (
              <button
                type="button"
                onClick={refreshOnshapeStatus}
                disabled={isRefreshing}
                className="mt-2 text-[11px] font-medium text-sky-600 hover:text-sky-800 disabled:text-gray-400"
              >
                {isRefreshing ? "刷新中..." : "刷新导入状态"}
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
