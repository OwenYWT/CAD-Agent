import { useState } from "react";
import { authFetch } from "../auth";
import { useCapabilities } from "../hooks/useCapabilities";
import type { CapabilityId } from "../types";

const API_BASE = import.meta.env.VITE_API_BASE || "";

const PARAM_TEMPLATES: Record<string, Record<string, unknown>> = {
  "cad/generate": { input: "uploads/<id>/model.py", output: "model.step" },
  "cad/inspect": { input: "uploads/<id>/model.step", operation: "refs", facts: true, planes: true, positioning: true },
  "cad/snapshot": { input: "uploads/<id>/model.step", output: "snapshot.png", mode: "view" },
  "cad/export": { input: "uploads/<id>/model.step", format: "glb", output: "model.glb" },
  "dxf/generate": { source: "uploads/<id>/drawing.py", output: "drawing.dxf" },
  "dxf/validate": { input: "uploads/<id>/drawing.dxf" },
  "cad-viewer/start": {},
  "cad-viewer/review": { input: "uploads/<id>/model.step" },
  "step-parts/search": { query: "M3 socket head 12", limit: 5 },
  "step-parts/download": { part_id: "part-id", filename: "part.step" },
  "urdf/generate": { source: "uploads/<id>/robot.py", output: "robot.urdf" },
  "srdf/generate": { source: "uploads/<id>/moveit.py", output: "robot.srdf" },
  "sdf/generate": { source: "uploads/<id>/world.py", output: "world.sdf", gz_check: "auto" },
  "sdf/gz-check": { input: "uploads/<id>/world.sdf" },
  "sendcutsend/preflight": { input: "uploads/<id>/drawing.dxf", material_sku: "material-sku", thickness_mm: 1.5, quantity: 1, services: [] },
  "gcode/discover": {},
  "gcode/inspect": { input: "uploads/<id>/model.stl" },
  "gcode/dry-run": { input: "uploads/<id>/model.stl", profile: "uploads/<id>/profile.json", output: "model.gcode", backend: "auto" },
  "gcode/slice": { input: "uploads/<id>/model.stl", profile: "uploads/<id>/profile.json", output: "model.gcode", backend: "auto" },
  "gcode/validate": { gcode: "uploads/<id>/model.gcode", profile: "uploads/<id>/profile.json" },
  "implicit-cad/export": { input: "uploads/<id>/model.implicit.mjs", format: "glb", output: "model.glb" },
  "implicit-cad/snapshot": { input: "uploads/<id>/model.implicit.mjs", output: "snapshot.png", mode: "view" },
  "bambu-labs/dry-run": { gcode: "uploads/<id>/model.gcode", handoff: "plain" },
  "bambu-labs/serial": { printer: "printer-alias", execute: false, confirm_serial: false },
  "bambu-labs/status": { printer: "printer-alias", execute: false, confirm_status: false },
  "bambu-labs/upload": { gcode: "uploads/<id>/model.gcode", printer: "printer-alias", execute: false, confirm_send: false },
  "bambu-labs/start-print": { gcode: "uploads/<id>/model.gcode", printer: "printer-alias", execute: false, confirm_start_print: false },
  "bambu-labs/pause-print": { printer: "printer-alias", execute: false, confirm_pause_print: false },
  "bambu-labs/cancel-print": { printer: "printer-alias", execute: false, confirm_cancel_print: false },
  "bambu-labs/clear-error": { printer: "printer-alias", execute: false, confirm_clear_error: false },
};

const UPLOAD_KEYS = ["input", "source", "profile", "gcode", "config", "template_project", "right"];

interface RunResult {
  status: "succeeded" | "blocked" | "failed" | "dry_run";
  capability: string;
  action: string;
  request_id: string;
  data?: unknown;
  files?: Array<{ name: string; url?: string; sha256?: string; size_bytes?: number }>;
  checks?: Array<Record<string, unknown>>;
  command_preview?: string[];
  blocked_reasons?: string[];
  error?: string;
}

const STATUS_STYLE: Record<RunResult["status"], string> = {
  succeeded: "bg-emerald-50 text-emerald-800",
  dry_run: "bg-sky-50 text-sky-800",
  blocked: "bg-amber-50 text-amber-900",
  failed: "bg-red-50 text-red-800",
};

function stripSecrets(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stripSecrets);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>).map(([key, item]) => [
      key,
      /access.?code|password|secret|token/i.test(key) ? "" : stripSecrets(item),
    ]));
  }
  return value;
}

