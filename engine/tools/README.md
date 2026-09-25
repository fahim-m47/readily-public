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
