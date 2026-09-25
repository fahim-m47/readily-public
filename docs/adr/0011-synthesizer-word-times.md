# Read word times from the synthesizer first

The word-coordinate channel is filled from the durations Kokoro and Kitten
already predict. This implements ADR 0002 §7's preference
for synthesizer timings. An aligner is the fallback for entries that cannot
report them; interpolation remains the fallback when neither supplies a
complete mapping. ADR 0004's storage decision stays unchanged.

The Catalog declares `word_timing_languages`, independent of Tier and
Backend. It names base language codes: Kokoro and Kitten report English;
Supertonic, Qwen and Chatterbox declare none. The worker checks the selected
Voice's language. This capability stays off the wire and is also the input
for the aligner download planner. Entries without native
times can declare a forced-aligner Support Model; native mapping failures
still fall back to estimated times without invoking it.

Kitten exposes `duration` as output 1. Kokoro's pinned export contains
`/encoder/Gather_output_0`, the integer duration vector after rounding and
clamping, but does not list it as an output. The Manifest declares a
`derived_files` entry for Kokoro, and the store writes
`kokoro-v1.0.timed.onnx` beside the pinned export at install: the pinned
bytes followed by a hand-encoded protobuf tail that appends the tensor to
the graph's outputs. The Kokoro lane loads the derived file. The pinned
file, its Manifest hash and the download do not change; an installed Kokoro
gains the derived file at the next Engine start, when `repair` writes it
the way it backfills licence files. Disk use for Kokoro doubles.

Patching the graph in memory at load was rejected after measurement.
ONNX Runtime given serialised bytes held about 1.35 GB more resident memory
for Kokoro than ONNX Runtime given the same graph's file path, whatever
produced the bytes, which would have put the entry at about four times its
`ram_class_gb`. Loading the derived file from disk measures the same as
loading the pinned file.

Both pinned exports produce **600 samples per duration unit at 24 kHz**,
verified against their output waveforms. That is 40 units per second, not
80. The number of duration entries equals the unpadded id count plus two;
their sum times 600 equals the waveform's sample count.

One pure mapper walks Misaki tokens against Block text with a forward
cursor, including token whitespace. It replays vocabulary drops and Kitten's
respelling, and checks the complete id sequence as well as its length.
Expanded numbers stay attached to the original Source word. Ambiguous
multiword tokens, missing words, invalid durations, or any mismatch reject
the whole Block's mapping. The worker logs the Narration id and Block ordinal
and leaves interpolation to supply estimated positions. It never logs Source
text. Word spans overlapping retained audio are clipped at the trim edges;
metadata outside the raw audio's bounds remains invalid.

The Engine takes no dependency on the `onnx` package. The derivation
appends three nested length-delimited fields, and ONNX Runtime refuses at
load a derived file naming an output no node produces.

Existing cached Segments without timings keep estimated positions until
regenerated. Native timings do not change a Generation Record or its cache
key. Choosing an aligner does: `word_timing` is part of the Record, so a
Segment generated under one aligner choice is not reused under another.
The reproducible qualification command is documented in `engine/tools/README.md`.
