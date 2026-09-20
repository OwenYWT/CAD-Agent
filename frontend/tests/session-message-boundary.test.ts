import assert from "node:assert/strict";
import test from "node:test";
import { dispatchSessionMessage } from "../src/controllers/sessionMessages.ts";
import { useSessionStore } from "../src/stores/sessionStore.ts";

test("socket results update the addressed panel while another panel is active", () => {
  const store = useSessionStore.getState();
  store.reset();
  const first = store.getActivePanel().id;
  store.addPanel();
  const second = store.getActivePanel().id;
  assert.notEqual(first, second);
  dispatchSessionMessage({type:"generation_result", data:{panel_id:first, success:true, code:"real-addressed-result"}},
    store.sessionId, useSessionStore.getState(), () => assert.fail("unexpected retransmit"));
  const state = useSessionStore.getState();
  assert.equal(state.panels.find(p=>p.id===first)?.result?.code, "real-addressed-result");
  assert.equal(state.getActivePanel().id, second);
  assert.equal(state.getActivePanel().result, null);
});

test("a missing task error remains scoped to the addressed panel", () => {
  const store = useSessionStore.getState(); store.reset();
  const first = store.getActivePanel().id; store.addPanel();
  dispatchSessionMessage({type:"task_status", data:{panel_id:first, status:"not_found"}},
    store.sessionId, useSessionStore.getState(), () => assert.fail("unexpected retransmit"));
  assert.equal(useSessionStore.getState().getActivePanel().lastError, null);
  assert.equal(useSessionStore.getState().panels.find(p=>p.id===first)?.lastError, "未找到可恢复的任务，请重新提交");
});
