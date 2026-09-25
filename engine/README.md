# Readily Engine

The local service that downloads, verifies, and runs Voice Models to produce
Narrations ([ADR 0001](../docs/adr/0001-tauri-shell-python-engine.md)). The
`.app` ships no Python: a bundled `uv` provisions a pinned interpreter and
these wheels on first run, resolved by the committed `uv.lock`.

## Run it

```sh
uv run readily-engine
```

The Engine binds `127.0.0.1` on `READILY_ENGINE_PORT` (default `0`: the
OS assigns a free port) and announces the bound port on stdout as
`READILY_ENGINE_PORT=<port>` — only after the socket is held, so the
announced port is never up for grabs. The supervisor never sets the
variable; it learns the port from that announcement. Every route requires
the per-launch bearer token. In the app the Rust supervisor generates it
and hands it over via the `READILY_ENGINE_TOKEN` environment variable —
never argv. Standalone, the Engine generates one and prints it to stderr:

```sh
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:$PORT/health
```

## Application data

Production data lives under `~/Library/Application Support/Readily/`
(overridable with `READILY_DATA_DIR`):

```text
~/Library/Application Support/Readily/
├── readily.db
├── readily.db-shm
├── readily.db-wal
├── models/
├── segments/
│   └── <first-two-hash-chars>/<full-hash>.flac
├── staging/
├── engine/
├── logs/
└── hf-home/
```

`readily.db` is the durable Narration History and stays in Time Machine.
`models/`, `segments/`, and `engine/` are created with
`tmutil addexclusion` — they are reproducible downloads and cached speech,
not irreplaceable state. A refused exclusion is logged, but does not prevent
the Engine from starting.

Segment audio is FLAC. Encoding uses macOS `/usr/bin/afconvert` from a
24-bit WAV staging file; decoding uses the MIT `miniaudio` package. Short
packets are padded for Apple's encoder; a sibling `<hash>.frames` file
records the original sample count so every decode trims back to exact PCM.
The Engine never creates a Narration-level audio file: playback consumes
individual stored Segments. History HTTP responses never expose hashes or
paths.

Browser origins are the webview's alone. A debug build of the supervisor
sets `READILY_ENGINE_ALLOW_DEV_ORIGIN=1` to additionally admit Vite's
`http://127.0.0.1:1420`; a shipped app never does. Running the Engine by
hand behind `bun run dev` needs that variable set too.

Under the supervisor (`READILY_ENGINE_SUPERVISED=1`) the Engine also
watches its stdin: the supervisor holds the other end of that pipe for as
long as it lives, so however the app dies — quit, crash, force-quit — the
Engine sees EOF and exits instead of leaving an orphan listening on
loopback. Standalone runs skip the watchdog; their stdin is a terminal.

## Qualify audio

The ear-calibrated qualification harness runs checks A-F over a captured run
or corpus: synthesis noise, trim edges, feed integrity, discontinuities,
callback timing, and in-pause crackle. It prints a numeric verdict and exits
nonzero when any check fails:

```sh
uv run readily-qualify tests/fixtures/audio/qualification
```

The committed corpus retains raw/processed Segment pairs, the callback tap,
and timing manifests from labeled listening runs. New Catalog Voice Models
must pass the harness before the human listening session.

## Curate a Voice Model

Adding a model to the Catalog, or bumping its weights, is one command plus a
reviewed manifest PR — never a hand-typed hash:

```sh
uv run readily-curate kokoro:82m --write     # re-pin a committed entry
uv run readily-curate ~/new-model.json       # add one, printing the entry
```

