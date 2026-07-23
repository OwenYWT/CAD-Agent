import { useEffect, useMemo, useState } from "react";
import { authFetch } from "../auth";
import { LOCAL_CAPABILITIES } from "../capabilities/manifest";
import type { CapabilityDefinition } from "../types";

const API_BASE = import.meta.env.VITE_API_BASE || "";

function normalizePayload(payload: unknown): CapabilityDefinition[] | null {
  if (Array.isArray(payload)) return payload as CapabilityDefinition[];
  if (payload && typeof payload === "object") {
    const value = payload as { capabilities?: unknown; items?: unknown };
    if (Array.isArray(value.capabilities)) return value.capabilities as CapabilityDefinition[];
    if (Array.isArray(value.items)) return value.items as CapabilityDefinition[];
  }
  return null;
}

export function useCapabilities() {
  const [remote, setRemote] = useState<CapabilityDefinition[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let disposed = false;
    void authFetch(`${API_BASE}/api/capabilities`)
      .then(async (response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const parsed = normalizePayload(await response.json());
        if (!parsed) throw new Error("Invalid capability response");
        if (!disposed) setRemote(parsed);
      })
      .catch((reason: unknown) => {
        if (!disposed) setError(reason instanceof Error ? reason.message : "能力状态不可用");
      })
      .finally(() => {
        if (!disposed) setLoading(false);
      });
    return () => { disposed = true; };
  }, []);

  const capabilities = useMemo(() => {
    if (!remote) return LOCAL_CAPABILITIES.map((item) => ({ ...item, available: undefined }));
    const byId = new Map(remote.map((item) => [item.id, item]));
    return LOCAL_CAPABILITIES.map((fallback) => {
      const server = byId.get(fallback.id);
      return server
        ? {
            ...fallback,
            ...server,
            // Presentation taxonomy and localized copy remain frontend-owned;
            // the server owns runtime state, actions, dependencies and risk.
            name: fallback.name,
            group: fallback.group,
            summary: fallback.summary,
          }
        : fallback;
    });
  }, [remote]);

  return { capabilities, loading, error, source: remote ? "server" as const : "fallback" as const };
}
