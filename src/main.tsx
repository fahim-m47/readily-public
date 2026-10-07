import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { tauriEngineClient } from "./engine/tauri";

// Only the macOS window lays its title bar over the page
// (`tauri.macos.conf.json`), so only there does the title bar strip leave
// room for the traffic lights (`App.css`). WebKitGTK names Linux here.
if (navigator.userAgent.includes("Macintosh")) {
  document.documentElement.dataset.titleBar = "overlay";
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App client={tauriEngineClient} />
  </StrictMode>,
);