It downloads the draft's named files, computes their SHA-256 from the bytes
that actually arrived, installs them through the real store, narrates one
standard passage in **every** Voice through the real Backend, and runs the
qualification harness (checks A, B, D, F — the synthesis-only ones; a capture
feeds no audio device, so C and E have nothing to measure) over the result,
plus a floor on how much of it is voiced speech, which the artifact checks
cannot see. A model that does not qualify is refused, and no entry or clip is
written. With `--write`, a passing model's Voice Preview clips land in
`public/previews/` and its entry is merged into `catalog/manifest.json` in
one publish step, ordered so an interruption never leaves the manifest
naming a clip that is not there. Publishing also sweeps that entry's own
`public/previews/<name>/<tag>/` of clips no Voice in it names any more, so
dropping or renaming a Voice cannot leave one shipping in the bundle.
Without `--write` nothing in the repository is touched: the entry is printed
for inspection and the clips stay staged beside the capture, ready to
audition.

Curation qualifies the *model*: artifact checks over every Voice, one Voice
Preview each. It does not qualify a Voice for **Simple** mode. A Voice ships
with `qualification: null` and is offered in Advanced only until a curator
runs `--check-simple --voice <id>` over the standard passage, listens to the
joined capture, and copies the reported recipe digest into that Voice's
`qualification` (see "Qualifying a Voice for Simple" below). A Voice Model therefore
offers every Voice that clears curation, while Simple keeps only the Voices
the author and the curator both rate highly. Kokoro answers to one more gate.
It offers the English presets that clear the artifact checks on the standard
passage, and refuses `am_adam`, which clears them but carries an F+ in the
model's own `VOICES.md`. A grade that low reports something the artifact
checks cannot hear. A Simple pin needs more still. The author grades the
preset B- or better, and a curator listens to the recipe the pin names.
Re-curation carries a pin forward while its digest still matches the recipe,
and drops it otherwise.

### Qualifying a Voice for Simple

Simple pins each Voice's Generation Record defaults, chunk budget and Pause
Policy with a recipe digest in the Catalog. Changing any of those values
invalidates the qualification; re-curating identical bytes preserves the pin.
Word timings are not part of admission. Advanced keeps every entry and any
saved control overrides; Simple ignores those overrides without deleting them.

Run the check against an already installed, pinned model store. It
synthesizes locally and downloads nothing:

```sh
readily-curate qwen3-tts:0.6b --check-simple --voice Chelsie \
  --chunk-budget 225 --data-dir /path/to/model-store \
  --capture-dir /path/to/fresh-capture
```

The command runs production chunking, synthesis, trimming and assembly over
the standard 20-paragraph passage; smaller budgets can yield more than 20
Blocks. It keeps every raw Block, the joined passage and `qualification.json`
with the Generation Records, recipe digest and measurements. It fails on
missing voiced speech, pitch departing more than 15 Hz from the first Block,
saturation, or a sentence or paragraph ending above −36 dBFS after zero-margin
trim, since silent padding cannot hide a cut word. Mid-sentence cuts are
measured but need listening to tell expected continuation from a damaged word.

Listen to the whole joined passage, including mid-sentence seams, before
copying the reported digest into the Voice's `qualification` and committing
the matching recipe defaults. Passing the acoustic checks alone does not
qualify a recipe, and the suite's synthetic walking and clipped signals prove
refusal, not listening evidence for any model. A Catalog change like this
crosses the Catalog trust boundary and needs a human line-read before merge.

A **draft** is a Catalog entry with the two things curation produces left
out: each file is a bare path string rather than a `{path, sha256,
size_bytes}` object, and no Voice carries a `preview`. Everything else — the
`source` repo and revision commit, the Tier, the license, the RAM class, the
provenance note, the Voices, the tunables — is what the curation PR is
reviewed on, so it is written by hand. Copy a committed entry out of
`catalog/manifest.json` and strip those two things; without `--write` the
script prints the finished entry for inspection instead of merging it.

Read the licence off the *weights*, not off a repo tag: it is checked against
the Catalog's licence allowlist when the draft parses, before a byte downloads,
and a model whose sample code is MIT while its weights are CC-BY-NC is refused
there ([ADR 0009](../docs/adr/0009-model-weights-redistributable-licences.md)).
A repo tag has been wrong twice (Supertonic, Pocket TTS): the tag describes the
code, and the weights carry their own file.

