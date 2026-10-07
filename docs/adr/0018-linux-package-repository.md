# Linux updates come from an apt and dnf repository, with the packages on GitHub

Status: accepted (2026-10-05) · Builds on [ADR 0016](0016-linux-packages-and-numpy-libquadmath.md)

ADR 0016 made Linux a `.deb` and an `.rpm` and left their updates to the
package manager. A package manager updates from a repository, and Readily
had none, so a Linux reader installed each release by hand from a file,
and the shell could tell them of a new version only once `latest.json`
named Linux, which it did not. Three things had to be settled: where the
packages live, how a reader's package manager finds the next one, and
what is signed with which key.

## The decisions

1. **The packages live on GitHub Releases of the public mirror**, as
   assets of the release tagged `v<version>` on `fahim-m47/readily-public`.
   The download site is a Vercel Hobby project with a 100 MB cap per
   deployment, and the DMG already fills most of it, so a `.deb` and an
   `.rpm` near 100 MB each beside it is not an option there, and a second
   Vercel project per platform would not scale either. (*Amended
   2026-10-07*: at 0.2.0 the DMG and the update archive passed 100 MB
   themselves and joined the packages on the release; Vercel now serves
   pages and metadata alone.) GitHub serves release
   assets at no cost, without a size cap that matters, and the mirror
   already carries the tag of every release. Readers who want a file
   rather than a repository can fetch one from the same page.

2. **Readers install and update through an apt and a dnf repository**,
   not a Flatpak. A Flatpak would carry its own WebKitGTK, GTK and
   GStreamer in its runtime, which is the AppImage's problem (ADR 0016)
   moved into a sandbox, and it needs an audio and a portal story Readily
   has not written. A repository is what a `.deb` and an `.rpm` are
   made for: the reader adds it once, and `apt upgrade` or `dnf upgrade`
   brings Readily along with everything else. The in-app check stays
   what ADR 0016 amended it to: an announcement with a link, never an
   install.

3. **The repository metadata lives on the download site, the packages
   do not.** apt's `Packages` and `Release` and dnf's `repodata/` are a
   few kilobytes, served under `/linux/` on `readily-download` with the
   same `no-cache` header as `latest.json`. They name each package by an
   address under `/linux/pool/v<version>/`, and the site redirects that
   path to the asset on the GitHub release. apt and dnf follow the
   redirect; both verify the package against the hash in the signed
   index, so where the bytes come from does not matter to their check.
   The apt repository is a flat one (`deb <site> linux/`): one
   architecture, one package, no suite or component to name. Vercel
   does not normalize a `./` in a path, so the distribution is `linux/`
   and the index's `Filename` is written from the site root.

4. **The indexes are signed with an OpenPGP key that lives on the
   release Mac; the packages are not signed.** A reader's apt and dnf
   trust one key, committed as `site/download/linux/readily.asc` and
   served from the site, and `linux-candidate.sh` signs with that key and
   no other, the way the minisign key signs the Mac update archive. The
   packages are built on the Linux machine, where no key is allowed: an
   unsigned package is what CI builds too, and apt and dnf check each
   package against the SHA-256 in the index they verified. Signing the
   packages as well would put the key on the Linux box, or the packages
   back through the Mac to be signed with `dpkg-sig` and `rpmsign` ported
   there, for a check readers already get from the index. The `.repo`
   file says so: `repo_gpgcheck=1`, `gpgcheck=0`.

5. **A release is both halves or neither.** `latest.json` carries one
   version for every target, so once it names Linux, a Mac-only release
   would tell every Linux copy of a version the repository does not
   have. `publish-release.sh` refuses a production run without
   `candidate-linux.json` built from the same commit, and the GitHub
   release must exist before it runs, since the metadata it publishes
   points at it.

## Consequences

- A Linux reader adds two lines once (the Linux page has them) and
  updates with the system. The shell's announcement at launch says a
  version is out and leaves the install to the package manager.
- The release has a Linux machine in it, as `verify:linux` already does,
  and a new key in one-time setup. The Linux build goes through
  `scripts/linux-candidate.sh`; `createrepo_c` runs through `uvx` at a
  pinned version so the box needs nothing installed as root.
- The order of a release changes: export to the mirror, tag, make the
  GitHub release, then publish. Before this the tag and release came
  after a second Mac had accepted the build.
- Losing the package signing key means a new one every Linux reader
  imports by hand; it cannot end updates the way losing the minisign key
  would, since the key is not compiled into anything. A stolen one can
  sign an index naming any package, until the committed key is replaced.
- Older Readily metadata is not kept: each publish replaces `/linux/`
  with one version's indexes, as it replaces the DMG. A reader's package
  manager keeps the installed package and sees the newest one only.
- Rebuilding a published version is refused on the GitHub side as on
  the Vercel side: an asset already on the release must be these bytes.
- Intel Macs remain the one unpublished target.
