# Engine-owned SQLite History and a budgeted FLAC Segment store

Status: accepted (2026-08-25) · Builds on [ADR 0001](0001-tauri-shell-python-engine.md), [ADR 0002](0002-chunking-playback-pipeline.md), [ADR 0003](0003-baked-in-hash-pinned-catalog.md)

History must be permanent while its audio is huge — a 2h Narration is hundreds of MB — and ADR 0002 made the content-addressed Segment cache the source of truth while leaving residency, formats, eviction, and History's relationship to the files here. The organizing idea: **the record of a Narration is forever; its audio is reproducible and therefore expendable.**

## The decisions

1. **The Engine owns all persistent data** — the database, the Segment store, and the model store (ADR 0003). The UI reads History over the localhost API like everything else; the Rust core keeps only window chrome state. One writer, no cross-process coordination, and the Engine stays independently curl-able. Python costs no meaningful performance here: SQLite and FLAC are C libraries either way, the audio callback feeds from memory (ADR 0002), and inference dwarfs storage I/O by orders of magnitude — Python only orchestrates.
2. **SQLite (WAL mode) is the app database** — the industry answer for local app data (Apple's own apps, every browser; sqlite.org's recommended application file format). It holds Narrations, their full Source text, Segment manifests (the Narration↔Segment join table), gap records (ADR 0002 §8), a `settings` table (selected model/voice, speed, retention knobs), and a per-Narration resume playhead updated periodically during playback — ADR 0002's crash-resume made concrete. JSON manifests suit immutable blob stores (ADR 0003's models); History is mutable and relational.
3. **Segments are stored as FLAC.** Lossless keeps "cache is source of truth" honest — export never re-degrades — at roughly half of WAV (~95MB per narrated hour at 24kHz mono). Segments decode to exact PCM for ADR 0002's sample-accurate butt-joins.
4. **A Narration is never materialized as an internal file.** It is a manifest of Segment hashes; playback streams from the store. A single audio file exists only through user-triggered **Export** to a location they choose: decode → concat → encode once, **M4A (AAC) by default, WAV as the lossless option**. No MP3.
5. **Retention: metadata forever, audio under two user knobs.** History rows, Source text, and gap records persist until the user deletes the Narration. Audio lives under (a) a **disk budget** (default **5GB** ≈ 50 narrated hours; oldest-out when exceeded) and (b) **keep-audio-for-X-days** (default **Never**, i.e. off). Both knobs are clocked by **last-played** (initialized at creation) — audio you replay never ages out. The budget governs Segments only; models have their own lifecycle (ADR 0003). Evicted audio re-synthesizes silently through the normal pipeline on replay, same <3s first-audio bar. v1 Favorites become the never-evict pin.
6. **Delete is refcount GC.** Content addressing means Narrations can share Segments, so deleting a Narration drops its manifest and sweeps Segments no other manifest references (at delete time, with a lazy startup sweep as backstop). Deleting visibly frees disk — the delete button is not a lie.
7. **Everything lives under `~/Library/Application Support/Readily/`**: `models/` (ADR 0003), `segments/` (hash-named, sharded by prefix), `readily.db`, `engine/` (the uv-provisioned runtime, ADR 0001), `logs/`. Segments deliberately do **not** go in `~/Library/Caches` — the OS and cleaner apps may purge Caches at will, which would silently break the retention promise; Readily runs its own janitor. `models/`, `segments/`, and `engine/` are marked **Time Machine-excluded** (all reproducible); the database — the irreplaceable part — stays backed up.

## Consequences

- The Engine API grows History, settings, and export routes; the UI stays a stateless client. Exact routes are in [docs/wire.md](../wire.md).
- Segment keys include the Catalog entry `version` (ADR 0003 §5), so a weights bump never poisons cached audio — but an *evicted* Narration re-synthesized after a bump won't be byte-identical to the original. Accepted: History guarantees the record, not the waveform.
- The UI surfaces the two retention knobs, disk usage, per-Narration "audio present/cleaned up" state, and the export flow.
- The Engine carries schema + migrations, the GC sweep, the janitor, and Time Machine exclusion via `tmutil`/xattr at directory creation.

## Generation Record amendment — 2026-09-05

Each Segment now persists the Generation Record passed to its lane. A canonical
JSON encoding of the whole record determines its SHA-256 key: model id, Catalog
version, Voice id, Voice Reference digest, decode mode, declared generation
parameters, Block text, and optional explicit seed. With no explicit seed, the
record hash determines the RNG seed. A fixed per-attempt offset keeps gate retries
reproducible. Position, pause settings, and playback speed are outside the record.

Schema 4 preserves History and saved speed values, removes the synthesis speed
constraints, and adds the record alongside each Segment reference. Legacy audio
without a record is regenerated before replay or Export. A missing Segment whose
Catalog version changed is rekeyed with the current entry; retained audio with a
record remains playable. The legacy FLACs themselves are not swept: a format-1
file stays on disk, and counts toward the Segment budget, until its Narration is
replayed and rekeyed or deleted, even though History reports its audio as not
present. The single synthesis interface returns one Block's complete audio.
