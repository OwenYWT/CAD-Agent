import assert from "node:assert/strict";
import test from "node:test";

import {
  formatReferenceAttachmentsForPrompt,
  parseReferenceAttachmentsFromPrompt,
  summarizeReferenceAttachment,
} from "../src/utils/referenceAttachments.ts";

test("summarizes image reference metadata", () => {
  const summary = summarizeReferenceAttachment({
    name: "phone-stand-sketch.png",
    type: "image/png",
    size: 248 * 1024,
  });

  assert.deepEqual(summary, {
    name: "phone-stand-sketch.png",
    mime_type: "image/png",
    size_bytes: 253952,
    size_label: "248 KB",
    category: "image",
  });
});

test("summarizes CAD reference metadata from extension fallback", () => {
  const summary = summarizeReferenceAttachment({
    name: "bracket.STL",
    type: "",
    size: 2_621_440,
  });

  assert.equal(summary.category, "cad");
  assert.equal(summary.mime_type, "application/octet-stream");
  assert.equal(summary.size_label, "2.5 MB");
});

test("formats and parses reference attachment prompt block", () => {
  const attachments = [
    summarizeReferenceAttachment({ name: "sketch.webp", type: "image/webp", size: 12_288 }),
    summarizeReferenceAttachment({ name: "fit.step", type: "", size: 4096 }),
  ];

  const promptBlock = formatReferenceAttachmentsForPrompt(attachments);

  assert.match(promptBlock, /Reference attachment: sketch\.webp, image\/webp, 12 KB, category=image/);
  assert.match(promptBlock, /Reference attachment: fit\.step, application\/octet-stream, 4 KB, category=cad/);
  assert.match(promptBlock, /User intent: use these attachments as shape, proportion, or fit references/);
  assert.deepEqual(parseReferenceAttachmentsFromPrompt(`make holder

${promptBlock}`), attachments);
});
