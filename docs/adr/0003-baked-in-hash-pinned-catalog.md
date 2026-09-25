# Baked-in, hash-pinned Catalog Manifest and a verified model store

Status: accepted (2026-08-25) · Builds on [ADR 0001](0001-tauri-shell-python-engine.md)

Readily's top threat is its own Catalog: downloadable model weights are code execution on the user's machine, and "nothing leaves your machine" is a headline claim. Catalog v0 trades update flexibility for a structurally small attack surface.

## The decisions

1. **The Catalog Manifest ships inside the app** (`catalog/manifest.json`, bundled at build). There is no runtime catalog fetch in the MVP — the threat model's worst story, "compromised catalog host swaps a model URL", is structurally impossible at launch. Adding or bumping a model is a reviewed PR plus a release; at five models, that's the right cost. The manifest carries `schema_version` from day one so a future remote+signed catalog is an addition, not a migration.
2. **Every model file is pinned twice**: its Hugging Face repo is named with an immutable revision commit, and each file's SHA-256 is recorded in the manifest at curation time. The Engine refuses to load any file that doesn't hash-match, so even a compromised upstream repo cannot reach execution. This is the security pipeline's "model-hash-verification" rule made concrete; the manifest PR is the review point.
3. **Engine-owned model store with a verification invariant.** Models live in `~/Library/Application Support/Readily/models/<name>/<tag>/` — not Hugging Face's cache layout, because a catalog entry may span several HF repos (Kokoro's model and voices files, community MLX quantizations) and deletion must be trivial. Downloads land in a staging directory via `huggingface_hub` (`snapshot_download` with `local_dir`, resumable), are hash-verified, then atomically promoted. Invariant: **a promoted model directory exists ⇔ its contents are verified and complete.** The Engine loads only from promoted paths — a Semgrep-enforceable rule.
4. **No incidental egress.** The Engine sets `HF_HUB_DISABLE_TELEMETRY=1` and confines Hugging Face's transient download metadata inside the Readily tree. Network use is exactly the model downloads the user asked for.
5. **Identity is `name:tag`, and tags name variants, not versions** (`kokoro:82m`, `chatterbox:turbo`, `qwen3-tts:0.6b`; a bare name resolves to the entry's default tag). Weight bumps increment a per-entry `version` field instead of churning tags — History rows stay meaningful across bumps, and Segment-cache keys include the version so cached audio never crosses a weights change.

## Consequences

- Catalog growth is gated on app releases until a signed remote catalog exists (explicitly post-MVP).
- The catalog browser works fully offline: voice metadata and bundled Voice Preview clips (curation artifacts, ~4–8MB) ship with the app.
- The threat model ([docs/threat-model.md](../threat-model.md)) inherits its catalog boundaries from this ADR: manifest integrity at build time, the download-verify-promote seam, and the loads-only-promoted-paths invariant.
- Curation gains a scripted step: regenerate pinned hashes and preview clips whenever a model enters the Catalog or bumps.
