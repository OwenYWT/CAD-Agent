import { useCallback, useEffect, useRef, useState } from "react";
import { acquireDocumentFeatureLease, releaseDocumentFeatureLease } from "../services/engineeringService";
import type { CloudDocument } from "../types/document";

export function useFeatureLease(document: CloudDocument, featureId: string) {
  const [clientId] = useState(() => crypto.randomUUID());
  const [held, setHeld] = useState(false);
  const [error, setError] = useState("");
  const token = useRef<string | null>(null);
  const latest = useRef(document);
  const active = useRef(true);
  const inflight = useRef<Promise<string> | null>(null);
  useEffect(() => { latest.current = document; }, [document]);
  const ensure = useCallback((): Promise<string> => {
    if (inflight.current) return inflight.current;
    const request = async () => {
      try {
        const result = await acquireDocumentFeatureLease(latest.current, featureId, clientId, token.current || undefined);
        if (!active.current) {
          await releaseDocumentFeatureLease(latest.current.document_id, result.token);
          throw new Error("编辑会话已关闭");
        }
        token.current = result.token; setHeld(true); setError("");
        return result.token;
      } catch (e) {
        token.current = null;
        if (active.current) { setHeld(false); setError(e instanceof Error ? e.message : "编辑租约已失效"); }
        throw e;
      } finally { inflight.current = null; }
    };
    inflight.current = request();
    return inflight.current;
  }, [featureId, clientId]);
  const release = useCallback(async () => {
    const current = token.current;
    if (current) {
      await releaseDocumentFeatureLease(latest.current.document_id, current);
      if (token.current === current) token.current = null;
      if (active.current) setHeld(false);
    }
    if (active.current) setError("");
  }, []);
  useEffect(() => {
    active.current = true;
    const interval = setInterval(() => { if (token.current) void ensure().catch(() => {}); }, 30000);
    return () => {
      active.current = false; clearInterval(interval);
      if (token.current) void releaseDocumentFeatureLease(latest.current.document_id, token.current).catch(() => {});
    };
  }, [ensure]);
  return { ensure, release, held, error };
}
