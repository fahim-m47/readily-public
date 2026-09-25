# Building a release candidate

How a signed, notarized Readily DMG is produced, what a Mac needs before it
can produce one, and how a candidate is put where readers and installed
copies look for it. The tag and the announcement are still by hand.

## What the candidate is

`scripts/release-candidate.sh` builds the committed tree into

```text
dist-release/<version>/
  Readily_<version>_aarch64.dmg           signed, notarized, stapled
  Readily_<version>_aarch64.app.tar.gz    what an installed Readily downloads
  Readily_<version>_aarch64.app.tar.gz.sig  its minisign signature
  latest.json                             what the update endpoint serves
  SHA256SUMS                              checksums for the image and archive
  candidate.json                          commit, version, checksums, notary ids
```

The `.app` inside the image is signed with the hardened runtime and
`src-tauri/Entitlements.plist` (empty by design: the shell spawns `uv` and
the Engine as separate processes, so nothing the hardened runtime forbids
happens inside it), notarized and stapled by the Tauri bundler. The bundled
`uv` is signed by `scripts/prepare-bundle.sh` before bundling, because Tauri
signs nothing under `Resources/` and notarization rejects an unsigned
executable anywhere in the bundle. The image is then notarized and stapled
by the script, so Gatekeeper can pass the download itself offline.

The script refuses a dirty tree, and untracked files count (ignored ones
do not). A candidate is a build of one commit and `candidate.json` names it,
and the bundle copies whole source directories — so a file that was never
added would ship inside an app whose commit does not contain it. Binary
reproducibility is not claimed; two builds of the same commit are two
candidates.

## The update artifacts

The same `.app`, gzipped, with a detached minisign signature: that is what an
installed Readily downloads when the reader accepts an update. Two
things have to line up or over-the-air updates quietly stop working, so the
script checks both before a candidate exists.

- **The key.** The signature must come from the key whose public half is
  compiled into this build, and the script compares their minisign key ids
  rather than trusting that the right key was in the environment. Signing
  with the wrong key produces a valid signature that every installed Readily
  rejects, and the only way out is asking readers to download again. A
  missing `plugins.updater.pubkey` is refused for the same reason: a build
  that trusts no key accepts no update. Switching
  `bundle.createUpdaterArtifacts` off is refused too: it leaves no archive
  to sign.
- **The endpoint.** `plugins.updater.endpoints` in `tauri.conf.json` must
  name exactly one `https` URL, and it must be the updates project's own
  `latest.json`. Both scripts carry the project's address, so moving the
  project means editing both. The `.invalid`
  placeholder is refused: a copy built with it would check an address that
  resolves nowhere for as long as it exists, and no later release could
  reach it.

`latest.json` is written but not uploaded. Publishing it beside the archive
at the endpoint's own URL is what makes a release reachable by installed
copies, and that is a separate command (see "Publishing"), run on purpose.

`RELEASE_NOTES_FILE` names a file whose text becomes the update's notes —
the prose a reader sees when Readily offers them the version. Each version's
notes live at `docs/release-notes/<version>.md`, written before the candidate
is built, so what the prompt shows and what the repository records are one
file rather than two that can disagree. Left unset the release carries no
notes and the prompt says what installing does instead, which is only right
for a candidate nobody is offered.

Three things the script will not check for you. It embeds whatever file you
name, so naming the previous version's notes ships the previous version's
notes. `UpdatePrompt` renders only the first 600 characters, so notes
longer than that reach a reader cut off mid-sentence — keep the file under
that, and let the download page carry the longer telling. And it makes a
paragraph of every line, so a note hard-wrapped the way the rest of this
repository wraps prose arrives broken into fragments, each ending wherever
the column ran out. Write each paragraph on one long line and separate
paragraphs with a blank one.

That last rule is not negotiable by fixing the renderer, because the
renderer that shows a version's notes is the one already installed on the
reader's Mac, not the one in the release being offered.

## Where a release lives

Two static Vercel projects in the maintainer's Vercel team, deployed from a
maintainer's Mac with the Vercel CLI; `scripts/publish-release.sh` reads the
team slug from `READILY_VERCEL_SCOPE`. Neither is linked to git: a git-linked project would put the
binaries in a public repository, and a push would redeploy the
repository root over a published release. Never delete or rename
`readily-updates`: its address is compiled into every installed copy, a
`*.vercel.app` name goes back to the pool for anyone to claim, and losing it
would strand every installed copy. A later holder could invent the version
and notes in the update prompt, though the signed archive's own version check
prevents it from downgrading the running build (threat model, B6).

