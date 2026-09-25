# Forced alignment for entries without word times

The wav2vec2 Base 960h ONNX aligner is a Catalog-managed Support Model. Qwen, Chatterbox and Supertonic can choose it. Kokoro
English and Kitten use their own durations and never download or load it.
The routing capability is `word_timing_languages`.
A failed native mapping remains estimated rather than running the aligner.

The per-entry `word_timing` Control uses the existing choice schema. `off`
disables alignment; `wav2vec2:base-960h` enables the selected Support Model.
`off` is the default on the three entries that can use it, so an upgrade
neither changes their cache keys nor makes an installed Voice Model wait on
a Support Model download. The Manifest rejects choices outside its Support
Models. Advanced mode exposes this Control; Simple mode has no
timing setting. Support licences remain readable beside the Voice Model
licence.

The download planner reads each Voice's effective controls and capability.
If every eligible Voice has timing off, no Support Model is required. A
shared installation is reused and its disk usage counted once. Deleting a
Voice Model leaves shared support installed. The timing choice is frozen in
the Generation Record before work begins. Changing it changes new Segment
keys; existing audio and records remain readable, and records without a
timing choice retain their original keys.

The graph, vocabulary, preprocessing configuration and model card are pinned
under `support_models` in `catalog/manifest.json`. Curation, anonymous download,
SHA-256 verification, atomic promotion, startup repair and licence writing use
the same machinery as Voice Models. The loader reads only promoted files.
The Hub call explicitly disables credentials. The aligner adds no runtime
dependency or network call site. It touches trust boundaries B1 and B2,
plus the B3 server adapter for readiness and licence projection.

The aligner resamples to 16 kHz, obtains CTC log probabilities through ONNX
Runtime, and runs Viterbi in NumPy on a 20 ms grid. It aligns the known Block
text, without transcription or torch. Alignment runs on trimmed audio;
accepted coordinates are offset back into the untrimmed Segment before
storage. Accepted words carry `matched` provenance. The internal 0.8
threshold rejects uncertain words or an unexplained Segment. Unsupported
text, including digits and out-of-vocabulary accents, rejects the Segment.
Missing words are interpolated, and alignment errors do not discard
successful audio.
