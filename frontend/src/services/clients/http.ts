export { authFetch } from "../../auth";
export const API_BASE = import.meta.env.VITE_API_BASE || "";

function apiErrorMessage(detail: unknown, fallback: string): string {
  if (typeof detail === "string" && detail.trim()) return detail;
  if (detail && typeof detail === "object") {
    const record = detail as Record<string, unknown>;
    if (typeof record.message === "string" && record.message.trim()) return record.message;
    if (typeof record.detail === "string" && record.detail.trim()) return record.detail;
    if (record.detail) return apiErrorMessage(record.detail, fallback);
  }
  return fallback;
}

export class EngineeringApiError extends Error {
  code: string | null;
  retryable: boolean;
  httpStatus?: number;

  constructor(message: string, code: string | null, retryable = false, httpStatus?: number) {
    super(message);
    this.name = "EngineeringApiError";
    this.code = code;
    this.retryable = retryable;
    this.httpStatus = httpStatus;
  }
}

export async function readJson<T>(response: Response, fallback: string): Promise<T> {
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as { detail?: unknown };
    const nested = body.detail && typeof body.detail === "object"
      ? body.detail as Record<string, unknown>
      : null;
    throw new EngineeringApiError(
      apiErrorMessage(body.detail, fallback),
      typeof nested?.code === "string" ? nested.code : null,
      nested?.retryable === true,
      response.status,
    );
  }
  return response.json() as Promise<T>;
}