export default function CapabilityRunner() {
  const { capabilities, loading } = useCapabilities();
  const [capabilityId, setCapabilityId] = useState<CapabilityId>("step-parts");
  const capability = capabilities.find((item) => item.id === capabilityId) ?? capabilities[0];
  const [actionId, setActionId] = useState("search");
  const [paramsText, setParamsText] = useState(() => JSON.stringify(PARAM_TEMPLATES["step-parts/search"], null, 2));
  const [uploadKey, setUploadKey] = useState("input");
  const [uploading, setUploading] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<RunResult | null>(null);

  const action = capability?.actions.find((item) => item.id === actionId) ?? capability?.actions[0];

  const resetForAction = (nextCapabilityId: CapabilityId, nextActionId: string) => {
    setParamsText(JSON.stringify(PARAM_TEMPLATES[`${nextCapabilityId}/${nextActionId}`] ?? {}, null, 2));
    setResult(null);
    setError(null);
  };

  const changeCapability = (nextId: CapabilityId) => {
    const nextCapability = capabilities.find((item) => item.id === nextId);
    const nextAction = nextCapability?.actions[0]?.id ?? "";
    setCapabilityId(nextId);
    setActionId(nextAction);
    resetForAction(nextId, nextAction);
  };

  const changeAction = (nextAction: string) => {
    setActionId(nextAction);
    resetForAction(capabilityId, nextAction);
  };

  const updateUploadedInput = (input: string) => {
    let parsed: Record<string, unknown> = {};
    try {
      parsed = JSON.parse(paramsText) as Record<string, unknown>;
    } catch {
      // A successful upload is not discarded because another field has invalid JSON.
    }
    parsed[uploadKey] = input;
    setParamsText(JSON.stringify(parsed, null, 2));
  };

  const upload = async (file: File) => {
    setUploading(true);
    setError(null);
    try {
      const form = new FormData();
      form.append("file", file);
      const response = await authFetch(`${API_BASE}/api/capability-artifacts`, { method: "POST", body: form });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || `上传失败（HTTP ${response.status}）`);
      updateUploadedInput(body.input);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "上传失败");
    } finally {
      setUploading(false);
    }
  };

  const run = async () => {
    if (!capability || !action) return;
    let params: Record<string, unknown>;
    try {
      const value = JSON.parse(paramsText);
      if (!value || Array.isArray(value) || typeof value !== "object") throw new Error("参数必须是 JSON object");
      params = value as Record<string, unknown>;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "参数 JSON 无效");
      return;
    }
    setRunning(true);
    setResult(null);
    setError(null);
    try {
      const response = await authFetch(`${API_BASE}/api/capability-actions/${capability.id}/${action.id}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ params }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || `运行失败（HTTP ${response.status}）`);
      setResult(body as RunResult);
      setParamsText(JSON.stringify(stripSecrets(params), null, 2));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "运行失败");
    } finally {
      setRunning(false);
    }
  };

  const download = async (file: { name: string; url?: string }) => {
    if (!file.url) return;
    setError(null);
    try {
      const response = await authFetch(`${API_BASE}${file.url}`);
      if (!response.ok) throw new Error(`下载失败（HTTP ${response.status}）`);
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = file.name;
      anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "下载失败");
    }
  };

  if (loading && capabilities.length === 0) return <div className="p-4 text-xs text-slate-500">正在载入能力…</div>;

  return (
    <section aria-labelledby="capability-runner-title" className="space-y-4 p-4">
      <div>
        <h3 className="text-sm font-semibold text-slate-800" id="capability-runner-title">结构化能力运行器</h3>
        <p className="mt-1 text-xs leading-5 text-slate-500">上传输入产物并调用固定 action。参数不能提供任意命令或宿主路径；缺少外部依赖时返回 blocked。</p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <label className="space-y-1 text-[11px] font-medium text-slate-600">
          能力
          <select className="min-h-10 w-full rounded-md border border-slate-200 bg-white px-2 text-xs" onChange={(event) => changeCapability(event.target.value as CapabilityId)} value={capability?.id}>
            {capabilities.map((item) => <option key={item.id} value={item.id}>{item.name}{item.available === false ? "（依赖受限）" : ""}</option>)}
          </select>
        </label>
        <label className="space-y-1 text-[11px] font-medium text-slate-600">
          Action
          <select className="min-h-10 w-full rounded-md border border-slate-200 bg-white px-2 text-xs" onChange={(event) => changeAction(event.target.value)} value={action?.id ?? ""}>
            {capability?.actions.map((item) => <option key={item.id} value={item.id}>{item.id} · {item.name}</option>)}
          </select>
        </label>
      </div>

      {capability?.risk_level === "physical_action" || action?.requires_confirmation ? (
        <div className="rounded-md border border-red-200 bg-red-50 p-3 text-[11px] leading-5 text-red-800">
          此 action 可能影响真实设备。默认不会执行；部署级开关、execute 和 action 专用确认必须同时通过。请确认构建板清空、耗材/喷嘴/机型正确且操作员在场。
        </div>
      ) : null}

      {action?.available === false ? (
        <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-[11px] leading-5 text-amber-900" role="status">
          {action.blocked_reason || "当前部署缺少此 action 所需依赖。"}
        </div>
      ) : null}

      <div className="rounded-md border border-slate-200 bg-slate-50 p-3">
        <div className="flex flex-wrap items-end gap-2">
          <label className="min-w-28 flex-1 space-y-1 text-[11px] font-medium text-slate-600">
            将上传路径写入参数
            <select className="min-h-9 w-full rounded-md border border-slate-200 bg-white px-2 text-xs" onChange={(event) => setUploadKey(event.target.value)} value={uploadKey}>
              {UPLOAD_KEYS.map((key) => <option key={key} value={key}>{key}</option>)}
            </select>
          </label>
          <label className="min-h-9 cursor-pointer rounded-md bg-slate-800 px-3 py-2 text-xs font-medium text-white hover:bg-slate-700">
            {uploading ? "上传中…" : "选择并上传文件"}
            <input className="sr-only" disabled={uploading || running} onChange={(event) => { const file = event.target.files?.[0]; if (file) void upload(file); event.target.value = ""; }} type="file" />
          </label>
        </div>
      </div>

      <label className="block space-y-1 text-[11px] font-medium text-slate-600">
        Action 参数（JSON）
        <textarea aria-label="Action 参数 JSON" className="min-h-52 w-full rounded-md border border-slate-200 bg-slate-950 p-3 font-mono text-[11px] leading-5 text-emerald-300 outline-none focus:border-sky-500" onChange={(event) => setParamsText(event.target.value)} spellCheck={false} value={paramsText} />
      </label>

      <button className="min-h-11 w-full rounded-md bg-sky-700 px-4 text-sm font-medium text-white hover:bg-sky-800 disabled:cursor-not-allowed disabled:opacity-50" disabled={running || uploading || !action || action.available === false} onClick={() => void run()} type="button">
        {running ? "正在运行…" : `运行 ${capability?.name ?? ""} / ${action?.id ?? ""}`}
      </button>

      {error ? <div className="rounded-md bg-red-50 p-3 text-xs text-red-800" role="alert">{error}</div> : null}

      {result ? (
        <div className="space-y-3 rounded-lg border border-slate-200 bg-white p-3">
          <div className="flex items-center justify-between gap-2">
            <span className={`rounded-full px-2 py-1 text-[11px] font-medium ${STATUS_STYLE[result.status]}`}>{result.status}</span>
            <code className="text-[10px] text-slate-400">{result.request_id}</code>
          </div>
          {result.blocked_reasons?.length ? <ul className="list-disc space-y-1 pl-5 text-xs text-amber-900">{result.blocked_reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul> : null}
          {result.error ? <p className="text-xs text-red-700">{result.error}</p> : null}
          {result.files?.length ? (
            <div className="space-y-1">
              <p className="text-[10px] font-medium uppercase tracking-wide text-slate-400">产物</p>
              {result.files.map((file) => (
                <button className="flex min-h-10 w-full items-center justify-between rounded-md border border-slate-200 px-3 text-left text-xs text-slate-700 hover:bg-sky-50" key={`${file.name}:${file.sha256}`} onClick={() => void download(file)} type="button">
                  <span>{file.name}</span><span className="text-sky-700">下载</span>
                </button>
              ))}
            </div>
          ) : null}
          {result.command_preview?.length ? <details><summary className="cursor-pointer text-xs text-slate-500">安全命令预览</summary><pre className="mt-2 overflow-x-auto rounded bg-slate-950 p-2 text-[10px] text-slate-300">{result.command_preview.join(" ")}</pre></details> : null}
          {result.data != null ? <details><summary className="cursor-pointer text-xs text-slate-500">运行数据</summary><pre className="mt-2 max-h-72 overflow-auto rounded bg-slate-50 p-2 text-[10px] text-slate-600">{JSON.stringify(result.data, null, 2)}</pre></details> : null}
        </div>
      ) : null}
    </section>
  );
}
