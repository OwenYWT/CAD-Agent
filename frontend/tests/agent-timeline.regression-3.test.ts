import assert from "node:assert/strict";
import test from "node:test";

import { engineeringTaskEventLabel } from "../src/utils/engineeringLabels.ts";


// Regression: QA ISSUE-003 — Change Set audit event names leaked into Chinese UI
test("Change Set audit events have Chinese presentation labels", () => {
  assert.equal(
    engineeringTaskEventLabel("change_set.evidence_updated"),
    "变更证据已更新",
  );
  assert.equal(
    engineeringTaskEventLabel("change_set.accepted"),
    "变更已接受",
  );
  assert.equal(
    engineeringTaskEventLabel("change_set.committed"),
    "版本已提交",
  );
});
