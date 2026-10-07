# WAV Segments and WAV-only Export off macOS

Status: accepted (2026-09-26)

ADR 0004 §3 stores Segments as FLAC, and Export writes M4A by default. Both
encoders are `/usr/bin/afconvert`, which ships with macOS and exists nowhere
else. Intel Macs keep it; the Linux build needs another route.

The options were:

1. **Link a FLAC encoder.** pyFLAC (Apache-2.0, over BSD libFLAC) requires
   SoundFile, which loads libsndfile (LGPL). SoundFile itself is the same
   library. Both break the Engine's "nothing GPL/AGPL/LGPL linked or
   imported" policy (ADR 0006, ADR 0007).
2. **Find an MIT/BSD FLAC encoder.** None is maintained as a Python
   binding. miniaudio, already shipped for decoding, cannot encode FLAC.
   Spawning a system `flac` or `ffmpeg` would add an undeclared runtime
   dependency with GPL tooling. Writing a FLAC encoder ourselves would be
   bespoke codec code with no Readily-specific value.
3. **Store Segments as WAV off macOS**, at nearly three times the disk.

## Decision

Take option 3. `audio/encoding.py` is the one encoder seam. It names each
platform's Segment codec and Export encoders:

- **macOS** keeps FLAC Segments and M4A-by-default Export, both through
  afconvert, so nothing changes for an existing Mac library.
- **Everywhere else**, Segments are 24-bit WAV, written by the standard
  library and decoded by miniaudio. Export offers WAV only.

The Segment store keys files by content and names them by the platform's
suffix. A library is never carried between platforms, so the store never
mixes the two. The retention budget (ADR 0004 §5) bounds the extra disk the
same way it bounds FLAC: it counts bytes, not hours.

Export's default is the first format the platform offers. An Engine asked for
a format it cannot write refuses it with `422 invalid_request`. The UI never
names a format, so it offers no M4A to hide.

An AAC or FLAC encoder for Linux is a new dependency, and it must earn its
own licence review.

## Consequences

- No dependency is added, and the licence lanes see the same trees.
- A Linux library uses about 260MB per narrated hour (24-bit at 24kHz mono),
  instead of about 95MB.
- A Linux Export is lossless and large; there is no compressed option to hand
  someone.
- Tests that run on ubuntu exercise the real WAV Segment path. The FLAC and
  M4A paths stay under the macOS-only tests that call afconvert.
