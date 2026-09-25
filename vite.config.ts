/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Tauri loads the dev server from a fixed loopback origin; the strict port
// keeps that origin stable so the dev CSP can name it exactly.
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    host: "127.0.0.1",
    port: 1420,
    strictPort: true,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    // Stylesheets are stubbed to empty modules unless this is on, and
    // `contrast.test.ts` reads `App.css` as text to hold the shell's
    // muted tones to WCAG's 4.5:1. A stub would read as an empty file and
    // pass every assertion in it.
    css: true,
  },
});
