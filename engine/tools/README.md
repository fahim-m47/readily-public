# Pronunciation export

This separate development project keeps Torch, ilt-deep-phonemizer and nltk
out of the Engine's production dependencies and `engine/uv.lock`.

From the repository root, with a local copy of
`NRC-CNRC/en_us_cmudict_ipa_forward_g2p`'s `en_us_cmudict_ipa_forward.pt`
at revision `05f9e2f78b12f4f8852e4f8b3f77dc7e01749293`:

```sh
uv run --project engine/tools --extra export \
  engine/tools/export_pronunciation.py /path/to/checkpoint.pt /tmp/pronunciation
```

The script checks the checkpoint's SHA-256 before deserializing it. It
exports a single ONNX file and a lexicon JSON, then checks the Engine's
tokenizer and decoder against Torch on 22 words. The last two output lines
give each asset's size and hash. To verify an existing export without
rewriting it, add `--check-only`.

The graph uses ONNX opset 18 with a dynamic text length of 3 through 256
tokens. Each recognized input character occupies three tokens, with one
language token and one end token. The longest supported word therefore
contains 84 recognized characters. Longer unknown words retain the previous
empty fallback behavior. Lexicon hits are not length-limited.

The script never downloads or publishes files. The app ships the two exports
under `engine/src/readily_engine/loading/data/pronunciation/`, alongside their
MIT licence and provenance. The existing Engine resource mapping includes
that directory in the app, and the Engine wheel includes it as package data.
No Hugging Face publishing access or runtime asset download is needed.

After an intentional re-export, update the pins in `loading/pronunciation.py`,
run its focused tests, and bump the affected Voice Models' Catalog versions
so Narrations do not reuse Segments generated with the previous fallback.

Torch export tools may change the serialized bytes even when parity passes.
Changes to the pinned output hashes require review and another parity run.

# Synthesizer word times

With Kokoro and Kitten already installed beneath the same model root, run:

```sh
uv run --project engine python engine/tools/word_timings.py /path/to/models
```

The command checks every model file against the Catalog pins, generates the
fixed 20-Block passage with both loaders, and requires spoken coordinates for
every word after the normal silence trimming. It prints per-Block coverage
and fails on any estimate. It downloads nothing and does not play audio.

# Voice Reference clips

The cloning Voice Models (Qwen3 TTS and VoxCPM2) share twelve consented
reference clips cut from two CC BY 4.0 corpora: VCTK 0.92 (CSTR, University
of Edinburgh) and Hi-Fi TTS (NVIDIA, via the `MikhailT/hifi-tts` parquet
mirror). `reference_voices.json` names each Voice's take. With ffmpeg on
`PATH`, from the repository root:

```sh
uv run --project engine/tools --extra clips \
  engine/tools/reference_clips.py engine/tools/reference_voices.json
```

The script fetches only the members it needs — VCTK through HTTP range
requests into the 11 GB datashare zip, Hi-Fi TTS by reading the split's
parquet shards in order until one holds the row, at the mirror commit the
script pins and checked against the digests that commit records — and
keeps them under `--cache` for the next run. `--only <Voice>` (repeatable) re-cuts one Voice.
Each take is decoded to 24 kHz mono through ffmpeg's long windowed-sinc
resampler, trimmed to speech at −45 dB with 80 ms of room either side,
joined with 0.45 s gaps when a take spans consecutive sentences,
peak-normalised to 0.35 (the level of the bundled Chelsie clip; louder
references drove the Qwen3 0.6B narrations into clipping), faded into 0.36 s
of silence and padded to a whole number of codec tokens like the bundled
Chelsie clip, and written as 16-bit WAV under
`catalog/references/<name>/<tag>/<Voice>.wav` for every cloning entry, so
the three entries share the bytes. It prints a score table (duration,
noise floor, SNR, whether the clip sits in the 7.5–10 s window) and, per
cloning entry, the draft Voice stanzas with each clip's path, transcript and
credit, ready to paste into that entry's draft for `readily-curate`, which
pins the clip hashes.

Walter scores "long" at 10.64 s: Hi-Fi TTS has no shorter clean sentence
from his reader, and the tail of silence adds the rest. Neither cloning
loader trims a reference, so the length is kept rather than the take.

A transcript with digits, brackets, quotes or colons is refused: pick a
different take rather than editing the corpus line.
