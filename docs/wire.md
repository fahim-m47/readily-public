# The Engine wire, v1

The frozen conventions for talking to the Engine over its loopback HTTP+SSE
API (ADR 0001 §4). This document is the client contract: everything here is
enough to write a client without reading Engine source, and
`engine/tests/test_server_wire.py` pins it. New *routes* do not change
these conventions.

## Reaching the Engine

The Engine listens on `http://127.0.0.1:<port>`, where the port is chosen by
the OS at each launch. Nothing binds beyond loopback, ever (threat model B3).

Inside the app, the webview learns where the Engine is through two Tauri
commands provided by the supervisor:

- `engine_status` → the supervisor's view of the Engine:
  `{"state": "provisioning" | "starting" | "ready" | "restarting" | "failed", …}`
  (`ready` carries `port`; `starting`/`restarting` carry `attempt`;
  `restarting` carries `retryInMs`; `failed` carries `reason`). The webview
  polls it; the supervisor emits no Tauri events.
  `provisioning` is the first-run environment build and is published only
  when there is one to do — a launch whose environment already matches the
  lockfile never passes through it. It carries `note`: the last line `uv`
  wrote, republished as it arrives, or `null` before it has written one.
  There is no percentage to go with it — the wheels are resolved as the
  build goes — so `note` is the only progress this step has.
- `engine_config` → `{"port": number, "token": string}`, or `null` whenever
  the Engine is not ready.
- `engine_retry` → start the Engine over after the supervisor gave up.
  `failed` is not terminal: the supervisor waits there until this is called
  (or the app quits), and the status then moves back through
  `provisioning`/`starting` with a fresh restart budget. A no-op in every
  other state, and it carries no arguments.

A restarted Engine gets a **new port and a new token**, so a client must
re-read `engine_config` every time the status becomes `ready`, and must
treat a dropped SSE connection as "go back and ask the supervisor again".

Outside the app (tests, curl), run the Engine by hand: it announces
`READILY_ENGINE_PORT=<port>` on stdout after binding, and reads its token
from the `READILY_ENGINE_TOKEN` environment variable (generating and
printing one to stderr when unset).

## Auth

Every HTTP request carries the per-launch bearer token:

```
Authorization: Bearer <token>
```

- A missing or wrong token gets `401` with the error envelope below
  (`code: "unauthorized"`), on every route including `/health`. Routes are
  not enumerable without the token.
- Requests bearing a browser `Origin` header outside the allowlist get
  `403` (`code: "forbidden_origin"`). The allowlist is the webview's origin
  `tauri://localhost`, plus `http://127.0.0.1:1420` only when a debug
  supervisor sets `READILY_ENGINE_ALLOW_DEV_ORIGIN=1`. Requests with no
  `Origin` (curl, the supervisor) pass — the token is their gate.
- The browser's credential-free CORS preflight (`OPTIONS`) is answered for
  allowlisted origins without a token; a foreign origin's preflight gets a
  `400` with no `access-control-allow-origin`, so the browser never sends
  the real request.
- WebSocket handshakes are refused outright. The wire is HTTP and SSE only.

## Versioning

- These compatibility rules start when Readily ships its first release.
  Before then, an unreleased v1 shape may be corrected without adding a
  version that no released client consumed.
- Routes live under `/v1` (`/health` is the one unversioned route).
- Every JSON payload the Engine writes — success bodies, error envelopes,
  SSE event data — carries `"version": 1`. A client must reject a payload
  whose version it does not know rather than guess at its shape.
- Breaking changes mean `/v2` and `"version": 2`; v1 shapes never mutate.
  A v1 shape may gain a field; no field is ever removed or retyped, so a
  client keeps reading the fields it knows and ignores the rest.

## Errors

Every error, from middleware or route, is this one envelope. The sole
exception is a foreign origin's CORS preflight: the CORS layer answers it
before any route, so it is a `400` carrying the plain-text body
`Disallowed CORS origin` rather than an envelope. It does echo the
allowlist's `access-control-allow-methods` and
`access-control-allow-headers` — those describe the allowlist, not this
origin, and are inert without the `access-control-allow-origin` the layer
withholds, which is what makes the browser fail the preflight.

```json
{"error": {"version": 1, "code": "<code>", "message": "<sentence>"}}
```

`code` is the machine-readable part and is stable; `message` is a
human-readable sentence and is not for switching on. The v1 codes:

| HTTP | `code`                 | Meaning                                            |
| ---- | ---------------------- | -------------------------------------------------- |
| 401  | `unauthorized`         | Missing or wrong bearer token.                     |
| 403  | `forbidden_origin`     | Browser origin outside the allowlist.              |
| 422  | `invalid_request`      | Body does not match the route's v1 contract.       |
| 404  | `not_found`            | No such route (authenticated requests only).       |
| 405  | `method_not_allowed`   | Route exists, method does not; echoes `Allow`.     |
| 404  | `unknown_model`        | The model reference names no Catalog entry.        |
| 409  | `model_not_installed`  | Narrating needs a model that is not downloaded.    |
| 409  | `model_unsupported`    | This machine lacks what the model runs on.         |
| 503  | `engine_unavailable`   | The Engine cannot start a Narration right now.     |
| 409  | `narration_not_resumable` | History replay of an active or failed Narration. |
| 409  | `export_in_progress`   | An Export is already running; one at a time.       |
| 422  | `link_refused`         | The link, or a redirect it led to, is not a public https page. |
| 502  | `link_unreachable`     | The linked page could not be fetched.              |
| 502  | `link_too_large`       | The linked page is over 4 MiB.                     |
| 502  | `link_not_a_page`      | The link is not an HTML or plain-text page.        |
| 500  | `internal_error`       | Unexpected failure; details stay in the Engine log.|

