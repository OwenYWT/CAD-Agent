import { useCallback, useEffect, useRef, useState } from "react";
import { getAuthToken } from "../auth";
import { webSocketAuthProtocol } from "../adapters/durableTaskAdapter";
import { applyDocumentEvent } from "../adapters/documentAdapter";
import { observeDocument, forgetDocument } from "../stores/documentHeads";
import { getCloudDocument, updateDocumentPresence } from "../services/clients/documents";
import { guardDraft } from "../stores/draftGuard";
import type { CloudDocument, DocumentCollaboration, DocumentEvent, SelectionContext } from "../types/document";
import type { DocumentSyncStatus } from '../adapters/workspaceStatus';

export interface CloudDocumentConnection {
  document: CloudDocument | null; collaboration: DocumentCollaboration | null;
  selectedId: string | null; select: (id: string | null) => void;
  connected: boolean; error: string | null;
  syncStatus?: DocumentSyncStatus;
  selectionContext?: SelectionContext | null;
  clearSelection?: () => void;
  selectTopology?: (id: string, selector: NonNullable<SelectionContext["topology_selector"]>) => void;
  refresh?: () => Promise<CloudDocument | null>;
}

export function useCloudDocument(documentId: string | null): CloudDocumentConnection {
  const [snapshot, setSnapshot] = useState<CloudDocument | null>(null);
  const [collaboration, setCollaboration] = useState<DocumentCollaboration | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedContext, setSelectedContext] = useState<(SelectionContext & { documentId: string }) | null>(null);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sync, setSync] = useState<{ documentId: string | null; status: DocumentSyncStatus }>({ documentId, status: 'syncing' });
  const current = useRef<CloudDocument | null>(null);
  const selection = useRef<string | null>(null);
  const refresh = useCallback(async () => {
    if (!documentId) return null;
    let doc: CloudDocument;
    try { doc = await getCloudDocument(documentId); }
    catch (reason) {
      if (current.current?.document_id === documentId) { setError(reason instanceof Error ? reason.message : '文档同步失败'); setSync({ documentId, status: 'failed' }); }
      throw reason;
    }
    if (current.current?.document_id !== documentId) return null;
    if (doc.event_sequence >= current.current.event_sequence) {
      current.current = doc; observeDocument(doc); setSnapshot(doc); setError(null); setSync({ documentId, status: connected ? 'synced' : 'disconnected' });
    }
    return current.current;
  }, [documentId, connected]);
  useEffect(() => { selection.current = selectedId; }, [selectedId]);

  useEffect(() => {
    if (!documentId) return;
    let closed = false;
    let socket: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout>;
    const clientId = crypto.randomUUID();
    const heartbeat = () => {
      if (!closed && socket?.readyState === WebSocket.OPEN) {
        void updateDocumentPresence(documentId, clientId, selection.current).catch((e: unknown) => {
          if (!closed) setError(e instanceof Error ? e.message : "在线状态同步失败");
        });
      }
    };
    const open = () => {
      if (closed) return;
      if (!navigator.onLine) {
        setConnected(false); setSync({ documentId, status: 'disconnected' }); return;
      }
      const base = new URL(import.meta.env.VITE_API_BASE || window.location.origin);
      base.protocol = base.protocol === "https:" ? "wss:" : "ws:";
      base.pathname = `/api/documents/${encodeURIComponent(documentId)}/stream`;
      const workspace = new URLSearchParams(window.location.search).get("workspace");
      if (workspace) base.searchParams.set("workspace", workspace);
      const auth = webSocketAuthProtocol(getAuthToken() || import.meta.env.VITE_API_TOKEN);
      const stream = new WebSocket(base.toString(), auth ? [auth] : undefined);
      socket = stream;
      stream.onmessage = (message) => {
        if (closed || socket !== stream) return;
        try {
          const event = JSON.parse(message.data) as { type: string; data: unknown };
          if (event.type === "document_snapshot") {
            const doc = event.data as CloudDocument;
            if (doc.document_id !== documentId) throw new Error("文档身份不匹配");
            current.current = doc;
            observeDocument(doc);
            setSnapshot(doc); setError(null); setConnected(true); setSync({ documentId, status: 'synced' });
            heartbeat();
          } else if (event.type === "document_event" && current.current?.document_id === documentId) {
            const doc = applyDocumentEvent(current.current, event.data as DocumentEvent);
            current.current = doc; setSnapshot(doc);
            observeDocument(doc);
          } else if (event.type === "document_collaboration") {
            setCollaboration(event.data as DocumentCollaboration);
          }
        } catch (e) {
          setError(e instanceof Error ? e.message : "文档同步失败");
          setSync({ documentId, status: 'failed' });
          stream.close();
        }
      };
      stream.onerror = () => { if (!closed && socket === stream) { setError("文档连接失败，正在重连"); setSync({ documentId, status: 'disconnected' }); } };
      stream.onclose = (event) => {
        if (closed || socket !== stream) return;
        setConnected(false);
        setSync(previous => ({ documentId, status: previous.documentId === documentId && previous.status === 'failed' ? 'failed' : 'disconnected' }));
        if (event.code === 4003) {
          current.current = null; setSnapshot(null); setCollaboration(null); setSelectedId(null); setSelectedContext(null);
          forgetDocument(documentId); setError("文档访问权限已失效"); setSync({ documentId, status: 'failed' }); return;
        }
        if (navigator.onLine) timer = setTimeout(open, 2000);
      };
    };
    const offline = () => {
      clearTimeout(timer); socket?.close();
      setConnected(false); setSync({ documentId, status: 'disconnected' });
    };
    const online = () => {
      clearTimeout(timer);
      if (socket?.readyState !== WebSocket.OPEN && socket?.readyState !== WebSocket.CONNECTING) open();
    };
    window.addEventListener('offline', offline); window.addEventListener('online', online);
    open();
    const interval = setInterval(heartbeat, 15000);
    return () => {
      closed = true; clearTimeout(timer); clearInterval(interval); socket?.close(); forgetDocument(documentId);
      window.removeEventListener('offline', offline); window.removeEventListener('online', online);
    };
  }, [documentId]);

  const document = snapshot?.document_id === documentId ? snapshot : null;
  const select = (id: string | null) => {
    if (id === selectedId && selectedContext?.revision_id === document?.head_revision_id) return;
    guardDraft(() => {
      setSelectedId(id);
      setSelectedContext(document && id ? { documentId: document.document_id, revision_id: document.head_revision_id,
        state_version: document.state_version, feature_ids: [id] } : null);
    });
  };
  const selectTopology = (id: string, selector: NonNullable<SelectionContext["topology_selector"]>) => {
    const feature = document?.features.find(item => item.id === id);
    if (!document || selector.revision_id !== document.head_revision_id || !feature?.topology_bindings?.some(binding => JSON.stringify(binding) === JSON.stringify(selector))) return;
    guardDraft(() => { setSelectedId(id); setSelectedContext({ documentId: document.document_id, revision_id: document.head_revision_id, state_version: document.state_version, feature_ids: [id], topology_selector: selector }); });
  };
  const selectionContext = document && selectedContext?.documentId === document.document_id
    && selectedContext.revision_id === document.head_revision_id && selectedContext.state_version === document.state_version
    ? { revision_id: selectedContext.revision_id, state_version: selectedContext.state_version, feature_ids: selectedContext.feature_ids, ...(selectedContext.topology_selector ? { topology_selector: selectedContext.topology_selector } : {}) } : null;
  return { document, collaboration: document ? collaboration : null,
    selectedId: document?.features.some((f) => f.id === selectedId) ? selectedId : null,
    select, selectTopology, selectionContext, clearSelection: () => setSelectedContext(null), refresh, connected: Boolean(document && connected),
    syncStatus: sync.documentId === documentId ? sync.status : 'syncing', error };
}
