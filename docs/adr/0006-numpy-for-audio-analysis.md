# NumPy for deterministic PCM analysis

Status: accepted (2026-08-26); the `libquadmath` its Linux wheel bundles is admitted by [ADR 0016](0016-linux-packages-and-numpy-libquadmath.md)

The graduated audio-artifact fixes and Voice Model qualification harness need
FFT energy, spectral flatness, frame RMS, and exact sample-level comparisons.
Implementing those numerical primitives locally would be substantial,
security-sensitive machinery with no Readily-specific value.

## Decision

Use NumPy as the Engine's one direct numerical dependency. The artifact
processors remain pure functions over float PCM, so neither model loading nor
audio-device code is needed in CI. NumPy and the libraries bundled in its macOS
wheel use permissive licenses (BSD-3-Clause, 0BSD, MIT, Zlib, and CC0-1.0); it
adds no copyleft package or network call site.

Do not add SciPy for WAV loading. Its distributed wheels may bundle GCC runtime
components under GPL-with-runtime-exception and LGPL terms, which conflicts
with Readily's stricter "nothing GPL/AGPL/LGPL linked or imported" policy. The
qualification harness instead carries the small RIFF/WAVE reader it needs.

## Consequences

- `numpy` is locked and shipped with the Engine, and remains subject to the
  Python production license gate. The gate admits NumPy's exact composite
  SPDX expression; it does not broaden acceptance to a copyleft family.
- Audio fixtures use mono IEEE-float or integer PCM WAV; admitting another
  encoding requires an explicit reader change.
- Future Backends call the pure declick, crackle, and hf8k gate functions;
  this decision does not admit a model-loading dependency.
