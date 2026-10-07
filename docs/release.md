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
  latest.json                             what the update endpoint serves; names the archive on the GitHub release
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

`latest.json` is written but not uploaded. It names the archive at its place
on the mirror's GitHub release `v<version>`, and publishing it at the
endpoint's own URL, with the archive on that release, is what makes a release
reachable by installed copies: a separate command (see "Publishing"), run on
purpose.

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
| `readily-download` | `https://readily-download.vercel.app`: the pages (the download page linking the DMG on the release), `SHA256SUMS`, `THIRD-PARTY-NOTICES` renamed to `.txt`, and under `/linux/` the Linux page, the package signing key and the apt and dnf metadata | `site/download` |
| `readily-updates`  | `https://readily-updates.vercel.app`: `latest.json` and the update archive's signature                              | `site/updates`  |
| GitHub release `v<version>` on [`fahim-m47/readily-public`](https://github.com/fahim-m47/readily-public/releases) | every binary: the DMG, the update archive, the `.deb` and the `.rpm`. The Hobby plan caps a deployment at 100 MB, and each of them is at or past that | — |

Each folder's `vercel.json` says no framework and no build. The updates one
sends `Cache-Control: no-cache` with `latest.json`, which tells a browser or
a proxy between the reader and Vercel to ask again each time; Vercel's own
edge drops the old file when a new deployment goes up. The download one
does the same for the apt and dnf index files under `/linux/`, and
redirects `/linux/pool/<tag>/<file>` to the asset of that name on the
mirror's GitHub release `<tag>`: the signed indexes name the packages by
that path, apt and dnf follow the redirect, and the packages themselves
never pass through Vercel. The DMG and the update archive go to the same
release, linked by their GitHub addresses outright: the download page's
link and `latest.json`'s `url` both name
`https://github.com/fahim-m47/readily-public/releases/download/v<version>/<file>`,
and GitHub redirects each fetch to the bytes. Each folder tracks its `vercel.json` and
`.gitignore`, and the download folder its pages, `linux/readily.repo` and
`linux/readily.asc`; nothing else, so a stray `candidate.json` cannot land
in the repository. The CLI uploads ignored files all the same, so the
binaries deploy from a folder that never commits them.

The link between a folder and its project is `.vercel/project.json`, which is
untracked, so a fresh clone has none. The publish script relinks both folders
itself on every run and needs no setup step. The Hobby plan caps one
deployment at 100 MB; the DMG and the archive were about 87 MB each until
0.2.0 put both past it, which is when they moved to the release and the
deployments shrank to pages and metadata. The two projects stay two: the
updates address is compiled into every installed copy.

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

6. **Linux package signing key.** An OpenPGP key in this Mac's GnuPG
   keyring (`brew install gnupg`), signing only, never expiring, with no
   name on it but the product's:

   ```sh
   gpg --quick-generate-key "Readily packages" ed25519 sign never
   gpg --armor --export "Readily packages" > site/download/linux/readily.asc
   ```

   Commit `readily.asc`: it is the key every Linux reader's apt and dnf
   trust, served at `/linux/readily.asc`, and `linux-candidate.sh` signs
   with the key it names and no other. Export the secret half
   (`gpg --armor --export-secret-keys "Readily packages"`) into the
   password manager and nowhere else. Losing it means a new key, which
   every Linux reader imports by hand before their next update; replacing
   the committed key is a release in itself, and a stolen one lets its
   holder sign a repository index that names any package.

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

The candidate is an Apple Silicon build, and `latest.json` names
`darwin-aarch64` alone. An Intel Mac build (`bun tauri build --target
x86_64-apple-darwin`, which bundles the Intel `uv`) is not released yet.
Now that binaries live on the release, room is no longer the question;
what remains is a second artifact to notarize and a second target for both
scripts to learn.

It is a second `.app` rather than one universal build. A universal `.app`
would carry both architectures' Readily and `uv`, fused with `lipo`, in the
one image every Apple Silicon reader downloads, doubling it. Two builds
keep each reader's download what it was, at the cost of a second artifact
to notarize, and `latest.json` names each under its own target.

### The Linux half

```sh
scripts/linux-candidate.sh
```

Run at the same commit as `release-candidate.sh`, on the same Mac, with
`READILY_LINUX_HOST` pointing at the Linux machine that `bun run
verify:linux` uses ([CONTRIBUTING.md](../CONTRIBUTING.md#setting-up-the-linux-machine)
lists what it needs; a release build also takes `ffmpeg`, `rpm2cpio`,
`cpio` and `uv`). The script copies the committed tree there with `rsync`
into its own checkout (`READILY_LINUX_RELEASE_DIR`, by default
`readily-release`), runs `bun tauri build --no-sign --bundles deb,rpm`, and
opens both packages to run `scripts/check-bundle.sh` over what they
install, as `smoke-linux` does in CI. Then it writes the two repositories
readers' package managers read: for apt a `Packages` index describing the
`.deb` and a `Release` file naming the index's hashes, written by the
script; for dnf a `repodata/` folder written by `createrepo_c`, fetched by
`uvx` at a pinned version since Fedora's own package needs root to
install. Both name the package by its address under
`/linux/pool/v<version>/` on the download site, so the metadata is signed
over the address readers will fetch from. The lot comes back to
`dist-release/<version>/linux/`, where the Mac signs the apt `Release`
(as `InRelease` and `Release.gpg`) and the dnf `repomd.xml` with the key
from one-time setup, verifies each with `gpgv` against the committed
public key alone, and writes `candidate-linux.json` with the commit, the
packages' checksums and the key's fingerprint.

The packages are not signed, only the indexes. apt and dnf verify the
index's signature, then each package against the hash the index carries,
so an altered package is refused all the same; signing the packages too
would mean `rpmsign` and `dpkg-sig` on the Linux machine, with the key on
it ([ADR 0018](adr/0018-linux-package-repository.md)). The Linux build is
`--no-sign` as CI builds it: there is no update archive for the minisign
key to sign, since a Linux copy updates through its package manager and
never through the updater ([ADR 0016](adr/0016-linux-packages-and-numpy-libquadmath.md)).
`latest.json` still names a `linux-x86_64` target, with an empty signature
and the Linux page's address as its `url`, which is how an installed Linux
copy learns a new version exists; the shell reads the version and never
fetches the `url`. `latest.json` carries one version for every target, so
a release is both halves or neither: `publish-release.sh` refuses a
production run without the Linux candidate.

An Intel Mac is the one target still unpublished.

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

Before it runs, the GitHub release the binaries go on has to exist:
export the commit to the mirror with `scripts/public-export.sh`, tag it
`v0.1.1` there, and make a release from that tag (`gh release create
v0.1.1 -R fahim-m47/readily-public --notes-file
docs/release-notes/0.1.1.md`), not a draft. The script refuses without it,
since everything it publishes points at that release.

Reads `dist-release/0.1.1/`, clears each folder down to its tracked files,
and puts the release in the three places: the DMG, the update archive, the
`.deb` and the `.rpm` on the GitHub release, as assets; `SHA256SUMS`,
`THIRD-PARTY-NOTICES.txt` and the apt and dnf metadata under `linux/` on
the download project, with the landing page's version and size and the
download page's DMG link (the asset's address) rewritten in place; and
`latest.json` and the archive's signature on the updates project. Then it
fetches what went live and checks it: the DMG and the archive at their
addresses on the release against the checksums in `candidate.json`, both
packages through the pool redirect against the checksums in
`candidate-linux.json`, `SHA256SUMS`, `latest.json`, the signature, the
notices and every Linux metadata file against the copies it staged, the
landing page for this version and the download page for this DMG. Only
then does it print the site URL, the endpoint and the release. The
binaries upload first, the download project deploys next and the updates
project last, so a failure in between leaves new pages live with no
matching endpoint; `bunx vercel rollback` on `readily-download` puts the
previous pages back, and an asset already on the release is left there.
Running it twice for one version deploys the same bytes twice, and
uploads nothing the second time. It needs `minisign` (`brew install
minisign`), `gpgv` (`brew install gnupg`), `gh` logged in as someone who
can write the mirror's releases, and a Vercel CLI login on the team; no
secret leaves the password manager for this step.

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
- the candidate was built with an endpoint other than the updates project's
  `latest.json`, or `latest.json` names a version or archive other than
  this candidate's or an address other than the archive's on the release,
  or its signature does not verify against the public key in
  `src-tauri/tauri.conf.json` at the candidate's commit. Installed copies
  would fetch that file and refuse it, or fetch nothing;
- `latest.json` names any targets but `darwin-aarch64` and `linux-x86_64`,
  or the Linux entry carries a signature or points anywhere but the Linux
  page, since the shell on Linux installs nothing from it;
- there is no `candidate-linux.json`, or it names another commit than
  `candidate.json`, since `latest.json` tells Linux copies of every
  version and a release is one commit;
- a checksum in `candidate-linux.json` disagrees with the package beside
  it, an index does not describe that package at that checksum under the
  pool path, or a signature does not verify with `gpgv` against
  `site/download/linux/readily.asc` at the candidate's commit. Readers'
  package managers would refuse the whole repository;
- in production, the mirror has no GitHub release `v<version>`, or it is a
  draft, or it already carries an asset of this name with other bytes.

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
cannot publish against the wrong key. The same goes for the Linux half: a
preview without a `linux/` folder in the candidate rehearses the Mac half
alone and says so, and one with it (a real `linux-candidate.sh` run signed
with a throwaway OpenPGP key swapped into `readily.asc` the same way)
checks the Linux half and stages its metadata. A preview uploads nothing
to GitHub and fetches no binary; the pages it deploys link assets that are
not there. In preview mode the script puts the pages back itself. Previews sit behind Vercel Authentication, so the script
reads them through the CLI's `curl` rather than plain `curl`. Remove them
afterwards with `bunx vercel remove`.

The script never makes a tag, a GitHub release or a message. The tag and
the release come first, on the mirror, so the binaries have somewhere to
go; the message comes after a second Mac has accepted the
published build.

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
