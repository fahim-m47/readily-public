import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { tauriEngineClient } from "./engine/tauri";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App client={tauriEngineClient} />
  </StrictMode>,
);
