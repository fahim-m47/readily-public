# Linux ships as packages on the system's libraries, and numpy's libquadmath is admitted

Status: accepted (2026-10-02) · Builds on [ADR 0006](0006-numpy-for-audio-analysis.md) and [ADR 0007](0007-license-policy-exceptions-engine-tree.md)

Readily's licence policy is permissive-only for anything linked or imported
into a shipped process, and copyleft is denied by default. Two things in the
Linux build broke that rule without anyone deciding to allow them.

The first was the AppImage. An AppImage carries its own copy of every
library the app links, taken from the machine that built it. One built on
Fedora 44 carried 174 libraries: 92 under LGPL, including WebKitGTK, GTK,
GLib, GStreamer, libsoup and GnuTLS, and two under GPL, which were
PackageKit's GTK module and mpg123. The bundler had pulled GTK modules in
from the build machine.

The second is numpy. Its Linux wheel puts three shared libraries in
`numpy.libs/`:

- OpenBLAS, under BSD-3-Clause.
- `libgfortran`, under GPL-3.0-or-later WITH GCC-exception-3.1, which
  CONTRIBUTING already allows as a runtime under an exception.
- `libquadmath`, under LGPL-2.1-or-later. `libgfortran` links it.

The pip-licenses gate reads numpy's declared licence and nothing inside the
wheel, so it never saw `libquadmath`. On macOS 14 or newer, the wheels uv
installs for both architectures link Accelerate and bundle nothing.

## The decisions

1. **Linux ships as a `.deb` and an `.rpm`, and never as an AppImage.**
   Both packages declare dependencies on the distribution's WebKitGTK, GTK,
   GStreamer and PortAudio. The reader's package manager installs those from
   the distribution, which is also where their source and licences come
   from. A Readily package holds only Readily's own binary, the bundled `uv`,
   the Engine sources and the Catalog, so Readily distributes no copyleft
   library.

   Bundling the AppImage's libraries would have meant, for every release:
   - publishing each one's licence and corresponding source;
   - keeping each one replaceable;
   - dropping the GPL modules, which the policy refuses outright.

   For a desktop app, a package that uses the system's libraries is the
   ordinary choice, not a workaround.

2. **LGPL-2.1-or-later is admitted for exactly `libquadmath` as numpy's
   wheel bundles it.** The ruling covers that one library in that one
   package. It does not admit LGPL in general, and it does not cover any
   other wheel's bundled libraries.

   Readily never distributes the library. `uv` downloads the wheel from PyPI
   on the reader's machine at first run, pinned by hash in `engine/uv.lock`.
   It stays an unmodified shared object in its own file, loaded dynamically,
   so the reader can always replace it. Its licence text is already in
   `THIRD-PARTY-NOTICES`.

   The alternatives cost more than the obligation they avoid:
   - building numpy from source without OpenBLAS's Fortran runtime;
   - giving up numpy, which ADR 0006 chose so the Engine doesn't carry its
     own FFT code.

   This doesn't reopen ADR 0006's refusal of SciPy or ADR 0007's refusal of
   num2words, though the same download argument would apply to both. Each
   had a small permissive replacement, a WAV reader and a stand-in module,
   so the policy cost nothing to keep. Nothing small replaces numpy.

## Consequences

- **Linux gets no in-app updates.** Tauri's updater would reinstall a `.deb`
  or an `.rpm` as root, asking for the reader's password through `pkexec` or
  `sudo`, and a package belongs to its package manager. So the shell never
  checks for an update on Linux, the Linux build makes no update artifact,
  and `latest.json` names no Linux target.
  Readers install each release by hand until Readily publishes an apt and
  dnf repository or a Flatpak. Either one keeps the system libraries out of
  what Readily distributes. *Amended 2026-10-05* for the update notice: the shell
  now checks on Linux too, and announces a newer release with a link to the
  download page. It still installs nothing, and it hears of a release only
  once `latest.json` names a Linux target. *Amended again the same day*:
  `latest.json` names `linux-x86_64`, and the packages come from an apt
  and dnf repository ([ADR 0018](0018-linux-package-repository.md)).
- Two Linux release blockers go away with the AppImage:
  - **The downgrade gap in threat model B6.** An AppImage's signed bytes
    carry no version, so a host could have offered an older signed build.
    With no Linux updates, there is nothing to downgrade.
  - **Provisioning on every launch.** An AppImage mounts at a new path each
    time it starts, and the Engine is installed editable from that path, so
    uv provisioned it again on every launch. A package installs to the same
    path every time.
- A package runs on distributions about as new as the one that built it, and
  only on those whose package names match: the `.deb` targets Debian and
  Ubuntu, and the `.rpm` targets Fedora and its relatives.
- No gate looks inside a wheel. Another dependency whose wheel bundles a
  copyleft library would pass the pip-licenses gate unnoticed, just as
  `libquadmath` did. Adding a Python dependency still means listing what its
  Linux and macOS wheels carry, and a new bundled licence needs a ruling
  like this one.
