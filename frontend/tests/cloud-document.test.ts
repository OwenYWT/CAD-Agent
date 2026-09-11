import test from "node:test";
import assert from "node:assert/strict";
import { applyDocumentEvent } from "../src/adapters/documentAdapter.ts";
import type { CloudDocument, DocumentEvent, SemanticFeature } from "../src/types/document.ts";

const feature = { id: "hole", kernel_name: "Hole", parameters: [{ id: "Hole.Diameter", value: 6 }] } as SemanticFeature;
const doc = { document_id: "doc", head_revision_id: "r1", state_version: 4, event_sequence: 12,
  features: [feature], roots: ["hole"], mesh: { url: "/mesh1" } } as CloudDocument;
const event = { sequence: 13, event_type: "state_delta", payload: {
  base_revision_id: "r1", revision_id: "r2", base_state_version: 4, state_version: 5,
  upserted: [{ ...feature, parameters: [{ id: "Hole.Diameter", value: 8 }] }], removed: [], roots: ["hole"],
  mesh: { url: "/mesh2" }, fcstd: null, state: null, modeling_backend: "freecad", parameter_state_sha256: "new",
} } as DocumentEvent;

test("ordered state delta updates one feature and mesh without mutating committed input", () => {
  const next = applyDocumentEvent(doc, event);
  assert.equal(next.features[0].parameters[0].value, 8);
  assert.equal(next.features[0].id, feature.id);
  assert.equal(next.mesh?.url, "/mesh2");
  assert.equal(doc.features[0].parameters[0].value, 6);
  assert.equal(next.state_version, 5);
  assert.equal(applyDocumentEvent(next, event), next);
});

test("missing events and revision/state conflicts demand a snapshot", () => {
  assert.throws(() => applyDocumentEvent(doc, { ...event, sequence: 14 }), /不连续/);
  assert.throws(() => applyDocumentEvent(doc, { ...event, payload: { ...event.payload, base_revision_id: "wrong" } }), /不连续/);
  assert.throws(() => applyDocumentEvent(doc, { ...event, payload: { ...event.payload, base_state_version: 0 } }), /不连续/);
});

test("comments advance the event cursor without changing geometry", () => {
  const next = applyDocumentEvent(doc, { ...event, event_type: "comment.created" });
  assert.equal(next.mesh, doc.mesh);
  assert.equal(next.features, doc.features);
  assert.equal(next.state_version, 4);
  assert.equal(next.event_sequence, 13);
});

test("versioned feature intent synchronizes without changing the committed geometry", () => {
  const annotation = { sequence: 13, event_type: "feature.annotated", payload: {
    feature_id: "hole", role: "定位孔", intent: "保持同心", annotation_version: 1, annotation_source: "user",
  } } as DocumentEvent;
  const next = applyDocumentEvent(doc, annotation);
  assert.equal(next.features[0].intent, "保持同心");
  assert.equal(next.features[0].annotation_version, 1);
  assert.equal(next.mesh, doc.mesh);
  assert.equal(next.state_version, doc.state_version);
  assert.equal(applyDocumentEvent(next, annotation), next);
  assert.throws(() => applyDocumentEvent(next, { ...annotation, sequence: 14 }), /不连续/);
});
