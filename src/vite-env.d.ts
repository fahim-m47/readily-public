/// <reference types="vite/client" />

interface ImportMetaEnv {
  // "ogg" in a Linux build, whose Voice Previews are Opus rather than the
  // Catalog's AAC; unset everywhere else. Set by scripts/opus-previews.sh.
  readonly VITE_PREVIEW_FORMAT?: "ogg";
}
