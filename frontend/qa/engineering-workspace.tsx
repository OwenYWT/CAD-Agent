import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { LOCAL_DEV_USER } from "../src/auth";
import ErrorBoundary from "../src/components/ErrorBoundary";
import EngineeringWorkspace from "../src/components/workspace/EngineeringWorkspace";
import { I18nProvider } from "../src/i18n/I18nProvider";
import "../src/index.css";

// The harness intentionally mounts the same production workspace and stores.
// It contains no alternate layout, fixture interactions, or production branch.
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <I18nProvider>
      <ErrorBoundary>
        <EngineeringWorkspace
          onLogout={() => undefined}
          onUserUpdate={() => undefined}
          user={LOCAL_DEV_USER}
        />
      </ErrorBoundary>
    </I18nProvider>
  </StrictMode>,
);
