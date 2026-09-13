import assert from "node:assert/strict";
import test from "node:test";
import { useSessionStore } from "../src/stores/sessionStore.ts";

test("resumable replay keeps the panel clickable instead of generating", () => {
  const store=useSessionStore.getState();store.reset();
  store.setRunCreated({run_id:'legacy',session_id:'session',status:'blocked'});
  assert.equal(store.getActivePanel().isGenerating,false);
  store.setStep({step:'resume_available',message:'可继续任务',status:'running'});
  assert.equal(store.getActivePanel().isGenerating,false);
  store.setRunCreated({run_id:'legacy',session_id:'session',status:'running'});
  assert.equal(store.getActivePanel().isGenerating,true);
  store.setStep({step:'complete',message:'完成',status:'success'});
  assert.equal(store.getActivePanel().isGenerating,false);
});
