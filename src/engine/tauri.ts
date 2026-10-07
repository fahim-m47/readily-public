import { invoke } from "@tauri-apps/api/core";
import { createEngineClient } from "./client";

export const tauriEngineClient = createEngineClient({ invoke, fetch });
