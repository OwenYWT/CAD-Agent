"use strict";

const elements = {
  status: document.getElementById("status"),
  prompt: document.getElementById("prompt"),
  plan: document.getElementById("plan"),
  uploadConsent: document.getElementById("upload-consent"),
  f3dConsent: document.getElementById("f3d-consent"),
  cancel: document.getElementById("cancel"),
  proposal: document.getElementById("proposal"),
  preview: document.getElementById("preview"),
  approve: document.getElementById("approve"),
  approveF3d: document.getElementById("approve-f3d"),
  result: document.getElementById("result"),
  error: document.getElementById("error"),
};

let approvalNonce = "";
let f3dConsentNonce = "";

function text(value) {
  if (value === null || value === undefined) return "";
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

async function send(event, payload) {
  if (!globalThis.adsk || typeof adsk.fusionSendData !== "function") {
    throw new Error("Fusion communication is unavailable");
  }
  return await adsk.fusionSendData(event, JSON.stringify(payload));
}

function render(message) {
  elements.status.textContent = text(message.state || "idle");
  elements.proposal.textContent = text(message.proposal);
  elements.preview.textContent = text(message.preview);
  elements.result.textContent = text(message.result || message.message || message.question);
  elements.error.textContent = message.error ? text(message.error.message) : "";
  approvalNonce = typeof message.approval_nonce === "string" ? message.approval_nonce : "";
  f3dConsentNonce = typeof message.f3d_consent_nonce === "string" ? message.f3d_consent_nonce : "";
  elements.approve.hidden = !approvalNonce;
  elements.approveF3d.hidden = !f3dConsentNonce;
}

elements.plan.addEventListener("click", async () => {
  try {
    await send("request_plan", {
      prompt: elements.prompt.value,
      export_artifact_upload_consent: elements.uploadConsent.checked || elements.f3dConsent.checked,
      f3d_upload_authorized: elements.f3dConsent.checked,
    });
  } catch (_error) {
    elements.error.textContent = "Unable to send the planning request.";
  }
});

elements.approve.addEventListener("click", async () => {
  const nonce = approvalNonce;
  approvalNonce = "";
  elements.approve.hidden = true;
  try {
    await send("approve", {approval_nonce: nonce});
  } catch (_error) {
    elements.error.textContent = "Unable to send approval.";
  }
});

elements.approveF3d.addEventListener("click", async () => {
  const nonce = f3dConsentNonce;
  f3dConsentNonce = "";
  elements.approveF3d.hidden = true;
  try {
    await send("approve_f3d_upload", {approval_nonce: nonce});
  } catch (_error) {
    elements.error.textContent = "Unable to authorize F3D upload.";
  }
});

elements.cancel.addEventListener("click", async () => {
  try {
    await send("cancel", {});
  } catch (_error) {
    elements.error.textContent = "Unable to cancel the local request.";
  }
});

globalThis.fusionJavaScriptHandler = {
  handle(action, data) {
    if (action !== "controller_state" || typeof data !== "string" || data.length > 1048576) return;
    try {
      const message = JSON.parse(data);
      if (message && typeof message === "object" && !Array.isArray(message)) render(message);
    } catch (_error) {
      elements.error.textContent = "The Add-in returned an invalid response.";
    }
  },
};
