import { invoke } from "@tauri-apps/api/core";
import { save } from "@tauri-apps/plugin-dialog";
import { createEngineClient } from "./client";

export const tauriEngineClient = createEngineClient({ invoke, fetch, save });
