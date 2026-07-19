export type ReferenceAttachmentCategory = "image" | "cad" | "unknown";

export interface ReferenceAttachmentInput {
  name: string;
  type?: string | null;
  size: number;
}

export interface ReferenceAttachmentSummary {
  name: string;
  mime_type: string;
  size_bytes: number;
  size_label: string;
  category: ReferenceAttachmentCategory;
}

const IMAGE_EXTENSIONS = new Set(["png", "jpg", "jpeg", "webp"]);
const CAD_EXTENSIONS = new Set(["stl", "step", "stp"]);
const DEFAULT_MIME_TYPE = "application/octet-stream";
const PROMPT_HEADER = "Reference attachments:";
const PROMPT_INTENT = "User intent: use these attachments as shape, proportion, or fit references.";

function extensionOf(name: string) {
  const match = /\.([^.]+)$/.exec(name.trim().toLowerCase());
  return match?.[1] || "";
}

export function isSupportedReferenceAttachment(file: Pick<ReferenceAttachmentInput, "name" | "type">) {
  const mimeType = (file.type || "").toLowerCase();
  const extension = extensionOf(file.name);
  return (
    (mimeType.startsWith("image/") && IMAGE_EXTENSIONS.has(extension)) || CAD_EXTENSIONS.has(extension)
  );
}

export function referenceAttachmentCategory(file: Pick<ReferenceAttachmentInput, "name" | "type">): ReferenceAttachmentCategory {
  const mimeType = (file.type || "").toLowerCase();
  const extension = extensionOf(file.name);

  if (mimeType.startsWith("image/") || IMAGE_EXTENSIONS.has(extension)) {
    return "image";
  }
  if (CAD_EXTENSIONS.has(extension) || mimeType.includes("stl") || mimeType.includes("step")) {
    return "cad";
  }
  return "unknown";
}

export function formatReferenceAttachmentSize(sizeBytes: number) {
  if (!Number.isFinite(sizeBytes) || sizeBytes <= 0) return "0 KB";
  if (sizeBytes < 1024 * 1024) {
    return `${Math.max(1, Math.round(sizeBytes / 1024))} KB`;
  }
  const megabytes = sizeBytes / (1024 * 1024);
  return `${Number.isInteger(megabytes) ? megabytes : megabytes.toFixed(1)} MB`;
}

export function summarizeReferenceAttachment(file: ReferenceAttachmentInput): ReferenceAttachmentSummary {
  return {
    name: file.name.trim(),
    mime_type: file.type?.trim() || DEFAULT_MIME_TYPE,
    size_bytes: Math.max(0, Math.round(file.size)),
    size_label: formatReferenceAttachmentSize(file.size),
    category: referenceAttachmentCategory(file),
  };
}

export function formatReferenceAttachmentsForPrompt(attachments: ReferenceAttachmentSummary[]) {
  const validAttachments = attachments.filter((attachment) => attachment.name);
  if (validAttachments.length === 0) return "";

  const lines = validAttachments.map(
    (attachment) =>
      `Reference attachment: ${attachment.name}, ${attachment.mime_type}, ${attachment.size_label}, category=${attachment.category}`,
  );
  return `${PROMPT_HEADER}\n${lines.join("\n")}\n${PROMPT_INTENT}`;
}

function bytesFromSizeLabel(sizeLabel: string) {
  const match = /([0-9]+(?:\.[0-9]+)?)\s*(KB|MB)/i.exec(sizeLabel);
  if (!match) return 0;
  const value = Number(match[1]);
  const unit = match[2].toUpperCase();
  if (!Number.isFinite(value)) return 0;
  return Math.round(value * (unit === "MB" ? 1024 * 1024 : 1024));
}

export function parseReferenceAttachmentsFromPrompt(prompt?: string | null): ReferenceAttachmentSummary[] {
  if (!prompt) return [];

  return prompt
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.startsWith("Reference attachment:"))
    .map((line) => {
      const payload = line.replace(/^Reference attachment:\s*/, "");
      const parts = payload.split(",").map((part) => part.trim());
      const [name = "", mimeType = DEFAULT_MIME_TYPE, sizeLabel = "0 KB", categoryPart = "category=unknown"] = parts;
      const categoryValue = categoryPart.replace(/^category=/, "");
      const category: ReferenceAttachmentCategory =
        categoryValue === "image" || categoryValue === "cad" ? categoryValue : "unknown";
      return {
        name,
        mime_type: mimeType || DEFAULT_MIME_TYPE,
        size_bytes: bytesFromSizeLabel(sizeLabel),
        size_label: sizeLabel || "0 KB",
        category,
      };
    })
    .filter((attachment) => attachment.name);
}

export function withReferenceAttachmentsPrompt(text: string, attachments: ReferenceAttachmentSummary[]) {
  const attachmentBlock = formatReferenceAttachmentsForPrompt(attachments);
  const trimmedText = text.trim();
  if (!attachmentBlock) return trimmedText;
  if (!trimmedText) return `Use the attached references for this CAD request.\n\n${attachmentBlock}`;
  return `${trimmedText}\n\n${attachmentBlock}`;
}
