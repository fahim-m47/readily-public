# Readily

**Local, open-source, hyperrealistic text-to-speech for your Mac.**

Paste or type anything — a page, a chapter, a whole book — choose a Voice,
and Readily narrates it with an open-source Voice Model that runs on the Mac
itself. Nothing you paste, narrate or keep leaves the machine. Reading links
and files is future work; typed and pasted text is what Readily takes today.

> **Status:** signed, notarized builds for Apple Silicon Macs are published at
> [readily-download.vercel.app](https://readily-download.vercel.app).
> Installed copies update from `readily-updates.vercel.app` when the reader
> accepts.

## Requirements

- An **Apple Silicon** Mac (M1 or later) running **macOS 14 (Sonoma) or
  newer**. Both bounds come from MLX, which the expressive Voice Models
  need: it has no Intel build, and its wheels start at macOS 14. On anything
  older macOS refuses to open the app at all.
- **About 4GB of free disk** for a first run, plus each Voice Model you
  download. They range from 24MB (Kitten TTS Nano) to 1.7GB (Qwen3 TTS).
- **An internet connection for the first run, and for anything you add
  after it.** The app ships no Python; it downloads a pinned interpreter and
  its libraries the first time you open it, along with a first Voice Model.
  Narrating with a Voice Model you already have is entirely offline; a
  further Voice Model, or an Update, is a download like any other.

## Getting it

A signed, notarized disk image is published for each release at
[readily-download.vercel.app](https://readily-download.vercel.app), with an
installation note beside it. Open the image, drag Readily to Applications,
and open it. Gatekeeper opens it like any other app; there is no right-click
dance and no `xattr` to clear.

Readily checks for a newer version once at launch and offers it. It installs
one only when you say so, and it never installs an older build over a newer
one. Once the first run is behind you, that check is the only request
Readily makes without being asked — everything else it fetches, you asked
for.

To build from source instead, see [Building](#building).

## What the first run does

Opening Readily the first time shows a setup screen while a bundled `uv`
provisions the Engine — a pinned Python and its libraries, downloaded from
PyPI and verified against committed hashes. It takes a few minutes on a
normal connection and only happens once. If the network drops partway
through, the screen offers a retry rather than dying.

The same screen then fetches one Voice Model, the Catalog's default,
because an app that cannot speak is not an app yet. It is the one download
Readily makes for you rather than at your asking. When the screen goes, the
app can narrate: paste some text and press Narrate.

Every later Voice Model is yours to start. Open the Catalog, listen to the
bundled Voice Previews — they play offline, before any download — and pick
the Voice you want.

## What it does

- **Five Voice Models**, curated and hash-pinned: Kokoro, Kitten TTS Nano
  and Supertonic 2 start instantly; Qwen3 TTS and Chatterbox Turbo take
  longer to load and sound more expressive. The Voices are English.
- **Sources up to a million characters**, narrated in Blocks so playback
  starts long before the whole text is read.
- **Playback** from 0.5x to 4x, paused and resumed, and seekable to any
  word Readily has already narrated. A long Source plays while the rest is
  still being made, and that trailing text is there to read along with
  rather than to click into.
- **History** of every Narration, reopenable after a restart. Cached audio
  is evicted against a disk budget you set, and a Narration whose audio went
  is re-made on demand.
- **Export** of a finished Narration to a single audio file.
- **Simple and Advanced modes.** Simple resolves the Catalog's curated
  settings; Advanced exposes each Voice Model's own controls.

## Where it keeps things

Everything Readily writes lives in one folder, which Settings has a button
to open:

```text
~/Library/Application Support/Readily/
```

That is the provisioned Python environment, the Voice Models, the cached
speech, and your Narration history. Deleting the folder resets Readily to a
first run; deleting the app leaves it behind.

Two caches sit outside it, because they belong to `uv` rather than to
Readily and are shared with any other `uv` on the machine: the interpreters
it downloads (`~/.local/share/uv/python`) and its wheel cache (`~/.cache/uv`,
which can reach several GB). `uv cache clean` reclaims the second; Readily
will re-download what it needs on the next launch.

## Reporting a problem

Readily keeps a log file that records what it did and never what you pasted.
Settings has a **Show log file** button, which reveals `readily.log` in the
Finder. Send that file, what you were doing, and what you expected —
and nothing else from the data folder, because the rest of it holds the text
you narrated.

Security reports go the way [SECURITY.md](SECURITY.md) describes, not
through a bug report.

## Building

You need [Bun](https://bun.sh), [Rust](https://rustup.rs), and
[uv](https://docs.astral.sh/uv/) installed.

```sh
bun install
bun tauri build
```

The finished app lands at
`src-tauri/target/release/bundle/macos/Readily.app`, unsigned. It opens by
double-click on the machine that built it. Moved to another Mac it will not,
because macOS tags what arrives from elsewhere and an unsigned build has no
signature to check — that is what the signed release builds are for.

How a signed, notarized release is built and published is written up in
[docs/release.md](docs/release.md). Release notes per version live in
[docs/release-notes/](docs/release-notes/).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how changes land.

## License

[MIT](LICENSE)