| Project            | Serves                                                                                                              | Folder          |
| ------------------ | ------------------------------------------------------------------------------------------------------------------- | --------------- |
| `readily-download` | `https://readily-download.vercel.app`: the pages, the DMG, `SHA256SUMS`, and `THIRD-PARTY-NOTICES` renamed to `.txt` | `site/download` |
| `readily-updates`  | `https://readily-updates.vercel.app`: `latest.json`, the update archive and its signature                           | `site/updates`  |

Each folder's `vercel.json` says no framework and no build. The updates one
sends `Cache-Control: no-cache` with `latest.json`, which tells a browser or
a proxy between the reader and Vercel to ask again each time; Vercel's own
edge drops the old file when a new deployment goes up. Each folder tracks
its `vercel.json` and `.gitignore`, and the download folder its two pages;
nothing else, so a stray `candidate.json` cannot land in the repository. The
CLI uploads ignored files all the same, so the binaries deploy from a folder
that never commits them.

The link between a folder and its project is `.vercel/project.json`, which is
untracked, so a fresh clone has none. The publish script relinks both folders
itself on every run and needs no setup step. The Hobby plan caps one
deployment at 100 MB, and the DMG measures about 87 MB, which is why the page
and the update archive are two projects rather than one.

## One-time setup on the release Mac

Everything lives in the login keychain and `~/.private_keys`. CI holds no
signing secrets; releases are built and signed on a maintainer's Mac.

1. **Apple Developer Program** membership on the account that owns the app.
2. **Developer ID Application certificate.** Create a signing request in
   Keychain Access (Certificate Assistant → Request a Certificate From a
   Certificate Authority, saved to disk), upload it at
   developer.apple.com → Certificates → Developer ID Application, download
   the `.cer` and double-click it. `security find-identity -v -p codesigning`
   must then list `Developer ID Application: <name> (<team>)`. Export the
   identity as a `.p12` into a password manager; losing the private key means
   a new certificate. The `.cer` and the request can be deleted.
3. **App Store Connect API key** for notarization. At
   appstoreconnect.apple.com → Users and Access → Integrations → App Store
   Connect API, generate a Team key with the Developer role. Download the
   `.p8` (offered once) to `~/.private_keys/AuthKey_<KEYID>.p8`, mode 600.
4. **Updater signing key.** `bun tauri signer generate -w <path>` mints a
   minisign keypair; the public half goes in `plugins.updater.pubkey` in
   `src-tauri/tauri.conf.json` and ships in every build. Put the private key
   **and its password** in the password manager and nowhere else — not in the
   repo, not in CI. Losing them ends over-the-air updates for every copy
   already installed, because the public key is baked into those bundles: a
   new key reaches a reader only as a fresh download.
5. `.env.release` at the repo root (gitignored) with the two Apple ids the
   Tauri bundler reads and the updater key the bundler signs with:

   ```sh
   APPLE_API_KEY=<KEYID>
   APPLE_API_ISSUER=<ISSUER>
   TAURI_SIGNING_PRIVATE_KEY=<path to the minisign key, or its contents>
   TAURI_SIGNING_PRIVATE_KEY_PASSWORD=<that key's password>
   ```

The signing identity is resolved from the keychain at build time; it is not
written anywhere in the repository, because the certificate's subject is a
legal name.

## Setting the version

```sh
scripts/bump-version.sh 0.1.1
```

The version is written in four places — `package.json`,
`src-tauri/tauri.conf.json`, `src-tauri/Cargo.toml` and
`engine/pyproject.toml` — and the two lock files repeat it. The script
rewrites all of them and prints the diff; commit that on its own. It refuses
a version that is not newer than the newest one already written, and a tree
with uncommitted tracked changes, and a lock file that came back from `cargo
update` or `uv lock` changed in any line other than a version line —
dependency drift is its own commit, so lock it first and bump after. It puts
every file back if anything fails part way. `bun run verify` fails when the
six disagree, so a hand edit to one of them cannot reach a candidate.

## Building

```sh
RELEASE_NOTES_FILE=docs/release-notes/0.1.1.md scripts/release-candidate.sh
```

Twenty minutes or so from a cold checkout, most of it the Rust build. With
the dependencies already compiled it takes about two minutes. Both
notarization round trips wait on Apple's queue, which has no stated
turnaround, so neither figure is a promise. On success it prints
`candidate.json`. Any failed check stops the script: a signature that does
not hold, an executable without the hardened runtime, a notarization that
was not accepted, or a Gatekeeper assessment that would not pass on a
reader's Mac.

A plain `bun tauri build` without `APPLE_SIGNING_IDENTITY` in the
environment stays what it was: an unsigned local build, `uv` left as
published. It writes an update archive but signs nothing, and an unsigned
archive is one no installed Readily accepts — which is why the release script
demands the key up front rather than discovering it missing at the end of
the build.

