# Ear-calibrated audio fixtures

These files are byte-for-byte copies of ear-labeled runs from the listening
sessions that calibrated the accept gates. Do not regenerate them: the
stochastic Qwen samples are the captured evidence for the high-frequency gate.

- `qualification/039-kokoro_82m/`: a full run whose raw third Segment has two
  impulse-click clusters (five discontinuous sample steps); the processed
  Segment and device tap are clean.
- `qualification/024-qwen3-tts_0.6b/`: a full run whose raw Segment has one
  ear-confirmed in-pause crackle event; the processed Segment and tap are
  clean.
- `hf8k/016-degenerate.wav`, `017-degenerate.wav`, `018-clean.wav`: two
  consecutive Qwen generations over the same 89-character text rejected at
  0.2260 and 0.0602 high-frequency energy, followed by the clean 0.0083 draw.

The full qualification runs retain their original manifests, callback logs,
raw/processed Segment pairs, and taps so checks A-F exercise captured evidence
rather than synthesized test signals.

## Source hashes

```text
904f8e72dd90ac4eb9c4b50b3ed980795d0e0fd1b7d18d9fc553b8e106008330  hf8k/016-degenerate.wav
a2c05f98e7fcdd35be1ee81565851b8c3101eb0b434d09caa1b07b28655b046a  hf8k/017-degenerate.wav
78fecf69ce64d6305d231731a61553b35f112a43e5770ff3acf8696a381817d8  hf8k/018-clean.wav
```

The files inside each qualification directory retain the source run's names;
their provenance is the corresponding numbered local run.
