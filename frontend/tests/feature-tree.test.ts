import assert from "node:assert/strict";
import test from "node:test";
import { featureTreeRows } from "../src/adapters/featureTree.ts";
import type { SemanticFeature } from "../src/types/document.ts";
const feature = (id: string, containers: string[] = [], members: string[] = [], tip: string | null = null) =>
  ({id,kernel_name:id,dependencies:["unrelated"],structure:{category:"feature",container_ids:containers,member_ids:members,body_tip_id:tip}} as SemanticFeature);

test("tree respects native container order and Body Tip, independently of dependencies", () => {
  const result = featureTreeRows([feature("Pad",["Body"]),feature("Body",[],["Sketch","Pad"],"Pad"),feature("Sketch",["Body"])],"measured");
  assert.equal(result.hierarchyAvailable,true);
  assert.deepEqual(result.rows.map(r=>[r.feature.id,r.level,r.bodyTip]),[["Body",1,false],["Sketch",2,false],["Pad",2,true]]);
});
test("legacy, cyclic and ambiguous containment remains a complete flat list", () => {
  const items = [feature("A",["B"],["B"]),feature("B",["A"],["A"])];
  for (const status of [undefined,"measured"]) {
    const result = featureTreeRows(items,status);
    assert.equal(result.hierarchyAvailable,false);
    assert.deepEqual(result.rows.map(r=>[r.feature.id,r.level]),[["A",1],["B",1]]);
  }
  assert.equal(featureTreeRows([feature("A",["B","C"])],"measured").hierarchyAvailable,false);
});
