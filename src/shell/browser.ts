import { openUrl } from "@tauri-apps/plugin-opener";

export const tryOpenInBrowser = async (url: string) => {
  try {
    await openUrl(url);
    return true;
  } catch {
    return false;
  }
};