Internal details never reach the wire — `message` is a fixed sentence per
code, not an exception string.

## Routes

### `GET /health`

`200` `{"status": "ok"}`. The supervisor's liveness probe; token required
like everything else.

### `GET /v1/catalog`

The picker's data source: enumerate the Catalog. The Manifest is baked
into the release (ADR 0003 §1), so this answers offline with no models
downloaded.

```json
{
  "version": 1,
  "defaultModelId": "kokoro:82m",
  "defaultFastModelId": "supertonic:99m",
  "models": [
    {
      "id": "kokoro:82m",
      "name": "Kokoro",
      "tier": "instant",
      "license": "Apache-2.0",
      "licenseTerms": {
        "id": "Apache-2.0",
        "name": "Apache 2.0",
        "bindsReader": false,
        "credit": null,
        "attribution": null,
        "text": "Apache License\nVersion 2.0, January 2004\n…"
      },
      "referenceLicenses": [],
      "ramClassGb": 0.5,
      "voices": [
        {
          "id": "af_heart",
          "name": "Heart (Female)",
          "language": "en-US",
          "preview": "kokoro/82m/af_heart.m4a"
        }
      ],
      "defaultVoiceId": "af_heart",
      "downloadBytes": 353746785,
      "runsHere": true
    }
  ]
}
```

An entry carries what a user chooses between — Tier, Voices, licence,
memory class, download cost — and never the curation facts behind it: file
hashes and the Backend stay off the wire (CONTEXT.md: Backend is not user
vocabulary).

Each entry also carries `parameters`, the controls a user may change, and
`effectiveValues`, what each Voice currently resolves them to. A declaration
names its `label`, `unit`, `description` and `kind`, and a numeric one adds
`hardRange`, `recommendedRange` (`null` when the entry recommends nothing
narrower) and `nullable`; a range is `{"min", "max", "minExclusive"}` with
`max: null` for an open end. Those wire keys are camelCase while the
Manifest writes the same fields snake_case. `effectiveValues` is keyed by
Voice id and holds one value per declared control. See
[Declared controls](controls.md) for what a control means and
`PATCH /v1/settings/controls` for changing one.

- `defaultModelId` is what this machine narrates with before anyone
  chooses. It is the Manifest's default unless that entry's Backend is not
  here (the Intel build has no MLX), when it is `defaultFastModelId`
  instead; the Manifest's default is the answer only when neither runs.
- `defaultFastModelId` is the Manifest's own pick for the way out of a wait:
  the instant-Tier entry the shell offers while an expressive Narration is
  still being synthesized, when that entry is installed. `null` when the
  Manifest names none; absent from an Engine older than the field.
- `license` is the Manifest's licence id, as v1 has carried it from the
  start. `licenseTerms` is that licence in full, added beside it so the
  launch screen can show the text before the reader accepts it (ADR 0009
  §3-4 and its first-launch amendment).
  `id` repeats `license`; `name` is what the sheet prints. `bindsReader`
  is a fact about the licence, not the entry: when true, the licence binds
  whoever runs the weights. The app asks for every licence at first launch
  either way, so it is informational. `credit` is a line the licence asks the UI to display ("Built
  with Llama"), or `null`. `attribution` is where the weights came from,
  and is an object only for an entry whose licence asks for it — CC-BY
  §3(a)(1) is the only allowlisted one that does (ADR 0009 §2), and it is
  `null` everywhere else. When present it carries what that clause asks
  for: `creator`, `copyrightNotice`, `warrantyNotice`, a `source` page for
  the pinned upstream revision, and whether Readily `modified` the files it
  hands out. The creator, copyright notice, and modification fact are
  explicit attribution data on the entry. The warranty notice comes from
  the licence's obligations.
  The Manifest's curation `provenance` note stays off the wire. Whether
  a licence asks is the Engine's decision, so a client renders the block
  when it is there and never switches on a licence id. `text` is the
  canonical licence text the app bundles, never fetched.

- `referenceLicenses` credits the **Voice Reference** clips a cloning
  entry's Voices are cut from, for the clips Readily did not record
  itself. It is one block per licence, so the text travels once however
  many Voices share it:
  `{id, name, text, warrantyNotice, clips: [{voice, creator, copyrightNotice, source, modified}]}`,
  where `voice` is the Voice id the clip conditions and `source` is the
  corpus page the credit points at. The same allowlist that admits
  weights admits clips (ADR 0009, clip amendment), and only a licence
  that asks for attribution has a block, so a client renders what is
  there and never switches on a licence id. The list is empty for a
  preset model, and for an entry whose clips are all Readily's own.
  Blocks come in the order the Voices do.

- `supportModels` lists each required Support Model as `{name, licenseTerms}`.
  Its licence has the same shape as the Voice Model licence. These are
  dependencies, not Voice choices. Entries with native word times need none.
  Download size and memory class include required support; total installed
  disk usage counts a shared Support Model once.

- `runsHere` is whether this machine can run the entry at all. It is
  `false` for the expressive Tier on a machine without the MLX lane (an
  Intel Mac, Linux), and such an entry cannot be downloaded or narrated
  with: speech, resume, an Export that must synthesize and a re-roll all
  answer `409 model_unsupported`, even when the entry is on disk (the
  Apple-silicon build can fill a data directory the Intel build then
  opens). Deleting it still works. The picker shows it disabled rather
  than hiding it. Which Backend is missing stays the Engine's business.
- `ramClassGb` is a class, not a measurement: roughly what running the
  entry costs in memory, for a client that wants to say so before a
  download. Compared against the machine it warns, it never blocks.
- `preview` names a Voice's bundled **Voice Preview** clip as a path
  relative to the app's own preview root — an `.m4a` the client loads from
  its bundle, never from the Engine and never from the network, which is
  what lets a Voice be auditioned offline before its model exists on disk
  (ADR 0003). It is `null` for a Voice whose clip curation has not yet
  generated, and a client shows that Voice as having no preview rather
  than guessing at a path.

### `GET /v1/models`

The local model store's state: what is downloaded and what it costs on
disk. Model references in these routes follow Catalog resolution: a full
`name:tag` names one entry, a bare `name` resolves to its default tag.

```json
{
  "version": 1,
  "diskBytesTotal": 353746785,
  "models": [
    {
      "id": "kokoro:82m",
      "installed": true,
      "diskBytes": 353746785,
      "downloadBytes": 353746785
    }
  ]
}
```

`installed` means verified and promoted — the store's invariant (ADR 0003
§3) makes those the same thing. `diskBytes` is measured usage (`0` when not
installed); `downloadBytes` is what installing would fetch.

