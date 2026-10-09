import assert from 'node:assert/strict';
import test from 'node:test';
import { workspaceStatus } from '../src/adapters/workspaceStatus.ts';

test('connection loss never claims that a document is currently synchronizing', () => {
  for (const sync of ['syncing', 'synced'] as const) {
    assert.deepEqual(workspaceStatus('disconnected', sync), { tone: 'offline', label: '实时连接已断开' });
  }
  assert.deepEqual(workspaceStatus('connected', 'disconnected'), { tone: 'offline', label: '文档连接已断开' });
});
test('document synchronization errors remain distinct from connectivity and recover on a valid snapshot', () => {
  for (const connection of ['connected', 'connecting', 'reconnecting', 'disconnected'] as const) {
    assert.deepEqual(workspaceStatus(connection, 'failed'), { tone: 'failed', label: '文档同步失败' });
  }
  assert.deepEqual(workspaceStatus('connected', 'syncing'), { tone: 'pending', label: '文档同步中' });
  assert.deepEqual(workspaceStatus('connected', 'synced'), { tone: 'synced', label: '文档已同步' });
  assert.equal(workspaceStatus('connected', 'synced', 'candidate').label, '候选未提交');
});