A Backend that samples can be refused on one draw and pass on the next —
Qwen3-TTS has no seed, and its check-A hf8k score on a later Segment is
logged rather than gated by its own accept gate, so it lands either side of
the threshold across runs. The refusal keeps the capture and prints its path:
listen to the Segment it names before re-running, because a second run that
passes is a second draw, not a fix.

Check B is deterministic. `trim_silence` cuts on 10ms frame boundaries, so
whichever sample the waveform happens to be at becomes the Segment's edge,
real enough to click at playback start. The `Assembler` ramps the edges it
exposes (`EDGE_FADE_SECONDS`), skipping the openings a crossfade or a
butt-join already carries, so a hard edge reaching check B is a genuine
assembly bug rather than a draw to re-roll.

Curating always re-promotes: the previously installed model is retired
before the freshly pinned bytes are verified into its place, so the capture
that qualifies an entry can only ever come from the bytes that entry pins.
That is what makes a weights bump — the case this script exists for — safe
to run over a model already sitting in the store.

It re-promotes into a store of its own, `Readily/curation/store`, and not the
app's. It has to install before it can listen, so a refused model is left
promoted; in the app's store that would be unqualified weights sitting under
the entry's identity, with the qualified ones already retired and swept.
Nothing re-checks a hash at load time and the Segment cache is keyed on
`version`, so the app would narrate with weights its manifest never pinned
and mix them with cached audio from the old ones. A separate root costs one
re-download and cannot. Point `--data-dir` at the app's tree only if you want
exactly that.

Two ways a healthy model is refused anyway. A draft whose `chunk_budget_chars`
falls below the capture passage's longest sentence (about 90 characters) puts
a crossfade seam mid-sentence, and check B reads the blended edge as a step
discontinuity on every Segment; the committed entries budget 450 and 300. And
the voiced-speech floor measures periodicity below 1.5kHz, so a whispered or
breathy Voice preset can score under it while sounding fine. Neither is a
quiet wrong pass — both refuse loudly, and both are the analyzer's to fix
rather than this script's.

Re-curating an unchanged entry rewrites the same bytes, so

```sh
uv run readily-curate kokoro:82m --write && git diff --exit-code ../catalog/manifest.json
```

is the regeneration proof: a pin that no longer matches shows up as a diff.
This is a developer command — it downloads models and runs inference, which
is why it never runs in CI.

## One registry, five Architectures

A Catalog entry's `architecture` names the code that runs it — one package
under `loading/`, or one spec file in a family package, registered in
`loading/registry.py` under that id — and
that is the only thing `synthesizer_for` reads to choose a loader (ADR
0014). Two entries can share a runtime without sharing an Architecture:
`kokoro:82m` and `kitten-tts:15m` are both StyleTTS2 descendants on
onnxruntime, so `loading/styletts2/` runs both with one synthesizer and
registers each as its own Architecture off a `Spec` — the graph's
input name, the symbol table, the style archive's shape, the dialect rule,
the respelling, the repair chain — in `styletts2/kokoro.py` and
`styletts2/kitten.py`. A third StyleTTS2 export is a third spec file, not
a module; a `Spec` names data and one-argument functions, never a
method. `supertonic:66m` shares only the runtime, running four graphs with
no G2P at all. An id the registry
does not know is refused at `readily-curate --write` and at Engine boot
rather than defaulted, because handing a new entry the wrong Architecture's inputs
fails deep inside the runtime instead of at the seam that chose wrong. The
registry is a literal table — one line per model, never populated by
`importlib` — so the licence gate sees every shipped Architecture in-tree.

