# Threat model

Scope: **MVP** (typed, pasted, or `.txt`/`.md`/`.docx`/`.epub`/`.pdf` file
text, or the text of a page the reader opens by its link → narration;
curated Catalog; the page fetched by the Engine, egress row 5 and B3; Word,
EPUB, PDF and page parsing in the webview, B4).
This document is what every reviewer is pointed at during PR review, and its
[review triggers](#review-triggers) define which PRs get the deeper
trust-boundary review (see [security-pipeline.md](security-pipeline.md)).

## Assets

1. **The user's machine.** Readily downloads and executes powerful things by
   design: Voice Model weights, a Python runtime, wheels. Every download path
   is a potential code-execution path.
2. **The privacy claim.** "Readily downloads; it never uploads." The user's
   content — Sources, History, audio — never leaves the machine. Network
   egress is exactly the downloads the user asked for (models, first-run
   runtime), nothing else, ever.
3. **Release-artifact integrity.** The app build users install and the `v*`
   tags it is cut from. A tampered release is code execution on every user's
   machine.

## Egress inventory

The privacy claim, made checkable. Sanctioned egress operations:

| # | What | Where | Guard |
|---|---|---|---|
| 1 | Voice Model downloads (`huggingface_hub`, pinned revisions) | `engine/src/readily_engine/download/` — the Engine's one download package | Semgrep **no-egress** rule allowlists only this module; `HF_HUB_DISABLE_TELEMETRY=1` set by `download/environment.py` at startup (ADR 0003) |
| 2 | First-run Engine provisioning (bundled `uv`: interpreter + wheels) | Rust supervisor (ADR 0001) | `uv sync --locked --no-dev` against the committed, hash-pinned `uv.lock`; it is the supervisor's only network-capable invocation — the Engine itself launches with `uv run --no-sync`. `--no-dev` matches the set `licenses-engine-macos` clears, so nothing reaches a reader that the license policy never inspected |
| 3 | Reader-opened licence source page | `src/shell/browser.ts` hands the URL to the default browser | Explicit reader activation; Tauri scope allows only `https://huggingface.co/**`; Semgrep keeps the production plugin import at this one call site |
| 4 | Over-the-air update check and download | `src-tauri/src/update/` — the Rust supervisor, never the webview | One endpoint compiled into the bundle from `tauri.conf.json`; the release archive is rejected unless it verifies against the minisign public key compiled in beside it; the proxy features are off (`default-features = false`, `.no_proxy()`) so neither leg can be routed anywhere else; the webview holds no `updater:` capability, so the page cannot name a host, a header or a version; a Linux build makes the request but never downloads or installs, and announces a newer release with a link to the download page compiled into the binary; the install comes through the reader's package manager from Readily's apt and dnf repository, whose indexes are signed with an OpenPGP key that lives on the release Mac and name each package by its SHA-256 (ADR 0016, ADR 0018) |
| 5 | Reader-opened link: the page a reader asks to read | `engine/src/readily_engine/download/link.py`, reached by `POST /v1/sources/fetch` from the composer's "Open link" | Explicit reader action, never on paste; https only, no credentials in the URL; one DNS lookup whose every answer must be a public address, then a connection to that address with TLS verified against the hostname; redirects followed by hand, at most five, each checked the same way; 4 MiB, and 30 seconds for every wait on the socket (the DNS lookup is bounded by the OS resolver instead); `text/html` or `text/plain` only. Standard library `http.client`, which reads no proxy settings; a bare GET with Readily's user agent, no cookies and no referrer |

Everything else — UI, chunker, playback, storage — is zero-network. The
table lists what Readily sends; it leaves out what macOS sends for it. A
reader who syncs Documents to iCloud has macOS upload every Export (the
audio, and a file name from the Source's first words) as it would any file
they saved there. The webview's CSP allows no remote origins, so the UI cannot leak even by bug.
Row 3 reveals which Voice Model the reader inspected to Hugging Face, but
through the reader's browser: Readily's webview and Engine make no outbound
request for it. Row 4 is the one
request Readily makes without being asked, and it is the narrowest shape
that can still answer "is there a newer Readily": a GET of a fixed URL. It
discloses the reader's IP address, the plugin's own user agent
(`tauri-plugin-updater/2.11.0`) and the time of the request to Vercel, which
serves that URL (`readily-updates.vercel.app`, the `readily-updates` project
in `docs/release.md`), plus what any HTTPS request shows a host: the TLS
handshake and reqwest's default headers. The check runs once per launch, so
that host's logs are a record of when each reader opened Readily. An
update the reader accepts is then fetched from `github.com`, which redirects
to GitHub's asset host: the archive is an asset of the public mirror's
release (`docs/release.md`), so GitHub sees the same three things about the
copies that update, as it does about every browser that downloads the DMG. The
download site, a second Vercel project (`readily-download.vercel.app`), sees
the same three things about a browser, and its Vercel Web Analytics script
reports each page view to Vercel with the page's URL and referrer, from which
Vercel derives the visitor's location down to the city, browser, OS and
device type; it counts visitors by a hash of the request that it discards
after 24 hours. The check does **not** disclose the version they are running — the endpoint carries no `{{current_version}}`
template, so every copy that checks sends an identical request — and it
carries nothing about what they read. The host is a Vercel-assigned
subdomain with no custom domain, held only as long as the project is:
deleting or renaming the project would strand every installed copy at an
address nobody controls. A party who later acquired the name would see
reader IPs and could put prose of their own in the update prompt, which
renders `version` and `notes` as text. It could not get unsigned bytes
installed, because the plugin verifies the archive against the compiled-in
public key before it hands the bytes to the supervisor; it could not get an
older signed build installed on a Mac either, because the supervisor reads
the bundle's signed `Info.plist` and requires its version to be newer than
the running build. On Linux the check runs but installs nothing: a newer
release is announced with a link to the download page, which is compiled
into the binary rather than read from `latest.json`, so a party holding the
endpoint could change the prompt's prose and not where it sends the reader
(ADR 0016).

Row 5 is a request the reader makes, to a site they chose, and it tells that
site what a browser visit would and less: the reader's IP address, Readily's
user agent (`Readily (+https://github.com/fahim-m47/readily-public)`), the time,
and the page asked for, with the TLS handshake and a bare GET's headers. It
sends no cookie, referrer, or anything about what else the reader has open,
and the reader's DNS resolver sees the hostname as it would for a browser.
Nothing about the fetch is stored or logged; the page's text becomes the
draft, where the reader sees it before anything is narrated.

**No telemetry, no crash reporting, no analytics in the Mac app**, not even
opt-in; adding any later is a threat-model revision, not a settings toggle.

### Local process inventory

The Engine invokes two local macOS system tools with fixed executable paths
and fixed operations: `/usr/bin/tmutil addexclusion` keeps reproducible data
out of Time Machine, and `/usr/bin/afconvert` encodes audio — as FLAC for the
Segment cache, and as AAC/M4A or WAV for Export. The codec and container are
part of the allowlisted argv, so adding a format (MP3, say) is a rule
amendment rather than a code change. Every variable argument is an
Engine-owned local path, Export's included: the Engine writes every Export
into one audio folder the shell names when it spawns the Engine, `Readily`
in the reader's Documents (`src-tauri/src/data.rs`), under a
name made from the Source, and the request that asks for one carries only
the format and refuses any other field. The name is claimed exclusively,
so an Export never replaces a file already there; it takes the next free `… 2`, `… 3`. Stated plainly: a
compromised webview can make Readily add audio files to that folder, and
nothing else — it cannot name a path, overwrite a file, or write outside
the folder. Letting a request say where an Export goes is an amendment to
this paragraph. Semgrep blocks other
`subprocess` imports, `subprocess` and `asyncio` invocation APIs, and direct
`os` spawn, exec, fork, and shell APIs. It separately checks every invocation
in the three modules whose `subprocess` imports are explicitly acknowledged,
and rejects aliases of the covered invocation functions.

The Rust shell spawns two programs of its own. `uv` provisions and launches
the Engine (row 2 above). `/usr/bin/open` shows Readily's data folder in the
Finder, behind Settings' "Open data folder" button — absolute
executable path, no shell, and exactly one argument: the data directory the
Rust side computes for itself from the platform's application-support
directory. Behind "Open audio folder" it is the same binary and the folder
Exports land in, which `src-tauri/src/data.rs` computes from the home
directory by the Engine's own rule. Behind "Show log file" it is the same
binary with `-R` and the log file's path, which `src-tauri/src/logs.rs`
computes the same way. No argument ever comes from the webview, so what is
reachable is two folders and one file rather than a path the page names (B4). Semgrep's spawn rule is
Python and covers the Engine tree only; these two are held instead by review
of `src-tauri/`, which `CONTRIBUTING.md` routes to the trust-boundary review.
One file under that tree is excluded from the spawn and egress rules by
name: `engine/tools/reference_clips.py`, the curation-time script that
fetches two consented corpora and runs ffmpeg on a curator's machine to cut
the Voice Reference clips. It is a separate uv project, never shipped and
never imported by `readily_engine`; the Catalog pins what it cuts by digest.

## The log file

`logs/readily.log` in Readily's data folder (`~/Library/Application
Support/Readily` on macOS, `~/.local/share/Readily` on Linux) is the one file a
reader is asked to send with a bug report, and it is designed for that.
The shell writes its own records there (`src-tauri/src/logs.rs`) and
relays the Engine's stderr into it, a megabyte at a time with one older
file kept, so the two together never pass about two megabytes. It holds
Engine states, exit codes, update outcomes, Narration and Segment ids,
ordinals, attempt counts and durations. It never holds a Source, a Block,
a voice sample, an Export's path (its name is the Source's first words),
or the launch token.

What keeps those out is the call sites: every `logger` call in the Engine
and every `log` macro in the shell writes ids and status, and an Export
failure is recorded as its exception's type because the message would
quote the destination. Behind the call sites sit two backstops. The
Engine's logging config (`readily_engine/logs.py`) records every
exception as its frames and its type, never its message, because
`ValueError(block.text)` is the one short record a careful call site
cannot prevent, and installs itself as the interpreter's exception hooks
so an uncaught one is recorded the same way; both it and the shell's
writer withhold any line longer than a status line could be, so a record
that quotes a paragraph is written as its length, and spell the reader's
home `~`. The supervisor names the bundle's paths by what they are in
every line it relays, since a traceback printed before the Engine's
config exists names its files in full and the bundle sits wherever the
reader put it. A Block, or a short Source, is shorter than
that, so the length rule is not a filter; the rule is the call sites. A test runs a whole Narration, with a failed and
retried synthesis, with every logger at `DEBUG`, and greps the result for
the Source. `readily.db` in the same folder *does* hold every Source the
reader ever pasted, which is why the bug template asks for the log files
and says not to attach the folder. Adding a log line that quotes what a
reader typed is a threat-model amendment, not a debugging aid.

## Trust boundaries

### B1. Catalog Manifest & model download/verification (ADR 0003)

- *Compromised or lazy-renamed upstream HF repo swaps weights* → repos are
  pinned to an immutable revision commit **and** every file's SHA-256 is in
  the baked-in manifest; the Engine refuses hash-mismatched files.
- *MITM on the download* → HTTPS + the same SHA-256 check.
- *Partially-written or tampered model dir gets loaded* → download to
  staging → verify → atomic promote; **a promoted dir exists ⇔ verified and
  complete**; the Engine loads only from promoted paths (Semgrep
  **no-load-without-verification** rule). A promoted dir that stops being
  complete afterwards (a file removed beside it, a later Manifest pinning a
  file under it) is re-verified at Engine startup and retired, so it is
  downloaded and verified again before it is installed.
- *Malicious PR edits a manifest URL/hash* → `catalog/**` is a review
  trigger; manifest changes are curation PRs with provenance checked.
- *Upstream repo adds a malicious extra file* → the Engine downloads exactly
  the files the manifest names — never "the whole repo".

### B2. Model loading (weights-format policy)

- *A model file is itself a program* (pickle: `.pt`/`.pth`/`.ckpt`/`.bin`
  via `torch.load`) → **banned loaders**: no `pickle`, no `torch.load`, no
  `joblib`, no `np.load(allow_pickle=True)` anywhere in the Engine
  (Semgrep rule). Allowed weight formats: **safetensors** (MLX lane) and
  **ONNX** (ORT lane); sidecar assets as `.npz`/`.npy` with
  `allow_pickle=False` and plain JSON. Admitting any new format is a
  threat-model amendment, not a loader patch.

  The dev-only exception is `engine/tools/export_pronunciation.py`.
  It checks the NRC checkpoint against one committed SHA-256 before calling
  `torch.load` in its separate export environment. That exact call has a
  Semgrep suppression; the rule still covers every other Engine path.
  The tool and its Torch dependencies are excluded from the shipped Engine.
  Its ONNX and JSON outputs ship beside the Engine source with their MIT
  notice. The pronunciation loader checks both against committed SHA-256
  pins before parsing or inference; the app bundle check compares these
  resources byte for byte. They never pass through the download store. The runtime's allowed formats are unchanged.

Bundled Voice References also stay outside the download store. Curation pins
each WAV's SHA-256 in the Catalog Manifest, and the loader verifies it before
decoding at Engine start. The shipped samples are already shaped for
conditioning; loading does not trim them. A changed clip, transcript, or
digest requires an entry version bump. Qwen versions 1–3 predate these pins,
so once a version 4 or later model is loaded, replay and Export treat their
cached Segments as misses.

### B3. Localhost API (webview ↔ Engine, ADR 0001)

- *A malicious webpage in the user's browser hits `127.0.0.1:<port>` (DNS
  rebinding / CSRF)* → the Engine's listening bind is loopback only and
  owned by `server.serve()` (Semgrep **bind-only-in-server** rule) — the
  only bind in the product. Every route — SSE included — requires the
  per-launch 128-bit bearer token (a request without it gets 401); Origin allowlist:
  a request carrying a browser Origin outside the Tauri webview origin is
  rejected 403, while requests with no Origin (the supervisor, `curl`)
  still need the token. Vite's dev origin is admitted only on a **dev
  run** — Tauri's own `dev` cfg, which is off in anything bundled, and not
  the Cargo profile the app happens to be compiled with. The two come
  apart: `tauri build --debug` bundles an app with `debug_assertions`
  still on, and gating the origin on that would leave a shipped app
  trusting `http://127.0.0.1:1420`, making any page the user's own machine
  serves there a permitted origin. A `uv` child inherits an allowlist
  (`PATH`, `HOME`, `TMPDIR`, `XDG_RUNTIME_DIR`) rather than the
  environment the app was launched from, so
  `READILY_ENGINE_ALLOW_DEV_ORIGIN` reaches the Engine
  only where a dev run sets it and a stray value in a developer's shell
  cannot re-open it in a bundled build. Non-HTTP protocols (WebSocket)
  are refused outright rather than passed around auth.
- *A local process squats the Engine's port and is handed the token* → the
  supervisor never picks or reserves a port. The Engine binds `:0` itself
  and only then announces the bound port on stdout (`server/serve.py`), so
  the port never exists un-owned: there is no reserve-then-release window
  in which another local user's process could bind it and receive the
  launch token from the supervisor's first health probe.
- *Local process reads the token* → token is handed to the Engine via
  environment at spawn and to the webview in-process — **never in argv**
  (`ps` shows argv to every local user; a process's environment is
  readable only by its own user).
- *The Engine outlives the app and keeps listening* → an orphaned Engine is
  a token-guarded loopback listener nobody owns. A normal quit SIGKILLs the
  child's whole process group — `uv run` does not exec, so the Python
  Engine is a grandchild a plain kill would miss, and one wedged in a
  native call (the MLX crash class) can be past honoring any polite
  signal. For the paths no shutdown handler ever sees — crash, force-quit —
  the supervisor holds one end of a pipe for its whole life and the Engine
  exits on EOF (`server/lifetime.py`).
- *The supervisor's own health probe becomes an egress path* → it is a
  hand-written loopback request, not an HTTP client crate: general clients
  honour `HTTP_PROXY`/`ALL_PROXY` from the environment, and a probe that
  can be pointed off-machine has no place in an app that never uploads.
- *A link turns the Engine into a proxy onto the reader's own network
  (SSRF)* → a page the reader opens could name, or redirect to, the
  Engine's own port, the router's admin page, or a cloud metadata address,
  and have the Engine GET it with the reader's network position. The
  route is token-guarded like every other, so only the webview can call
  it, and `download/link.py` refuses before connecting to any host that is
  not on the public internet: it resolves the name once, refuses if any
  answer fails `ip.is_global` (loopback, private, link-local and so
  `169.254.169.254`, carrier-grade NAT, unique-local, and an IPv4-mapped or
  6to4 address wrapping one of those), and then connects to that checked
  address with TLS verified against the hostname. There is no second lookup
  for a rebinding DNS server to answer differently. Redirects are followed
  by hand, at most five, and every hop is checked the same way before it
  is fetched; an IP-literal URL is checked as its own answer. The URL must
  be https and carry no credentials, and `http.client` reads no proxy
  variables, so nothing in the environment can reroute the fetch. What
  comes back goes only to the webview, capped at 4 MiB and 30 seconds (the
  DNS lookup is the one wait outside that budget; the OS resolver bounds
  it), and only as `text/html` or `text/plain`, which the webview reads as
  data (B4).
- Non-goal: defending against an attacker already running code as the same
  user (see Accepted risks).

### B4. Webview & shell (Tauri capabilities, entitlements)

- *Pasted Source text executes as markup in the read-along view* → text is
  data: React's default escaping, **no `dangerouslySetInnerHTML`** (Semgrep
  rule), CSP with no inline script and no remote origins. The same answer
  covers the licence text the Catalog sheet now shows: it is authored
  upstream, and its pinned hash says the bytes are the ones the Manifest
  named, nothing about what they say.
- *Importing a file becomes a way to read the disk* → the webview may
  receive file contents the OS hands it through a browser drop or file
  input; it may never ask Rust or the Engine to read a path. A `.txt`,
  `.md`, `.docx`, `.epub` or `.pdf` file dropped on the composer, or picked behind
  its "Open file" button, arrives through the browser File API as a `File` — bytes and a
  name, no path — and its text replaces the draft through the same
  `onTextChange` typing uses (`src/shell/importSource.ts`). That is the only
  path: no `fs:` or `dialog:` permission was added for it, and no
  command takes a path, so a compromised page has nothing to name. Tauri's
  own drag-and-drop handler is off for the window (`dragDropEnabled: false`
  in `tauri.conf.json`), which is what lets the drop reach the page at all;
  a file let go anywhere else in the window is refused at the document, so
  the webview never navigates to it. The file's size is checked against a
  cap before a byte is read — 8 MB for text, 100 MB for a document — and
  the text a document yields is held to the same 8 MB, which bounds the
  read and the character count the composer spreads afterwards. The text is
  decoded as UTF-8 and rendered as data, like a paste. A `.md` file's markup is tidied line by
  line for the Voice's sake — not parsed, rendered or followed — so a link
  in it stays text and nothing is fetched.
- *A crafted document exploits its parser or exhausts memory* → every
  parser is JavaScript in the webview (`src/shell/formats/`), so a parser
  bug lands inside the page the previous bullet already treats as
  untrusted: no native code, no new permission, and CSP still keeps it
  from loading or sending anything. A Word file is a zip; it is unpacked
  in a module Web Worker with fflate's streaming inflater, only
  `word/document.xml` is inflated, and the inflated bytes are counted as
  they come out — never trusting the size the archive declares — against
  a 32 MiB cap, so a zip bomb is refused as "unpacks to more than Readily
  opens". The XML is parsed with the browser's `DOMParser`, which fetches
  no DTD or external entity, and only paragraph, run, tab and break
  elements are read; deleted revisions, headers and footers are skipped.
  An EPUB is a zip too, unpacked the same way under the same cap, with
  only its XML and XHTML entries inflated. The package it names is read
  for the reading order, and every path in it is resolved within the
  archive and looked up among the entries already unpacked, so a
  `../../` reference names nothing and no path reaches the disk. A book
  whose `encryption.xml` encrypts anything by more than font obfuscation,
  or cannot be parsed, is DRM-locked and refused as locked; one that only
  obfuscates fonts reads.
  Chapters are parsed with `DOMParser` into inert documents — no script
  runs, and no image, stylesheet or link is loaded — and only their text
  is walked, leaving out the head, scripts, styles and navigation. A PDF
  is read with Mozilla's pdf.js, whose parser runs in its own module Web
  Worker served from the app bundle, and only its text-extraction API is
  called: no page is drawn, no annotation, form or link is acted on, and
  pdf.js 6 compiles nothing with `eval`, which CSP forbids regardless. It
  is handed the file's bytes and no CMap or font URL, so it has nothing to
  fetch; CJK text that needs a CMap comes out garbled or empty rather than
  loading one. A PDF behind an open password is refused as locked; one
  with only an owner password opens, as in any reader. The text is data
  from there on, like a paste. A page opened with "Open link" reaches the
  webview from the Engine as bytes (egress row 5); the webview never
  contacts the site itself, since CSP still confines its connections to
  the Engine. An HTML page is parsed with `DOMParser` into a detached
  document — no script runs, and no image, stylesheet or frame is loaded
  — and Mozilla's Readability picks the article out of it; only the
  article's text is walked, and nothing is rendered or followed. A
  plain-text page is read as text. A link typed or pasted into the draft
  is text too: only the reader pressing Read in the "Open link" row
  fetches one.
- *Webview navigates to remote content* → Tauri serves bundled assets only;
  `connect-src` is the Engine's loopback origin, nothing else. A licence's
  source page is the one remote page the app can reach, and reaching
  it is not navigating: it is handed to the reader's own browser through
  the opener plugin, and Readily's window stays where it was. A native
  navigation hook allows only Tauri's app origin and Vite's exact development
  origin, so alternate link activation cannot bypass the opener.
- *Over-broad native capability* → Tauri capabilities minimal — exactly
  a URL-scoped `opener:allow-open-url`
  for that source page, and the two argument-free window commands
  (`start-dragging`, `internal-toggle-maximize`) that let the title-bar
  row move and zoom its own window — and nothing more. The updater plugin
  is registered for its Rust half and granted **no** capability at
  all, not even `updater:default`: its own `check` command takes a proxy, a
  header map, a timeout and a target from whoever calls it, so granting it
  would hand a compromised page an outbound HTTPS request it gets to
  address. The log plugin is registered the same way and granted no
  capability either: its `log` command takes the message from the caller,
  and the page holds the Source, so granting it would let a compromised
  page write into the one file designed never to hold one.
  `tauri-plugin-process` is not a dependency, so `process:allow-restart`
  has nothing to grant; the restart is `AppHandle::restart()` in Rust.
  A lib.rs test asserts that no granted permission identifier begins with
  `updater:`, `process:` or `log:`, and a Semgrep rule keeps both plugins' JavaScript
  packages out of `src/` entirely. The opener is granted no default
  set: its scope is `https://huggingface.co/**` — the host the Manifest
  pins every entry's files at — so the webview cannot hand the platform a
  file, a program, or another site to open. The plugin's automatic link
  interception is disabled, and Semgrep keeps its JavaScript API at one
  production import site (`src/shell/browser.ts`). Export takes only a
  format; the **Engine** names the audio folder and the file itself (see
  Local process inventory), so the page can add files to that folder but
  never choose where they go. The webview never gains file-write
  capability of its own, so Export is the single narrow write the shell
  can reach — one rule, in one place, rather than a filesystem capability
  spread across the UI. The supervisor is granted
  **no** shell capability and needs none: the Engine is spawned by Rust the
  webview cannot address, so a compromised page can name no command and
  supply no argument.
  The release bundle runs under the hardened runtime and is notarized,
  and its entitlements file is deliberately empty: no microphone,
  camera or network-server entitlement, and none of the runtime exceptions
  (unsigned executable memory, library validation off, inherited
  entitlements) that would hand a loaded dylib the app's identity. An
  entitlement gets added only when a notarized build fails at runtime and
  names the one it wants.
- *A supervisor command becomes a way to run something* → the nine
  commands the webview can reach — `engine_status`, `engine_config`,
  `engine_retry`, `open_audio_folder`, `open_data_folder`, `reveal_logs`,
  `update_status`, `update_install`, `open_download_page` —
  take **no arguments at all**, so
  there is nothing in them for a compromised page to steer. They are
  enumerated in one `invoke_handler` list in `src-tauri/src/lib.rs`, and
  Tauri keeps only the last such call, so that single list is the whole
  reachable set. `open_audio_folder`, `open_data_folder` and `reveal_logs`
  do start `/usr/bin/open`, but each on a path it computes itself — the
  audio folder Exports land in, Readily's own data directory, the log file
  inside it — so what a compromised page gains is one Finder window, not a
  path and not a program name. `engine_retry` does start
  `uv sync --locked`, the one step that reaches the network, but only with
  the fixed argv `src-tauri` already holds — `--project` naming the bundled
  Engine directory ahead of the subcommand, so the committed hash-pinned
  `uv.lock` is the one that decides — and only from `Failed`; every other
  state ignores it. What a compromised page gains is the power to make the app
  rebuild its own environment, which it can already provoke by killing the
  Engine; what it does not gain is a command, a path, or a package name.
  `update_install` does fetch and run an installer, but the page
  cannot say which: the endpoint and the minisign public key are compiled
  into the bundle, the command re-checks the endpoint itself rather than
  trusting anything it was told, and the archive is refused unless it
  verifies against that key. What a compromised page gains is the power to
  install the release we published, at a moment of its choosing — which is
  the same software the reader was about to be offered anyway — and it can
  only do so once, because the status machine refuses a second install while
  one is running. What it does not gain is a URL, a host, a header, a proxy
  or a version. `open_download_page` hands the reader's browser one https
  page compiled into the binary, so what a compromised page gains is a tab
  on Readily's own download page.
  There is no `engine://status` event and nothing for a screen to
  subscribe to: the webview polls, so no listen capability is ever
  granted.

### B5. First-run provisioning & dependency supply chain

- *Compromised PyPI release of a pinned dep / typosquat* → resolution is
  frozen by hash in `uv.lock`; bumps arrive as reviewed Dependabot PRs;
  cargo-deny (advisories/bans/sources) and the `licenses` job gate the
  other lockfiles; Bun postinstall scripts stay disabled.
- *Interpreter supply chain* → `uv` ships inside the app bundle, fetched at
  build time by `scripts/prepare-bundle.sh` against a pinned version and two
  SHA-256 constants — the release archive, and the binary taken out of it —
  and pins the python-build-standalone build it provisions in turn.
  The release bundle is Developer ID-signed and notarized, and
  the bundle seal in `Contents/_CodeSignature` covers `Contents/Resources` —
  the shipped `uv` and all 74 of the Engine's `.py` files among them — so a
  download that was altered in transit or on the website fails Gatekeeper
  the first time it is opened. `uv` is signed separately before the bundler
  copies it, because Tauri signs `Contents/MacOS` and `Contents/Frameworks`
  and nothing under `Resources`, and an unsigned Mach-O there fails
  notarization.
  What the seal does not do is re-check on every launch: macOS validates the
  main executable's pages, not the resource hashes, so a local process
  running as the reader can still rewrite a shipped `.py` afterwards and the
  app will run it. `codesign --verify --deep --strict` is what notices, and
  the release script runs it on the build rather than on the installed copy.
  That residue sits inside the accepted *Same-user attacker* risk below and
  nowhere else. What the seal does buy is that the build-time pins survive
  the download: without it, anyone between the build and the reader could
  rewrite the bundle's contents and nothing would say so.
- *The environment the app was launched from redirects the build* → `uv`
  and the Python behind it read a long and growing list of variables:
  `UV_PROJECT` retargets the whole invocation, `UV_CONFIG_FILE` and
  `UV_DEFAULT_INDEX` decide where wheels come from, `PYTHONPATH` and
  `PYTHONSTARTUP` place a module inside the Engine process. Every `uv`
  child is spawned with the environment cleared to an allowlist (`PATH`,
  `HOME`, `TMPDIR`, and `XDG_RUNTIME_DIR` for a Linux desktop's sound
  server), and `--project` sits ahead of the subcommand — the
  only position that is right for all three invocations, because `uv run
  <cmd>` hands everything after `<cmd>` to the child — so the working
  directory is not the only thing saying which project gets synced and run
  (`engine/launch.rs`).
  Dropping the rest of the environment also drops a corporate-proxy user's
  `HTTPS_PROXY`, which is the cost of not letting that be set for them.
- *A config file redirects the build where a variable no longer can* →
  `HOME` is inherited, so `~/.config/uv/uv.toml` is still read, and an
  `index-url` there still moves where wheels are fetched from (verified
  under a cleared environment). `--no-config` would close it and cannot be
  used: the Engine's `pyproject.toml` carries a
  `[tool.uv.dependency-metadata]` entry that `uv.lock` was resolved
  against, so `--locked --no-config` fails against its own lockfile. The
  wheels are bounded rather than shut: `engine/uv.lock` pins 1120 hashes
  across the 81 packages that have an artifact, the direct-URL spaCy model
  included, so a redirected index can stall a first run but cannot
  substitute a package.

  The interpreter is not bounded that way, and this is the sharpest edge
  in B5. `python-install-mirror` only moves the URL, and `uv` still checks
  the build against the hash compiled into it — a redirected mirror fails
  with a hash mismatch. `python-downloads-json-url` replaces that table
  wholesale, and a table can say `"sha256": null`; `uv` then downloads and
  unpacks whatever it names, unverified. That is the interpreter the
  Engine runs, so the bullet above ("`uv` pins the build it provisions")
  holds only while nobody has rewritten the table it pins against. What
  keeps this off the risk register rather than on it is who can reach it:
  writing `~/.config/uv/uv.toml` already means holding the user's account,
  and an attacker there can replace the app itself. It is recorded because
  a control the app does not have should not read as one it does.

### B6. CI, release & repo (the build's own boundary)

- *Malicious fork PR exfiltrates secrets / poisons a release* → fork PRs
  run CI under `pull_request` with no secrets, never `pull_request_target`;
  workflow `permissions: contents: read`; actions SHA-pinned (Dependabot
  cooldown); zizmor + gitleaks in CI.
- *Tampered release tag* → `v*` tags immutable by ruleset; squash-only,
  no-bypass merge policy as code in `.github/rulesets/`.
- *A tampered download reaches a reader* → the macOS artifact is
  Developer ID-signed, notarized and stapled, and `scripts/release-candidate.sh`
  records the DMG's SHA-256 and the commit it was built from beside it, so
  what is on the website can be matched back to a revision. Build
  provenance attestation and an SBOM are future work
  (security-pipeline.md, "Later"). The signing key and the App Store Connect API key
  live in the maintainer's keychain and `~/.private_keys`, never in the repo
  and never in CI: no workflow can sign or notarize anything.
- *A tampered **update** reaches a reader* → the update archive carries a
  minisign signature made by a key that exists only in the maintainer's
  password manager, and the public half is compiled into every build.
  A host that can serve the update endpoint or the archive — by owning the
  website or the release host, by owning DNS, or by terminating the TLS —
  can withhold an update, but cannot
  get unsigned bytes past `tauri-plugin-updater`'s verification, and the
  shipped app never installs anything without the reader agreeing first.
  `latest.json` is not signed, so such a host can still invent the version
  and notes shown in the prompt. It cannot turn that lie into a downgrade:
  after the plugin verifies the signature over the archive bytes, the
  supervisor reads the bundle's `Info.plist` from those bytes and refuses
  the install unless that version is newer than the running build. Linux
  has no update archive to tamper with: Readily ships there as a `.deb` or
  an `.rpm`, which the reader's package manager updates from Readily's apt
  and dnf repository (ADR 0016, ADR 0018). Its indexes are signed on the
  release Mac with an OpenPGP key that, like the minisign key, lives in
  the maintainer's keyring and password manager and never in the repo or
  CI; the public half is committed at `site/download/linux/readily.asc`
  and imported once by the reader. The indexes name each package by its
  SHA-256, and the packages sit on the mirror's GitHub release behind a
  redirect, so a host that owns the download site, GitHub or the path
  between can withhold a package or serve a stale index, but cannot get
  other bytes past apt's or dnf's check of the package against the signed
  index. The stale index is accepted: the `Release` carries no
  `Valid-Until`, so an old signed index can be served for as long as the
  key stands, and a reader held on it never hears of a fix. An expiry
  would mean re-signing on a clock, for a repository with one package in
  it. Downgrade is the package manager's to refuse: apt and dnf install
  a lower version only when told to. **Losing the package signing key**
  means a new key that every Linux reader imports by hand.
  The plugin's install path has one escalation in it: when the running
  bundle's folder is not writable, it asks macOS for
  administrator rights and moves the bundle as root, and it installs a
  .deb or an .rpm as root through `pkexec` or `sudo`. Readily never reaches
  either prompt, because the supervisor refuses to offer an update at all
  unless the copy is a Mac bundle, its folder is writable, and it is not
  Gatekeeper's translocated one (`src-tauri/src/update/mod.rs`); a reader
  who cannot be updated in place downloads a fresh copy instead, and a Linux
  package updates through its package manager. **Losing the updater private
  key ends over-the-air updates for every installed copy**: the public key is
  baked into bundles already on readers' machines, so a new key reaches them
  only as a fresh download. Rolling it is a release, not a config change.

## Non-boundaries — kept that way

Structural absences doing security work. Reintroducing any is a
trust-boundary PR **and** a threat-model amendment:

- **No archive extraction in the Engine.** Models arrive as individual
  verified files; nothing in the Engine unpacks zips or tarballs, so zip-slip
  cannot exist there. The one archive Readily opens is an update's own
  `.tar.gz`, unpacked by `tauri-plugin-updater` in the supervisor **after**
  its minisign signature verifies against the key compiled into the bundle —
  so the bytes being unpacked are bytes we signed, and an attacker who can
  serve the endpoint cannot reach the extractor at all.
- **No runtime catalog fetch.** The manifest is baked into the release
  (ADR 0003) — "compromised catalog host" is structurally impossible.
- **No silent updater.** Readily checks for a newer release on launch and
  downloads nothing until the reader says yes; there is no background
  install, no staged-on-quit swap, and no way to turn the asking off. The
  check is fail-quiet by design: a check that cannot be made looks exactly
  like nothing published, because a reader who never asked about an update is
  owed no error about one. See row 4 of the egress inventory and B4.
- **No telemetry of any kind.**

## Accepted risks

- **Curation-time trust.** Hash pinning proves we got what we pinned, not
  that what we pinned is good. Mitigation is the curation PR review
  (provenance, license, community standing); residual risk accepted.
- **Same-user attacker.** The Engine is unsandboxed (App Sandbox is
  incompatible with the sidecar + uv pattern); the SQLite DB and Segment
  store are plain files. An attacker already running as the user has lost
  the game elsewhere; disk-at-rest privacy is FileVault's job.
- **Availability.** An Engine crash kills playback (ADR 0002); a corrupted
  cache re-synthesizes. Annoying, not dangerous.

## Review triggers

A PR touching any of these is **trust-boundary tier**: the required checks
plus a deeper security review before merge, and it must say so in its
description (CONTRIBUTING.md). The layout conforms to this list, not vice
versa — moving a package means updating this table and
the Semgrep rules in `.semgrep/` in the same PR.

| Path | Boundary |
|---|---|
| `catalog/**` (manifest, curation scripts) | B1 |
| `engine/src/readily_engine/catalog/**` (the Manifest's only reader — decides what may be fetched) | B1 |
| `engine/src/readily_engine/curation/**` (the script that mints the pins the rest of B1 enforces) | B1 |
| `engine/src/readily_engine/download/**` (the allowlisted egress package) | B1, Egress |
| `engine/src/readily_engine/store/**` (download/verify/promote) | B1 |
| `engine/src/readily_engine/loading/**` (model loading, backends) | B2 |
| `engine/src/readily_engine/server/**` (auth, bind, CORS/Origin) | B3 |
| `src-tauri/**` (supervisor, capabilities, `tauri.conf.json`, entitlements, the update endpoint and its public key) | B3, B4, Egress |
| `src/shell/importSource.ts`, `src/shell/formats/**` (the one place a file's or page's bytes become a Source, and the parsers it loads) | B4 |
| `engine/src/readily_engine/logs.py`, `src-tauri/src/logs.rs`, and any diff adding a `logger`/`log` call that formats text a reader typed | The log file |
| `.github/**` (workflows, rulesets, dependabot) | B6 |
| `scripts/release-candidate.sh` (signs, notarizes and signs the update archive) | B6 |
| `scripts/publish-release.sh` (puts the DMG and the archive on the mirror's GitHub release, and `latest.json` and the pages where readers and installed copies fetch them) | B6 |
| `SECURITY.md`, `docs/threat-model.md`, `docs/security-pipeline.md` | all |
| Any diff adding a network call site (Semgrep flags it) | Egress |
| Any diff adding a **new direct dependency** to the Engine (new package in `engine/pyproject.toml`) | B5 |

Explicitly outside the tier: UI/React components, chunker, playback, storage
schema, docs, lockfile-only version bumps (CI's advisory + license gates
cover those).