### `POST /v1/models/{model}/download`

Start (or resume) downloading a model. `202`
`{"version": 1, "modelId": "<name:tag>", "status": "accepted"}` — accepted
means *queued*: progress and completion arrive on `/v1/models/events`.
One download runs at a time; the rest wait in the order they were asked
for, listed in the snapshot's `queue`. Idempotent for a model already
downloading or waiting; an already-installed model reports `installed` on
the event stream without refetching. Each waiting download checks free
disk space as its turn comes, after reclaiming a broken copy it would
replace, and one that no longer fits is skipped with
`insufficient_disk_space` in `failures`. An entry whose `runsHere` is
`false` is
`409 model_unsupported`, refused before any byte is fetched. An id outside
the Catalog is
`404 unknown_model` — the Engine downloads only what the baked-in Manifest
names.

### `DELETE /v1/models/{model}`

Delete a model's files from disk. With no download running or waiting, it
happens at once: `200`
`{"version": 1, "modelId": "<name:tag>", "deleted": true|false, "queued": false}`
— `deleted` is `false` when nothing was installed. While a download runs
or waits, the delete waits its turn behind it in the same queue: `202`
with `"deleted": false, "queued": true`, and the model leaves `queue` once
it is gone — or, when its folder cannot be written, with
`store_unwritable` in `failures` and the model still installed. Only
downloads are waited for: a delete asked for while an earlier delete is
still being cleaned up happens at once beside it, the same model's
included — asked for again while its own clean-up runs, it is `deleted`
now if a `store_unwritable` left it installed and the folder is writable
since, or `"deleted": false` once it is already gone. Idempotent.
Stale partial downloads are cleaned up either way. An id outside the
Catalog is `404 unknown_model`.

### `DELETE /v1/models/{model}/queue`

Take a model's waiting download or delete out of the queue before it
starts. `200` `{"version": 1, "modelId": "<name:tag>", "removed": true|false}`
— `removed` is `false` when nothing was waiting, including when the job
has already started; a running job carries on. An id outside the Catalog
is `404 unknown_model`.

### `GET /v1/models/events`

Download progress, as SSE. Same framing as `/v1/events`, with
`event: download`; each event is a complete snapshot of download state:

```json
{
  "version": 1,
  "phase": "downloading",
  "modelId": "kokoro:82m",
  "bytesTotal": 353746785,
  "bytesDownloaded": 120000000,
  "error": null,
  "queue": [{"modelId": "qwen3-tts:0.6b", "action": "download"}],
  "failures": []
}
```

- `phase`: `idle` until the first request, then `downloading` →
  `verifying` → `installed`, with `failed` reachable from both working
  phases. The snapshot describes the most recent download until another
  starts.
- `bytesDownloaded` moves during `downloading` (roughly every 250ms) and
  equals `bytesTotal` once installed.
- `error` is `null` except in `failed`, where it carries the versioned
  `{version, code, message}` error shape: `download_failed` (network —
  retrying resumes where it stopped), `verification_failed` (the bytes
  did not match the Catalog Manifest's pinned hashes; the staged files
  were discarded and a retry starts clean), `store_unwritable` (a broken
  copy sits in a folder the Engine cannot write, so nothing was fetched;
  the message names the folder, and a retry works once it is writable) or
  `insufficient_disk_space` (the download no longer fit on disk when its
  turn came, so nothing was fetched; a retry works once there is room).
- `queue` lists the jobs waiting behind the running one, oldest first;
  `action` is `download` or `delete`. Empty when nothing waits.
- `failures` holds each model's latest failed download or delete as
  `{modelId, error}`, so a failure stays visible after the next job
  starts. A queued delete that could not run fails as `store_unwritable`
  here alone; `phase` and `error` describe downloads. An entry clears
  when its model is asked for again or deleted.

### `POST /v1/sources/fetch`

Fetch the page behind a link the reader asked to read, for the webview to
turn into a Source. Body `{"url": "<https URL>"}`, 1–2048 characters and
nothing else. `200` with the page's bytes exactly as fetched, not JSON and
not versioned: the body is the page. `Content-Type` is `text/html` or
`text/plain`, followed by `; charset=<label>` only when the page's own
header named a well-formed one; otherwise the webview looks in the page.

The Engine fetches only what `download/link.py` allows (threat model,
egress inventory row 5): https with no credentials in the URL, to a host
whose every DNS answer is a public address, following at most five
redirects, each checked the same way; at most 4 MiB, within 30 seconds.

- `422 link_refused`: not https, credentials in the URL, or the host or a
  redirect's host is not on the public internet. The refused host is never
  contacted.
- `502 link_unreachable`: DNS, connection, TLS or timeout failure, an HTTP
  error status, too many redirects, or a compressed body.
- `502 link_too_large`: the body is over 4 MiB.
- `502 link_not_a_page`: the page is not `text/html` or `text/plain`.

### `POST /v1/audio/speech`

Start a Narration. The body is the supported subset of OpenAI's speech
request; unknown fields are rejected (`extra="forbid"`):