Each Architecture declares what its consumers need: the files it opens
(`expected_files`, checked against every committed entry by
`tests/architectures/test_registry.py`), how a Voice conditions it (`preset` or
`reference`), its warm-up text, its parameter schema, and the Block budgets
`readily-curate --chunk-budget` may sweep. What Architectures genuinely share
stays beside them: the ONNX session options in `loading/onnx.py`, the
Misaki G2P that keeps espeak out of the tree in `loading/styletts2/`, and
in `loading/mlx_lane.py` the streaming, accept gate, crackle scrubber and
runaway cutoff over one small `generate` per MLX Architecture. Qwen3-TTS
(`loading/qwen3/`) conditions on a Voice Reference decoded by
`loading/references.py`; Chatterbox Turbo (`loading/chatterbox_turbo/`)
uses the voice baked into its weights.

Each Architecture owns whatever its own weights need afterwards. Kokoro comes off
its graph needing only a declick; Kitten rides a drifting DC bias, overshoots
full scale, and crackles in its own pauses, and its `Spec`'s repair in
`loading/styletts2/kitten.py` fixes all three in an order the file documents.

What every Architecture owes a Narration is enforced once, not per
Architecture: `LazySynthesizer` refuses a Voice the entry does not offer
before anything loads, and turns a silent draw into a `DegenerateDraw` the
worker redraws. "Supported" means the conformance suite passes
(`tests/architectures/conformance.py`), and a model ships when that suite
and `readily-curate` qualification both pass — the steps are in
[Adding a Voice Model](../docs/adding-a-model.md).

## Layout is a security boundary

The subpackages of `readily_engine` are the stable targets of the threat
model's review triggers and the Semgrep rules in `../.semgrep/`
([threat model](../docs/threat-model.md)):

| Package | Boundary |
|---|---|
| `catalog/` | parses the baked Catalog Manifest — the trust root deciding what the Engine may fetch and run; offline and pure (B1) |
| `curation/` | the developer-only script that mints the pins `catalog/` later trusts; composes the packages below rather than importing what they own (B1) |
| `download/` | the ONLY package that may touch the network, and owner of the HF environment policy (B1, Egress) |
| `store/` | staging → verify → atomic promote, re-verified at startup; zero network (B1) |
| `loading/` | the ONLY package that may call model loaders; promoted paths only (B2) |
| `server/` | owns the bind (`serve()`, loopback only, announced on stdout only once held), token sourcing + bearer auth, Origin allowlist, and how long the listener lives (B3) |

Moving or renaming any of these means updating the threat model's
review-trigger table and every Semgrep rule that names the path — in the
same PR.

## Test layout

Unit tests (`tests/`) must not import `mlx`: they run on ubuntu, where no
MLX wheel exists, so an `mlx` import is a hard failure by construction. The
real MLX path belongs to the macOS smoke lane. **No model downloads or
inference in unit tests, ever.**

`tests/architectures/` is laid out the way `loading/` is: one file per
Architecture, plus what they share.

| File | Holds |
|---|---|
| `conformance.py` | `Conformance`: what every Architecture owes, run across `synthesizer_for` with the Backend faked underneath |
| `fakes.py` | one fake per Backend family (`FakeOnnxRuntime`, `FakeMlx`), the scripted MLX model, the store's promoted-directory answer |
| `test_<architecture>.py` | that Architecture's `TestConformance` subclass, and tests for only what is unique to it (Kokoro's dialects, Kitten's respelling and repairs, Supertonic's four graphs, Qwen3-TTS's Voice Reference) |
| `test_styletts2.py` | the StyleTTS2 family synthesizer over a synthetic `Spec`: every seam a spec file can set |
| `test_registry.py` | every registered Architecture answers the whole interface and runs conformance |
| `test_mlx_lane.py`, `test_lazy_synthesizer.py`, `test_references.py`, `test_styletts2_timings.py` | the shared machinery beneath the Architectures |

A behaviour every Architecture must have is a conformance test, never a
copy in each Architecture's file. `conftest.ENTRY` and
`generation_fakes.record()` name no Architecture, so a test that needs one
names it.
