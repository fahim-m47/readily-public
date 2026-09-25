# Model weights carry their own licence policy: permissive only

Status: accepted (2026-09-01); §1 superseded by [ADR 0009](0009-model-weights-redistributable-licences.md), §2 and §3 stand · Builds on [ADR 0003](0003-baked-in-hash-pinned-catalog.md), [ADR 0007](0007-license-policy-exceptions-engine-tree.md)

The repo's licence policy is written for dependencies: nothing GPL/AGPL/LGPL
linked or imported into a shipped process, enforced by cargo-deny and the
`licenses` CI job. Model weights are not linked into any process, so they pass
every one of those gates without being looked at. A model card can release
its *sample code* under MIT and its *weights* under a licence the dependency
gates neither see nor would object to, such as BigScience OpenRAIL-M.

## The decisions

1. **A Catalog entry's weights must be under a permissive licence**:
   Apache-2.0, BSD-2-Clause, BSD-3-Clause, CC0-1.0, MIT. Not "OSI-approved",
   not "open-weights", not "commercial use allowed" — the same standard the
   dependency policy applies, applied to the one artifact that escapes it.
2. **The rule lives on the manifest schema, not in the curation script.** Both
   roads then meet it: the script refuses a draft before a byte downloads, and
   CI refuses a hand-edited entry when it parses the committed manifest.
   `test_every_committed_entry_ships_under_the_licence_it_was_curated_for` is the
   standing check.
3. **Additions to the list go through an ADR**, amending this one. Widening
   the literal in a curation PR is how the list stops meaning anything.
4. **`kitten-tts:15m` is the fast-end entry, not `supertonic:66m`.** Kitten
   TTS Nano 0.2 (15M parameters, Apache-2.0, ONNX) fits the list. The upstream
   package phonemizes through GPL `phonemizer`/espeak; this lane narrates with
   Misaki instead, as the Kokoro lane does (ADR 0007), so nothing copyleft is
   linked into a shipped process.

## Consequences

- A future entry under a RAIL, CC-BY-NC, or bespoke "open weights" licence is
  refused at the draft, with the list in the error message. *(Superseded by
  ADR 0009: RAIL is admitted with its obligations carried; CC-BY-NC stays
  refused.)*
- The Catalog's licence field is load-bearing, so curation may not copy it
  from a repo tag: it has to come from the weights' own licence file.
- Swapping the G2P swaps the alphabet. The Kitten export learned espeak-ng's
  spelling, and Misaki's normalized symbols (`A`, `I`, `O`, `W`, `Y`, `ʤ`,
  `ʧ`, `ᵊ`, `T`) all have valid ids in its table, so a wrong symbol narrates
  clean, wrong speech rather than failing. The loader maps the invertible
  spellings back (`ESPEAK_SPELLING` in `loading/styletts2/kitten.py`), and a
  test pins that mapping symbol by symbol. Misaki's many-to-one folds (length
  marks, `ɚ`, `ɜː`, `ɐ`, `ʔ`) are not recoverable and stay folded. A future
  G2P substitution needs the same tests: a wrong id is a valid id.
- The Kitten export ships three audio defects, fixed at the loader in this
  order: a positive DC drift in voiced speech (`remove_dc_drift`, a 100ms
  running mean), peak overshoot (a constant 0.8 gain, not per-draw
  normalisation), and crackle in its pauses (`scrub_noise_bursts`, after the
  gain, because `SILENCE_RMS` is absolute). The Assembler ramps the Segment
  edges it exposes by 3ms, because `trim_silence` cuts on 10ms frame
  boundaries and can leave a step against the surrounding silence.
- The export draws with unseeded `RandomNormalLike` and `RandomUniformLike`
  ops inside the pinned graph, so the same Block comes back different each
  time. Raw length is fixed; trimmed length varies per Block because the
  trailing decay sits near `SILENCE_RMS`. Seeding would change the pinned
  bytes (ADR 0003 §2), so it is not done. The manifest still regenerates
  byte-identically; Voice Previews are samples and are regenerated only when
  a re-pin or loader change moves the audio.
- Measured on an M-series Mac: 0.31GB resident after load, 0.52GB peak
  through a 470-char Block (the entry's chunk budget is 300), ~6× realtime.
  Against the 8GB floor that is a `ram_class_gb` of 0.5, the same class as
  Kokoro.