```json
{"model": "kokoro:82m", "input": "<text>", "voice": "af_heart"}
```

- `input`: required, non-blank, ≤ 1,000,000 characters.
- `model` / `voice`: optional. `model` follows Catalog resolution like the
  model routes (default: the Catalog's `defaultModelId`); an unresolvable
  reference is `404 unknown_model`. `voice` must be one the chosen entry
  offers (default: that entry's default voice); anything else is
  `422 invalid_request`. Playback speed is not part of the request; the
  Narration plays at the persisted speed (`PATCH /v1/settings/playback`).

`202` `{"version": 1, "narrationId": "<uuid>", "status": "accepted"}`.
Accepted means *queued*: synthesis and playback progress arrive on
`/v1/events`, not in this response — including synthesis failures. Starting
a Narration replaces the active one (ADR 0002 §1 — one active Narration;
the previous one stops). `409 model_unsupported` when the requested
model's `runsHere` is `false`, asked before the disk is, so a client is
never told to download what this machine cannot run.
`409 model_not_installed` when the requested model has not been
downloaded — the Engine loads only store-promoted models (ADR 0003 §3),
so download it first via `/v1/models`. `503 engine_unavailable` when the
Engine has no synthesizer configured.

### `POST /v1/audio/stop`

Stop the active Narration, silencing playback immediately and cutting
short the synthesis or model load preparing it. No body. The Narration is
saved `stopped`, keeping the Segments already made. A new Narration that
never produced audio is deleted instead; a resumed or rerolled one is always
kept. `200` `{"version": 1, "stopped": true}` — `stopped` is `false` when
nothing was preparing, playing, or paused. Idempotent.

### `PATCH /v1/settings/playback`

Set the persisted playback speed and apply it to the active Narration without
restarting synthesis or replacing the playhead:

```json
{"speed": 1.5}
```

`speed` must be a finite number from `0.5` through `4.0`; unknown fields are
rejected. `200` `{"version": 1, "speed": 1.5}`. The new speed reaches audio
after at most the short processed playback queue. Above `3.0`, speech stays
stretched at 3x and only authored pauses from the derived timeline receive
extra compression. The retained pause fraction decreases linearly from 100%
at 3x to 25% at 4x, before the 3x stretch. Silence inside a Segment and failure
gaps receive no extra compression. The resulting listening duration depends
on the Narration's pause content; 4x does not promise a quarter of source time.
Every speed in this range is offered in either mode.
Cached Segments, saved pauses, seek coordinates, and Export remain in source
time.

### `POST /v1/audio/pause`

Freeze the active Narration's playhead without abandoning it. No body.
`200` `{"version": 1, "paused": true}` — `paused` is `false` unless a
Narration was in `playing`. Idempotent in effect: pausing a paused
Narration returns `false` and changes nothing.

### `POST /v1/audio/play`

Release a pause exactly where it froze. No body.
`200` `{"version": 1, "playing": true}` — `playing` is `false` unless a
Narration was in `paused`.

### `POST /v1/audio/seek`

Move the active Narration's playhead. Unknown fields are rejected
(`extra="forbid"`):

```json
{"sourceOffset": 12}
```

- `positionSec` may be sent instead of `sourceOffset` to seek to an exact, finite,
  nonnegative source-time position. The Engine clamps it to the measured audio
  prefix. Sending both fields is rejected.
- `sourceOffset`: nonnegative integer (`422 invalid_request`
  otherwise), measured in Unicode code points in the normalized Source.
  A word seek starts before its onset: 50 ms for `spoken`, 150 ms for
  `matched`, and 250 ms for `estimated`, clamped to the Block start, with a
  15 ms fade-in. Missing timings are interpolated inside the
  Block. Whitespace between Blocks targets the following Block.

`200` `{"version": 1, "seeked": true}` — `seeked` is `false` unless a
Narration is preparing, playing, or paused. For a Source seek, the offset must be inside its Source,
and the lengths before the target are known. An uncached target is
synthesized first, then its word position is resolved. Unknown or retired
predecessors must be regenerated before seeking past them. The first Block
can always be restarted. Cached Blocks are seekable before
playback reaches them, without re-synthesis. A seek re-enters `preparing`,
and a paused Narration starts playing at the target. The Engine derives
`positionSec` from trimmed lengths, crossfades and the Narration's versioned
pause policy; the shell never converts Source coordinates to seconds.
While an unmeasured target is preparing, the position remains at the known
end of its prefix, then moves to the resolved target before playback.

### `GET /v1/history`

Durable Narration History, newest first (`created_at` descending, then
`id` descending). Summaries carry a Source preview (the first 160
characters of the stored normalized Source), never a content hash or a
filesystem path.

```json
{
  "version": 1,
  "history": [
    {
      "id": "n-1",
      "sourcePreview": "Read locally.",
      "modelId": "kokoro:82m",
      "voiceId": "af_heart",
      "speed": 1.0,
      "status": "interrupted",
      "createdAt": "2026-08-26T00:00:00+00:00",
      "updatedAt": "2026-08-26T00:00:00+00:00",
      "lastPlayedAt": "2026-08-26T00:00:00+00:00",
      "playheadSec": 0.5,
      "totalDurationSec": 1.0,
      "audioPresent": true,
      "hasGaps": false
    }
  ]
}
```

- `status`: `preparing`, `playing`, `interrupted`, `stopped`, `finished`,
  or `failed`. A restarted Engine rewrites leftover `preparing`/`playing`
  rows to `interrupted` and stays silent until the client resumes.
- `playheadSec` is the last checkpointed position; `totalDurationSec` is
  `null` until the Narration finishes.
- `audioPresent` is true only when every manifested Segment still has a
  file; `hasGaps` is true when any Block recorded a durable failure that
  no later run of *this* Narration has made good. A gap is a claim about
  audio this Narration never produced, so a Block that succeeds on a retry
  or a Resume clears its own gap. Segments are shared by content, so a
  different Narration of the same text storing that audio can make
  `audioPresent` true here while the gap still stands: it records what
  happened on this Narration's run, and only replaying it retracts that.

### `GET /v1/settings/retention`

The current Segment retention policy and the two categories of disk usage
shown by Settings:

```json
{
  "version": 1,
  "segmentBudgetBytes": 5368709120,
  "keepAudioDays": null,
  "diskUsage": {"modelsBytes": 353746785, "audioBytes": 1048576}
}
```

`segmentBudgetBytes` applies only to synthesized Segment audio, not downloaded
models. `keepAudioDays: null` means Never. Retention uses each Narration's
`lastPlayedAt`; replaying a Narration protects its shared Segments as well.

`audioBytes` is what the Segment store occupies right now, which may exceed
`segmentBudgetBytes`: the budget is applied when the policy changes and on the
Engine's retention schedule, not after every Segment a Narration writes.

`503 engine_unavailable` when storage is not up.

### `PATCH /v1/settings/retention`

Replace the complete retention policy. Both fields are required and unknown
fields are rejected (`422 invalid_request`):

```json
{"segmentBudgetBytes": 5368709120, "keepAudioDays": 30}
```

`segmentBudgetBytes` must be a whole number from `1` to `9223372036854775807`
— though a JavaScript client cannot represent anything above `2^53 - 1`
exactly, so treat that as the practical ceiling.
`keepAudioDays` is either `null` or a whole number from `0` to `36500` (100
years — past that, `null` is the setting you want). Anything outside those
ranges is `422 invalid_request` and leaves the stored policy untouched.

`keepAudioDays: 0` is legal and means *keep nothing*: every Segment is evicted
on the next sweep, so replay always re-synthesizes. `503 engine_unavailable`
when storage is not up.

The response has the same shape as `GET`, after the new policy has been
applied synchronously, plus one field `GET` does not carry:

```json
{"evictedBytes": 104857600}
```

`evictedBytes` is what this application of the policy removed, counted by the
sweep as it deleted. It is not `audioBytes` before minus `audioBytes` after: a
client cannot compute it that way, because the Engine's own retention schedule
sweeps between any two readings and a Narration being synthesized is adding
audio the whole time. `0` when the new policy took nothing.

### `GET /v1/settings/voice`

The Voice the picker shows as current — the user's stored choice, resolved
against the baked Catalog:

```json
{"version": 1, "modelId": "kokoro:82m", "voiceId": "af_heart"}
```

Resolved, not echoed. A client always gets a Voice it can render: before
anything has been chosen the answer is the Catalog's `defaultModelId` and
that entry's default voice, and a stored id a later release retired falls
back the same way (a stored voice its model no longer offers falls back to
that model's default, keeping the Voice Model the user chose). A stored
model whose `runsHere` is `false` falls back the same way a retired one
does: the Apple-silicon build can leave such a choice in a data directory
the Intel build then opens, and `defaultModelId` is already the fast model
there.

Being *installed* is not part of this answer: choosing a Voice is not
downloading its model. `GET /v1/models` says what is on disk, and narrating
with a model that is not answers `409 model_not_installed`.

`503 engine_unavailable` when storage is not up.

### `PATCH /v1/settings/voice`

Store a Voice choice made without narrating — which is the only way one
survives a quit, since the Engine otherwise records the choice as a side
effect of starting a Narration. Both fields are required and unknown fields
are rejected (`422 invalid_request`):

```json
{"modelId": "kokoro:82m", "voiceId": "af_heart"}
```

`modelId` follows Catalog resolution like every other model route (a bare
`name` resolves to its default tag) and is stored resolved; an unresolvable
reference is `404 unknown_model`. `voiceId` must be one the resolved entry
offers, or `422 invalid_request`. The response has the same shape as `GET`.

Speed is not part of this route: it is a separate knob, and choosing a Voice
leaves the stored one untouched.

### `PATCH /v1/settings/controls`

`GET /v1/settings/controls?modelId=…&voiceId=…` returns the same response
shape: `version`, resolved `modelId`, `voiceId`, sparse `overrides` and
`effectiveValues`. Clients editing controls read the sparse overrides before
replacing them, preserving unrelated settings.

Replace one Voice's control overrides. All three fields are required and
unknown fields are rejected (`422 invalid_request`):

```json
{
  "modelId": "supertonic:66m",
  "voiceId": "M1",
  "overrides": {"steps": 12, "seed": 42}
}
```

`overrides` is the whole record, not a patch of it: send `{}` to go back to
the entry's defaults. That record includes `prepare_first`, so a caller that
replaces it without carrying `prepare_first` over resets that Control too.
Every key must be a control the entry declares and every value must satisfy
that declaration, or `422 invalid_request`; an unresolvable `modelId` is
`404 unknown_model` and a `voiceId` the entry does not offer is
`422 invalid_request`. The response echoes `modelId`,
`voiceId` and the stored `overrides`, and adds `effectiveValues`, the same
map `GET /v1/catalog` projects for that Voice.

An override outlives the Manifest that accepted it. When a later Catalog
drops a control or narrows its range, the stored value is ignored on read
rather than refusing the Narration, so the Voice quietly returns to the new
default.

`503 engine_unavailable` when storage is not up.

### `GET /v1/history/{id}`

The same identity and settings as the summary, plus the full normalized
Source, ordered Segment ranges, and gap records. Segment `sourceStart` /
`sourceEnd` index that Source. An unknown id is `404 not_found`.

`?afterOrdinal=N` reads the Narration past a cursor: `segments` holds only
the Segments with `ordinal > N`, and `source` is omitted. `gaps` and the
summary fields are always whole. `N` is an integer of at least zero, or
the request is `422 invalid_request`; a cursor past the last Segment
returns an empty `segments` list. The client keeps the Source and the
Segments up to the cursor from an earlier full read, so a Narration of
thousands of Blocks is not resent whole each time one more is assembled.

The cursor is a promise the Engine checks: every Segment at or before `N`
has a non-null `durationSec` in the client's hands. A measured Segment
keeps its `timelineStartSec` and `timings` until a take swap, which the
client follows with a full read, or until a re-synthesis of its evicted
audio unmeasures it under the client. So when any Segment at or before
`N` is unmeasured, the Engine ignores the cursor and answers with every
Segment (`source` still omitted), and the client takes that list whole.
A gap is unmeasured, so a client's cursor stops at the Segment before it.

```json
{
  "version": 1,
  "id": "n-1",
  "sourcePreview": "Read locally.",
  "source": "Read locally.",
  "modelId": "kokoro:82m",
  "voiceId": "af_heart",
  "speed": 1.0,
  "status": "interrupted",
  "createdAt": "2026-08-26T00:00:00+00:00",
  "updatedAt": "2026-08-26T00:00:00+00:00",
  "lastPlayedAt": "2026-08-26T00:00:00+00:00",
  "playheadSec": 0.5,
  "totalDurationSec": 1.0,
  "audioPresent": true,
  "hasGaps": false,
  "segments": [
    {
      "ordinal": 0,
      "sourceStart": 0,
      "sourceEnd": 13,
      "boundary": "paragraph",
      "durationSec": 1.0,
      "audioPresent": true,
      "timelineStartSec": 0.0,
      "timings": []
    }
  ],
  "gaps": []
}
```

`boundary` is `sentence`, `paragraph`, or `mid-sentence`. History
responses never include Segment hashes, database keys, or filesystem
paths.

The same Narration read with `?afterOrdinal=0`:

```json
{
  "version": 1,
  "id": "n-1",
  "sourcePreview": "Read locally.",
  "modelId": "kokoro:82m",
  "voiceId": "af_heart",
  "speed": 1.0,
  "status": "interrupted",
  "createdAt": "2026-08-26T00:00:00+00:00",
  "updatedAt": "2026-08-26T00:00:00+00:00",
  "lastPlayedAt": "2026-08-26T00:00:00+00:00",
  "playheadSec": 0.5,
  "totalDurationSec": 1.0,
  "audioPresent": true,
  "hasGaps": false,
  "segments": [],
  "gaps": []
}
```

`timelineStartSec` is where a Block starts on the same timeline
`positionSec` and `totalSec` are measured in. It is derived from the Block
plan, stored trimmed frame counts, and the Narration's versioned pause
policy, using the same crossfade and pause decisions as playback and Export.
It is available for cached Blocks before they play, and `null` when an
unknown earlier length prevents deriving it. `durationSec` is the trimmed
Block's duration, or `null` until the Block has been measured; pauses and
crossfades also contribute to the timeline.

`timings` contains every word's interval once its Block and preceding
lengths are measured. Each entry has `sourceStart` and `sourceEnd` in Unicode
code points, `startSec` and `endSec` on this Narration's timeline, and
`provenance`: `spoken` for model durations, `matched` for accepted alignment,
or `estimated` for interpolation. There is no confidence float on the wire.
Any timing source owns its own acceptance threshold.

Seek and read-along use the same intervals. Missing words divide the time
between their nearest accepted anchors in proportion to their character
counts. The seek path has no G2P or loaded Voice Model requirement. Block
edges anchor the first and last missing runs. Conflicting or out-of-range
measurements are replaced by estimates. A first word before the saved trim
start clamps to the Block start and remains in the response. The UI labels
estimated positions as approximate. It also offers word controls before a
Block is synthesized, once its preceding lengths are known.

On disk, timings remain relative to the Segment's synthesis text and raw
waveform. Their metadata includes the accepted FLAC's SHA-256, so a
replacement waveform cannot inherit stale timings. The Engine applies each
Block's Source range, leading whitespace, saved trim and timeline start
when producing this response. Legacy sidecars without timings load without
migration and receive estimates. Playback Speed changes neither these
source-time seconds nor the stored Segment.

Kokoro English and Kitten report `spoken` words from their own durations.
Qwen, Chatterbox and Supertonic report `matched` words when the
Voice's `word_timing` Control selects the wav2vec2 aligner, which is
off by default. Every other word, including a whole Block whose lane
reports nothing, is `estimated`.

### `DELETE /v1/history/{id}`

Delete one Narration and garbage-collect only Segment audio no remaining
Narration references:

```json
{
  "version": 1,
  "narrationId": "n-1",
  "deleted": true,
  "audioBytesFreed": 1048576
}
```

Deleting an active Narration cancels its generation and playback before its
manifest is removed. An unknown id is `404 not_found`; storage that is not up
is `503 engine_unavailable`, never `404` — a client must not read "storage is
down" as "already deleted". Shared Segment audio is kept until its final
Narration reference is deleted.

### `POST /v1/history/{id}/resume`

Resume an `interrupted` or `stopped` Narration at its stored playhead, or
replay a `finished` Narration from the beginning. No body. Explicit: a
restarted Engine never auto-resumes. `?paused=true` makes it the active
Narration with the playhead frozen at the resume point — it reaches
`paused` instead of `playing` once audio is ready, and `POST /v1/audio/play`
releases it — so opening a row is silent until the reader presses play.
`202` uses the same accepted shape as speech:

```json
{"version": 1, "narrationId": "n-1", "status": "accepted"}
```

Accepted means queued: playback progress arrives on `/v1/events`. Resume
replaces any currently active Narration. Cached Segments are not
synthesized again; missing audio is regenerated through the same
store-first path as a new Narration — but only from the playhead onward.
Blocks that end before the stored playhead are skipped outright, so a
Narration whose audio retention has since swept away still resumes in
seconds rather than re-synthesizing the part the listener already heard.
A Narration with unknown earlier Block lengths resumes by walking from
the first unknown Block instead.

- `404 not_found` when the id is unknown.
- `409 narration_not_resumable` when the Narration is active or failed.
- `409 model_unsupported` when the stored model's `runsHere` is `false`,
  whether or not it is on disk.
- `409 model_not_installed` when the stored model is not downloaded.
- `503 engine_unavailable` when the Engine cannot start a Narration right now.

### `POST /v1/history/{id}/export`

Write one Narration to a single audio file in Readily's audio folder.
The one way Narration audio leaves Readily's own storage.

```json
{"format": "m4a"}
```

- `format` is `m4a` (AAC) or `wav` (lossless), and optional. There is no
  MP3. Left out, the Engine writes its default: `m4a` on macOS, `wav`
  where it cannot write M4A (ADR 0015). A format this Engine cannot
  write, or any other value, is `422 invalid_request`.
- The body names nothing else, and any other field is `422
  invalid_request`: the Engine chooses both the folder and the file name,
  so no client can steer where it writes. The folder is
  `Readily` in the reader's Documents, which the shell names in
  `READILY_AUDIO_DIR` when it spawns the Engine and shows through its
  `open_audio_folder` command. The file is named from the Source's first
  words, and never replaces a file already there: a second Export of the
  same Source becomes `… 2.m4a`, then `… 3.m4a`.

`202` answers immediately with the accepted shape plus the format:

```json
{"version": 1, "narrationId": "n-1", "format": "m4a", "status": "accepted"}
```

Accepted means *queued*: progress arrives on `/v1/export/events`. Pressing
Export behaves the same way whatever state the Narration's audio is in —
the request never blocks on the work.

Segment audio is stored losslessly, so an Export of audio that is still
cached costs no synthesis and no second lossy pass. A Narration whose audio
retention has since swept away is re-synthesized first, Block by Block, and
**that work queues behind whatever is currently playing**: there is exactly
one synthesizer on one thread (ADR 0002 §3), so an Export can sit in
`preparing` for as long as the active Narration needs. It never interrupts
what the listener is hearing.

- `404 not_found` when the id is unknown.
- `409 export_in_progress` when an Export is already running.
- `409 model_unsupported` when audio is missing *and* the stored model's
  `runsHere` is `false`, whether or not it is on disk.
- `409 model_not_installed` when audio is missing *and* the stored model is
  not downloaded. A fully cached Export needs no model and is refused for
  neither reason.
- `503 engine_unavailable` when storage is not up, or the Engine is
  shutting down and will not take new work.

### `GET /v1/export/events`

Export progress, as SSE. Same framing as `/v1/events`, with `event: export`;
each event is a complete snapshot of Export state:

```json
{
  "version": 1,
  "phase": "preparing",
  "narrationId": "n-1",
  "format": "m4a",
  "completedBlocks": 4,
  "totalBlocks": 27,
  "error": null
}
```

- `phase`: `idle` until the first Export, then `preparing` (decoding cached
  Blocks and re-synthesizing evicted ones) → `encoding` (one pass through
  the encoder) → `finished`, with `failed` reachable from both working
  phases. The snapshot describes the most recent Export until another
  starts.
- `completedBlocks` moves once per Block during `preparing` and equals
  `totalBlocks` by `encoding`.
- The file's name is never sent: it is made from the Source, and the
  Engine keeps filesystem paths off the wire.
- `error` is `null` except in `failed`, where it carries `export_failed`.
  Nothing is left in the audio folder: the file is encoded inside a
  staging folder in it, named `.readily-export-*.nosync` so a Documents
  folder synced to iCloud uploads neither a half-written file nor the
  encoder's intermediate, and linked into place only once it is complete. A failed Export removes the
  staging folder; one killed mid-write leaves a dot-folder to delete,
  never a truncated recording.

### `GET /v1/events`

The Narration state stream, as SSE.

## SSE framing

Read `/v1/events` with a **streamed `fetch`**, not `EventSource` —
`EventSource` cannot send the `Authorization` header. Parse
`text/event-stream` framing from the response body: events are separated by
a blank line; a line's field is everything before the first `:` and its
value the rest, with one optional leading space stripped.

Every SSE stream opens with `retry: 1000` (a reconnect hint) and carries
one event type; `/v1/events` carries only `narration` events:

```
event: narration
id: 1
data: {"version":1,"phase":"playing", …}
```

- `id` increments from 1 per connection. `data` is one line of JSON.
- Each event is a **complete snapshot** of Narration state, not a delta.
  Missing events is harmless; the next snapshot is the whole truth. A
  snapshot is sent on every state change, and at least every 15 seconds as
  a heartbeat (also how a client distinguishes a quiet Engine from a dead
  connection).

The v1 snapshot:

```json
{
  "version": 1,
  "phase": "idle",
  "narrationId": null,
  "modelId": "kokoro:82m",
  "voiceId": "af_heart",
  "positionSec": 0.0,
  "level": 0.0,
  "totalSec": 0.0,
  "speed": 1.0,
  "generationBehind": false,
  "lateCallbacks": 0,
  "error": null
}
```

- `phase`: `idle` → `preparing` (synthesis) → `playing` → `finished`, with
  `failed` reachable from `preparing`/`playing` and `paused` reachable
  from `playing` (`POST /v1/audio/pause` ↔ `POST /v1/audio/play`) or
  directly from `preparing` for a `?paused=true` resume. A new
  `POST /v1/audio/speech` moves any phase back to `preparing`, as does
  `POST /v1/audio/seek` for the active Narration;
  `POST /v1/audio/stop` moves an active phase to `idle`.
- `narrationId` matches the accepted response's id while a Narration is
  active or just finished/failed; `null` when idle.
- `modelId` / `voiceId` name what the current Narration resolved to. Before
  the first Narration they name the Catalog's `defaultModelId` and its
  default voice, whatever those are on this machine — read them, do not
  assume the values in the example above.
- `speed` is the persisted playback speed currently applied by the Engine.
  It can change during any Narration phase without changing `narrationId`.
- `generationBehind` is true while playing if completed generation is slower
  than consumption at the chosen speed and fewer than 60 listening seconds
  are ready, or in-progress generation has exhausted the ready audio. The
  estimate accounts for authored pause compression. It is false once preparation completes
  or playback is paused/stopped. Cached audio does not count as new generation
  cost. This is a warning only: the Engine never lowers speed automatically.
- `totalSec` grows as contiguous Blocks become ready, including while
  `preparing` or `paused`, and never shrinks within one admission: a seek
  through assembled audio keeps the length already reported. With
  prepare-first (the default) it is the Narration's full duration before
  playback starts; streaming, before playback finishes. Clients can use
  changes to refresh cached Block details;
  `positionSec` is the Engine's source-time playhead on that same timeline — live in
  every `playing`/`paused` snapshot (which is what makes the stream a
  position readout), and equal to `totalSec` at `finished`. While
  `preparing` it is where playback is about to resume — `0` for a new
  Narration, the seek target for a seek, the stored playhead for a
  Resume — so the readout never falls back to the start of the read while
  the Engine locates the Block. The stretcher reports the source represented
  by each emitted PCM chunk, so its internal latency never moves the read-along
  ahead. `finished` fires when the Engine's buffer drains, which
  can lead the last audible sample by up to the output latency (~0.4s).
- `level` is how loud the audio the device is playing right now is, 0–1,
  patched live beside `positionSec`: the RMS of the last PCM chunk handed to
  the output device on a decibel scale, with -50 dBFS and below reading `0` and speech at a normal
  mastering level around `0.6`. It is `0` whenever nothing is being heard —
  idle, preparing, paused, finished, or a silent stretch of a Narration. It
  is measured on the stretched audio, so it follows the playback speed a
  reader hears. It exists for the shell's voice orb to move with the voice;
  it is not a meter, and it leads the speaker by the output latency.
- `lateCallbacks` counts audio callbacks the Engine served late over this
  process's lifetime — the audio-health gauge; it staying
  `0` is the "clean audio" check.
- `error` is `null` unless it carries the same versioned
  `{version, code, message}` shape as HTTP errors. In `failed` the codes are
  `generation_failed` (synthesis could not produce this Narration) and
  `audio_output_unavailable` (the output device went away mid-playback; the
  Narration is left `interrupted` and can be resumed from `positionSec`).
  `generation_gap` rides `playing` and `finished` snapshots once a Block's
  retry has failed, while the rest of the Narration goes on.


## Simple mode

Each Catalog Voice includes `simple`, true only while its committed qualification
matches the current recipe. Qualification does not depend on word timings, and
it gates nothing: both modes accept every Voice.

`POST /v1/audio/speech` accepts `mode: "simple" | "advanced"` in the body,
defaulting to `advanced` for existing clients. The app starts in Simple.
Simple ignores saved Advanced control overrides and resolves the Catalog
defaults. `POST /v1/history/{id}/resume` takes no mode: History replays with
the settings it was made under.

Modes share the Segment cache. Mode is absent from the Generation Record and
its key, and so is Playback Speed. A Block's single retry redraws the seed
and, when it succeeds, the Segment is stored under that redraw. A later
Narration of the same text finds the rescued audio under the redraw. After
the retry fails, the gap is recorded in History and `/v1/events` carries
`error.code: "generation_gap"`, including when the remaining Blocks finish
playing.

### Advanced diagnostics and Block takes

Catalog entries add `wordTimingModels`, an array of `{id, name}` Support
Models for the declared word-timing choices. Entries with native timing do
not declare a word-timing control.

Narration snapshots add `diagnostics`: `audioSecondsPerSecond` (null before
measurement), `readySecondsAhead` at the current speed, zero-based
`preparingBlock` (null when idle), `generationComplete`, `retries`,
`cutoffs`, `ringStarvations` and `deviceUnderflows`. `preparingBlock` is
also null between Blocks while a streaming Narration waits on its
lookahead; `generationComplete` is true only once the active Narration's
last Block has been made, so it says whether a stop would cut generation
short rather than playback alone. Generation measurements cover the
playback run; the two audio-device counters cover the Engine session.
`playingBlock` is null outside an audible Block, otherwise it carries
`ordinal`, `recordHash`, `seed`, `cacheHit`, `wordTiming`
(spoken/matched/estimated), nullable `supportModel`, `take` (A/B) and
`hasComparison`. `wordTiming` is the
provenance the Segment's stored word times carry, `estimated` when it has
none; it says which Support Model ran, not whether every word was placed.

`POST /v1/history/{id}/take` accepts only `{"ordinal": 0, "action": "reroll"}`,
with action also accepting `A` or `B`. It returns 202 after admitting the
playback job; snapshots report preparation and playback. Invalid ordinals,
missing takes and unsupported actions return 422. The route requires the
same authentication and runnable, installed models as replay, refusing
with `409 model_unsupported` or `409 model_not_installed` as resume does.
Re-roll preserves A,
stores a new seed as B and plays from that Block. Selecting A or B updates
History's selected Generation Record; both takes remain referenced for
retention. Exports already holding a plan keep that plan's Segment identities.

This Advanced surface touches B3 (authenticated local requests), and cutoff
measurement touches B2 (backend-generated audio). It adds no network egress.
