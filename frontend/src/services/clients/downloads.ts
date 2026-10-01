import { authFetch } from "../../auth";
import { API_BASE } from "./http";

export async function downloadEngineeringArtifact(url: string, filename: string): Promise<void> {
  const external = /^https?:\/\//.test(url);
  const target = external ? url : `${API_BASE}${url}`;
  // S3-compatible presigned URLs already carry short-lived authorization in
  // their query string. Sending the WordsWave bearer token to that host both
  // leaks credentials and can invalidate the object-store signature.
  const response = external ? await fetch(target) : await authFetch(target);
  if (!response.ok) throw new Error(`文件下载失败（HTTP ${response.status}）`);
  const objectUrl = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
}
