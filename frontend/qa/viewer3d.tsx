import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import Viewer3D from "../src/components/Viewer3D";
import "../src/index.css";

const artifactUrl = new URLSearchParams(window.location.search).get("artifact_url");

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <main className="h-screen w-screen" data-qa-production-viewer="true">
      {artifactUrl ? (
        <Viewer3D stlUrl={artifactUrl} />
      ) : (
        <p role="alert">artifact_url is required</p>
      )}
    </main>
  </StrictMode>,
);