## Publishing

```sh
scripts/publish-release.sh 0.1.1
```

Reads `dist-release/0.1.1/`, clears each folder down to its tracked files,
and puts the release on the two projects: the DMG, `SHA256SUMS` and
`THIRD-PARTY-NOTICES.txt` on the download project, with the landing page's
version and size and the download page's DMG link rewritten in place, and
`latest.json`, the archive and its signature on the updates project. Then it
fetches what went live and checks it: the DMG and the archive against the
checksums in `candidate.json`, `SHA256SUMS`, `latest.json`, the signature
and the notices against the copies it staged, the landing page for this
version and the download page for this DMG. Only then does it print the
site URL and the endpoint. The download project deploys before the updates
project, so a failure after the first deploy leaves new pages live with no
matching endpoint; `bunx vercel rollback` on `readily-download` puts the
previous pages back. Running it twice for one
version deploys the same bytes twice. It needs `minisign`
(`brew install minisign`) and a Vercel CLI login on the team; no secret
leaves the password manager for this step.

The pages are tracked, and the script has just rewritten them. Commit that
change on its own, the way a version bump is committed: the next candidate
build refuses a dirty tree.

Before it touches anything, it refuses to deploy when

- the candidate's commit is not on `main`, because a release is a build of
  `main`;
- in production, the version is older than the one `latest.json` already
  offers, since a stale folder under `dist-release/` is still a build of
  `main`; the same version again is allowed only with the same archive
  and DMG, since a rebuild of a published version would give new
  downloads different bytes under a name installed copies already have;
- a checksum in `candidate.json` disagrees with the file beside it, or
  `SHA256SUMS` does not name exactly those two files at those hashes;
- the DMG or the archive is over the 100 MB Vercel would refuse at upload;
- the candidate was built with an endpoint other than the updates project's
  `latest.json`, or `latest.json` names a version, archive or host other
  than this candidate's, or its signature does not verify against the public key in
  `src-tauri/tauri.conf.json` at the candidate's commit. Installed copies
  would fetch that file and refuse it, or fetch nothing.

It then clears both folders down to their tracked files, and refuses when

- anything tracked under `site/` has uncommitted changes, since the folders
  deploy as the checkout has them, the script rewrites the pages and a
  rehearsal restores them. An untracked file in either folder is not refused:
  the clearing deleted it before the check ran;
- in production, `HEAD` is not `origin/main` or a tracked file is modified,
  since the pages, the Vercel configuration and the script itself come from
  the checkout and a release is published by the reviewed `main`;
- either folder as a whole is over 100 MB. This one runs after staging, so
  a run refused here leaves the candidate staged and, in production, the
  pages rewritten; `git checkout -- site/download` puts them back.

A run turned away by the first two leaves the staging folders cleared.
Either way nothing published changes — the folders are local, and the next
run stages the candidate again.

`--preview` deploys the same staging to preview URLs no reader or installed
copy reads. That is how the script is rehearsed: a `dist-release/0.0.0-test/`
with a 1 MB dummy DMG, an archive signed with a throwaway minisign key and a
`candidate.json` pointing at them, with the throwaway public key swapped
into `tauri.conf.json` for the run and put back with `git checkout` after.
Only a preview reads that working copy of the key; a production run reads
the key at the candidate's commit, so a rehearsal that was never put back
cannot publish against the wrong key. In preview mode the script puts the
pages back itself. Previews sit behind Vercel Authentication, so the script
reads them through the CLI's `curl` rather than plain `curl`. Remove them
afterwards with `bunx vercel remove`.

The script never makes a tag, a GitHub release or a message. Those come
after a second Mac has accepted the published build.

## Website analytics

Open the `readily-download` project's Analytics dashboard in Vercel.
In the Pages panel, `/` measures visits to the landing page and
`/download.html` measures visits to the page that starts the DMG download.
The overall page-view total includes both pages. Download-page views are
download attempts, not completed transfers or installs: reloads can count
again, direct DMG links skip the page, and blocked analytics are not counted.

Both pages use Vercel's cookie-free Web Analytics script. Enable Web Analytics
in the `readily-download` project before deploying; the current setup uses
the Hobby plan without custom events or paid add-ons. Collection starts with
the analytics deployment and does not reconstruct earlier visits. The Mac
app and update endpoint do not send analytics.

## When notarization is rejected

`xcrun notarytool log <submission-id> --key ~/.private_keys/AuthKey_<KEYID>.p8
--key-id <KEYID> --issuer <ISSUER>` prints the per-file reasons. The usual ones are an executable that is not signed with
the Developer ID, missing the hardened runtime, or missing a secure
timestamp. Add an entitlement only when a notarized build fails at runtime
and the failure names it.
