# Adding a Voice Model

A Voice Model ships when two things pass:

1. **Conformance.** Its Architecture's `TestConformance` passes: the one
   suite in `engine/tests/architectures/conformance.py` that says what every
   Architecture owes the Engine (ADR 0014 §4).
2. **Curation.** `readily-curate` accepts its entry. It narrates the
   standard passage in every Voice through the real Backend, and the checks
   A–F in `audio/qualification.py` pass over the capture. A capture feeds no
   audio device, so only the synthesis checks A, B, D and F have anything to
   measure. This is not a Voice's Qualification for Simple mode, which is
   checked separately with `readily-curate --check-simple`.

Those two runs are the whole test evidence a model PR needs. Anything else
in the PR tests only what is unique to that model.

## 1. The Architecture

Skip this step if the model is a new checkpoint of an Architecture the
registry already has. It then needs only a Catalog entry.

Otherwise, add a package under `engine/src/readily_engine/loading/<id>/` that
answers the `Architecture` interface in `loading/architecture.py`:

- its Backend: `onnxruntime`, or `mlx-audio`, which Intel Macs and Linux lack;
- every file loading an entry reads, including sidecars a library reads for it;
- whether a Voice conditions on a preset or a Voice Reference;
- its warm-up text;
- its parameter schema;
- the Block budgets `--chunk-budget` may sweep;
- `load`.

A StyleTTS2 export (Kokoro and Kitten are two) needs no package: add a
spec file under `loading/styletts2/` that builds a `Spec` — graph and
voices file names, the graph's tokens input, the symbol table, the phoneme
limit, the style archive's shape, and the dialect, respelling and repair
functions — and set `ARCHITECTURE = StyleTTS2Architecture(SPEC)`. A
`Spec` holds data and one-argument functions; a spec that wants a method
is a new Architecture.

Then register it with one line in `loading/registry.py`. Anything shared
stays shared: the ONNX session options live in `loading/onnx.py`, and the
MLX lane with its gate and runaway cutoff lives in `loading/mlx_lane.py`.

Read the licence off the weights, and read every new dependency's
transitive licences, before any of this: no GPL, AGPL or LGPL, and the
Catalog's licence allowlist refuses a draft whose weights fail it. Loading
never goes to the network. Only the download module may.

## 2. The Catalog entry

Write a draft entry and curate it (`engine/README.md` §"Curate a Voice
Model" covers drafts, Simple recipes and the ways a healthy model is still
refused):

```sh
cd engine
uv run readily-curate ~/new-model.json          # check, print the entry
uv run readily-curate ~/new-model.json --write  # and merge it into the Catalog
```

A model that fails the checks is refused, and nothing is written. Keep the
curation output for the PR.

## 3. Conformance

Add `engine/tests/architectures/test_<id>.py`, and in it:

```python
class TestConformance(Conformance):
    model_id = "new-model:small"  # the committed entry from step 2
    sample_rate = NEW_MODEL_SAMPLE_RATE  # None: whatever the Backend reports
    runaway_budget = False  # only if the graph returns one finished draw

    def promote(self, model_dir, entry):
        ...  # write the sidecars `load` parses: configs, voice files
```

The suite writes every other expected file as placeholder bytes. It then
reaches the Architecture through `synthesizer_for`, the way the Engine does,
with its Backend faked underneath:

- an ONNX graph answers with the scripted draw, or with `graphs[stem]` for
  an intermediate graph;
- `mlx_audio`'s `load` returns a fake model that replays the same draw.

It holds the Architecture to all of the following:

- a directory the store has not promoted is never loaded;
- a Voice the entry lacks is refused;
- audio comes back at the declared sample rate, or the Backend's;
- a silent draw raises `DegenerateDraw`;
- a draw that never ends is cut, later for a longer text, or, where
  `runaway_budget = False`, handed back whole;
- the warm-up record is one a Narration could store;
- the committed knobs survive the schema;
- an off-schema or out-of-range knob is refused at boot.

`tests/architectures/test_registry.py` fails while a registered
Architecture has no `Conformance` subclass. An Architecture on a third
Backend adds that Backend's fake to `fakes.py` and to `Conformance`'s
`backends` fixture.

```sh
uv run pytest tests/architectures/test_<id>.py
```

Nothing downloads and nothing imports MLX, so this runs on ubuntu. That run,
with the curation output from step 2, goes in the PR description.
