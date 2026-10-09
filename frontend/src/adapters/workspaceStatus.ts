import type { ConnectionState } from '../types/index.ts';
import type { DocumentViewMode } from '../types/document.ts';

export type DocumentSyncStatus = 'syncing' | 'synced' | 'disconnected' | 'failed';

export function workspaceStatus(connection: ConnectionState, sync: DocumentSyncStatus, mode?: DocumentViewMode) {
  if (sync === 'failed') return { tone: 'failed', label: '文档同步失败' };
  if (sync === 'disconnected') return { tone: 'offline', label: '文档连接已断开' };
  if (connection === 'disconnected') return { tone: 'offline', label: '实时连接已断开' };
  if (connection === 'connecting' || connection === 'reconnecting') return { tone: 'pending', label: '实时连接中' };
  if (sync === 'syncing') return { tone: 'pending', label: '文档同步中' };
  if (mode === 'candidate') return { tone: 'pending', label: '候选未提交' };
  return { tone: 'synced', label: mode === 'history' ? '历史只读' : '文档已同步' };
}
