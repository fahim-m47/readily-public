# One Architecture object per Voice Model, selected by a Manifest field

Status: accepted (2026-09-19) · Builds on [ADR 0003](0003-baked-in-hash-pinned-catalog.md) and [ADR 0011](0011-synthesizer-word-times.md)

The Engine's synthesis seam is one `Synthesizer.generate`, crossed by every
caller through `synthesizer_for`. The question is how the lane behind that
seam is chosen and where knowledge of one model lives. A loader picked from
hand-edited dicts keyed by an entry's `name` or `id`, a model-only validator
in `catalog/manifest.py`, or a check for one model by name in
`curation/cli.py` is each a place a new model must be remembered, and none
of them is where its code lives. This ADR decides the interface a Voice
Model implements, the key that selects it, and what the block behind it
owns.

## The decisions

1. **An Architecture is one object per model with a fixed set of declared
   facts and one `load`, not a data spec fed to a family synthesizer.**

   Two shapes were designed. (a) An interface each model implements: a
   `load` that turns a promoted directory into a `Synthesizer`, plus the
   facts its consumers need declared as attributes. (b) A data-only spec
   per export (graph input names, vocabulary, style lookup, expected
   files, budgets) and one deep synthesizer per family.

   Compared on three axes:

   - *Depth.* Under (a) each block is deep: a handful of facts and one
     method, everything else private. Under (b) the family synthesizer is
     deep but the spec is wide, because what varies between the
     exports is behaviour, not values: Kokoro chooses a dialect by Voice,
     Kitten runs the shared G2P and then respells its phonemes into the
     export's own inventory, Supertonic runs four graphs with no G2P, Qwen
     conditions on a decoded reference clip, Chatterbox Turbo takes text
     only. A spec that carries those is a spec of callables, which is the
     old module with its name changed; a spec that cannot carry them
     pushes the difference into `if spec.name == "kitten"` inside the
     family synthesizer, the leak this ADR exists to remove, one level
     down.
   - *Locality.* Under (a) everything about one model sits in one package
     directory, so detaching it is deleting that directory. Under (b) a
     model is split between its spec and a family module, and detaching
     one export leaves branches in the family that nothing uses.
   - *Seam placement.* Under (a) the seam is at the model and the
     conformance suite (`tests/architectures/conformance.py`) crosses it once, with one fake per runtime.
     Under (b) there are two seams, family and spec, and a conformance
     suite has to test spec validity as a second contract.

   (a) wins on all three. The family sharing that makes (b) attractive
   survives as ordinary library code a block imports: the ONNX session
   options in `loading/onnx.py`, the Misaki G2P, token and timing helpers
   in `loading/styletts2/`, and the streaming, gate, scrub and runaway
   cutoff in `loading/mlx_lane.py`. Kokoro and Kitten are one family with
   a spec file per export in `loading/styletts2/`, and that family package
   registers two Architectures; the interface does not care how a block is
   built behind it.

   The interface is a `typing.Protocol` in `loading/architecture.py`, the
   idiom `Synthesizer`, `PromotedDirs` and `Session` use, rather
   than an abstract base class: there is no shared behaviour to inherit,
   and a Protocol names a contract without a base class to carry it. No
   type checker runs in the gates, so the Protocol is `@runtime_checkable`
   and `tests/architectures/test_registry.py` walks the table and
   asserts that every entry `isinstance` the Protocol, which proves each
   member is present, and then that each declared attribute has the type
   the table gives it, which `isinstance` does not; that is what stops
   a half-answered Architecture at `bun run verify`. Its members, each
   named after the consumer outside the block that reads it:

   | Member | Read by |
   |---|---|
   | `load(model_dir, entry, *, references) -> Synthesizer` | `synthesizer_for`, behind `LazySynthesizer` |
   | `expected_files: frozenset[str]` | the boot check (decision 3), curation drafts |
   | `conditioning: "preset" \| "reference"` | `synthesizer_for`'s reference validation, curation drafts |
   | `warmup_text: str` | `LazySynthesizer.prewarm` |
   | `parameters: type[BaseModel]` | the two gates of decision 3, the conformance suite |
   | `chunk_budget_candidates: range \| None` | `readily-curate --chunk-budget` |

   `load` takes the entry because Voices and Tunables live there and a
   block reads them, not the Manifest. It takes `references` already
   verified and decoded, not a `references_root` to read them from,
   because `load` runs behind `LazySynthesizer` and a reference
   Architecture that is not the installed default would not run until the
   first Narration. `synthesizer_for` calls `voice_references`
   eagerly, outside the lazy closure, so a bundled clip that is missing or
   no longer matches its pinned SHA-256 fails at Engine start; threat
   model B2 wants that failure at startup, before anything decodes the
   bytes. So verification and decoding stay an eager step in
   `synthesizer_for`, shared library code under `loading/` because
   `conditioning` already says which entries need it, and `load` receives
   the verified mapping, empty for a preset Architecture. That mapping's
   type, `VoiceReferenceAudio`, sits beside the eager step in
   `loading/references.py` by the rule in decision 4: it is named by `architecture.py` and by every block that
   conditions on a clip, so it cannot stay inside one lane's module. No
   `sample_rate` member: no consumer outside a block reads one, and
   the MLX lane learns its rate from each draw, so the member is added
   when a consumer appears. Nor is the MLX runaway budget: its only
   reader is `MlxSynthesizer`, shared library code that a block's `load`
   constructs and hands the budget to, so it is a constructor argument
   inside the block and becomes a member only if a consumer outside a
   block reads it. The worker's shutdown grace is not in the table
   either: it bounds a thread join, not a draw, and a join has no text to
   budget by, so it stays a worker policy. Adding a member is a
   change to `architecture.py` that every block must answer, and the
   conformance suite makes a missing answer a test failure rather than a
   runtime surprise. `LazySynthesizer` stays generic: it wraps `load`
   behind the installed gate and builds the warm-up Record from
   `warmup_text`.

   `parameters` needs a dependency path, because `compose` lives in
   `catalog/` and decision 3 denies it a registry import. The path is that
   `compose` never sees an Architecture. The Manifest already declares,
   per entry, every control a user may set and its range, and `compose`
   validates overrides against those declarations inside `catalog/`. The Architecture's `parameters` class says which knobs
   the code reads. The two are cross-checked at the gates of decision 3,
   `readily-curate --write` and Engine boot, which have both in scope: an
   entry that declares or pins a parameter its Architecture does not read
   is refused there, and so is a Supertonic entry that pins no `steps`,
   a required field of that block's class. No model-only validator lives
   in the Manifest.

   Presence is not enough, though, because a per-Architecture class takes
   schema validation away from `compose`: an Advanced
   Override inside the Manifest's declared range but outside the block's
   schema would survive planning and fail deep in `generate`. So the
   gates check containment, not existence, over the controls the two
   sides share: the entry's declared parameters that name a field of the
   Architecture's schema. For each of those, the declared domain must be
   one the schema accepts in full, so that a value `compose` admits is a
   value the block admits. A narrower schema is not refused for being
   narrower; it is refused for being narrower than what the entry
   advertises, and the fix is to narrow the entry's declaration to match.
   Every committed entry passes this.

   Three things stay outside the intersection and keep validating in
   `catalog/`, where they already do. `decode_mode` and `word_timing`
   are Readily's own fields by decision 5, not model knobs. The
   `READILY_CONTROLS` — `seed`, `prepare_first` and the two chunk budgets
   — are the Engine's. And the Tunables are the chunker's: they are not
   Architecture-owned at all, and `Tunables.the_first_block_is_short`
   relates two of them in a way no per-control declaration can express,
   so `compose` keeps running `Tunables.model_validate` and the test that
   refuses a first Block longer than the budget keeps passing unchanged.
   The gates cover only the model knobs.

   The check is decidable because it compares field constraints, the
   `ge`, `gt`, `le`, `lt`, type and strictness a Pydantic field carries,
   against the `hard_range`, `choices` and `kind` a Control declares. A
   rule a block expresses as a `model_validator` rather than a field
   constraint is invisible to it and is therefore out of this contract:
   a block that needs one either makes it a field constraint or accepts
   that it fires inside `generate`. `loading/parameters.py` holds the
   comparison and the conformance suite tests it.

   `compose` keeps validating against the declarations alone, which is
   what lets it stay in `catalog/`. Inside the block, `generate` parses
   the Record's `parameters` with the same class. The Record carries
   parameters as the declared mapping `canonical_json` writes, under two
   constraints: `generation.py` imports nothing
   from `loading/` either, and no digest moves.

   Any model-specific rule that a consumer outside the block enforces is
   expressed the same way: as a fact the block declares and the consumer
   reads, never as a check on `entry.name`. The Qwen-only chunk-budget
   range is the second example after `steps`: it is
   `chunk_budget_candidates`, the budgets a curator
   may sweep with `--chunk-budget` when qualifying a Voice for Simple
   mode, `None` for a block with no sweep, and `readily-curate` reads it
   instead of the entry's name. A member read off an Architecture cannot
   feed argparse's `choices=` before any entry is resolved, so the range
   check runs after `resolve`.

2. **The registry key is a new Manifest field, `architecture`, and the
   registry is a static table.** Not `name`: ADR 0003 §5 keeps `name`
   across a weights bump and lets a tag name a variant, so a name is
   neither guaranteed to mean one architecture nor the thing that changes
   when the architecture does. The field is a plain string in `catalog/manifest.json`,
   and its value is the name of the package (or, for a family such as
   `loading/styletts2/`, the spec file inside it) that implements it, so it is
   validated there for one shape only: a Python identifier, lowercase
   letters, digits and underscores (`^[a-z][a-z0-9_]*$`), which is its
   own pattern and not the hyphenated entry-name one. Its values are the
   code's names for what it implements and are never derived from an
   entry's `name`: the table is `kokoro`, `kitten`, `supertonic`,
   `qwen3` and `chatterbox_turbo`, the last two spelled apart from their
   entries because Turbo is a different architecture from Chatterbox and
   Qwen3-TTS is one export of Qwen3.

   `loading/registry.py` is a literal mapping from id to Architecture
   object, one import and one line per block. No discovery, no entry
   points, no plugins: `engine-no-dynamic-import` forbids `importlib`,
   `loading/` being the only package that loads a model is threat model
   B2, and the licence gate needs every shipped line in-tree. A model
   ships by adding one line to this table, and that line is the review
   point.

   The code says whether a model takes a Voice Reference
   (`conditioning`), and the curation draft reads that rather than
   writing a Manifest field. `backend` is neither a member nor a Manifest
   field: a Backend is which runtime a block imports, a fact about an
   Architecture that is read from its code rather than declared by it.
   The code is the source of truth, and the Manifest never disagrees
   with it.

3. **`catalog/` imports nothing from `loading/`.** The dependency
   direction stays `loading → catalog → generation`; a registry import
   from the Manifest parser would be a cycle and would drag the runtimes
   into the offline, pure package that CI validates the committed Manifest
   with. So the Manifest cannot know whether an `architecture` string
   resolves, and two later gates refuse one that does not: `readily-curate
   --write`, before an entry is committed, and Engine boot, where
   `EngineNarrator` builds every synthesizer. The unit test
   `test_every_committed_entry_resolves_to_an_architecture` runs the
   committed Manifest through `synthesizer_for` against a fake Store, so
   an unresolvable string fails in CI before it fails on a machine. The
   boot check keeps both reference duties in `synthesizer_for`: the
   cross-check that an entry's Voices carry clips exactly when the code
   conditions on them, read from `conditioning`, and the eager
   verify-then-decode of those clips that decision 1 keeps out of `load`.
   A test checks `expected_files` against every committed entry's pinned
   and derived files. A Manifest that lies about its code
   fails at the seam that chose, not deep inside a runtime.

4. **What a block owns.** A block is a package `loading/<architecture>/`.
   In the package: the code, the parameter schema, the warm-up
   text, the expected files, the runaway budget and every constant
   measured on that model, plus its own test file under `engine/tests/architectures/`
   holding what is unique to it. Outside the
   package, central and with one writer, the Catalog entry: identity,
   pinned hashes, licence, Voices, Tunables, Qualification and the
   editorial the Manifest already holds (`decode_mode`, the declared
   parameters, word timing), because that is the trust root and a block
   must not be able to widen what the Engine downloads. The conformance
   suite is central too, parametrised over the registry, so that
   "supported" means one thing.

   Voice Reference clips stay under `catalog/references/` beside the
   Manifest, because each clip's bytes are pinned in the entry that names
   it, and a pin and its bytes belong to the same writer. The bundled pronunciation data under `loading/data/`
   is shared library data: two Architectures read it through the shared
   G2P, so it is owned by no block. The rule that decides both, and every
   later case: code or data used by exactly one Architecture lives in its
   package; used by two or more, it lives in `loading/` as shared library
   code, and the last block to stop using it takes it out in a follow-up
   PR, not in the detach diff.

5. **`decode_mode` stays a Generation Record field, byte for byte.**
   The alternative, folding it into the two MLX Architectures'
   parameters, is cleaner in one place: the shared Record loses a field
   only MLX honours, and the Manifest validator loses its special case.
   It is paid for everywhere else. `canonical_json` sorts keys, so
   moving `decode_mode` from the top level into `parameters` changes the
   bytes of every Record, not only MLX ones, and with them every Segment
   cache key on every install and every committed `qualification`
   digest: 22 qualified Voices across four of the five entries, 21 of
   them on the three fixed-graph exports that have no decoder to choose,
   would be re-earned for audio that has not changed, and nothing in the
   audio buys that back.

   The field is also not a lie for a fixed-graph export. A Generation
   Record describes how a draw was made, and a one-chunk synthesis is a
   streaming one, so an ONNX Architecture answers with the only value it
   has. So `decode_mode` and `word_timing` are the Record's two
   Readily-side fields, and the per-Architecture schema covers model
   knobs only. Reopen this if a third mode or a
   second decode axis appears; until then the cheapest correct answer is
   the one that leaves 22 digests alone.

6. **The acceptance test for detach and attach.** Two cases, because two
   entries may share an Architecture (decision 2). Detaching an *entry* means deleting it from
   `catalog/manifest.json` with the clips it names under
   `public/previews/` and `catalog/references/`, and its rows in
   `tests/test_catalog_manifest.py`, the one test file that asserts the
   committed Manifest's contents; no code changes. Detaching an
   *Architecture* means detaching every entry that names it, then
   deleting its package directory (or its spec file in a family package),
   its line in `loading/registry.py`,
   its test file, and its row in any `engine/tools/` script that lists it
   (`word_timings.py` also imports from the Kokoro and Kitten blocks).
   After either case `bun run verify` and `bun run
   verify:linux` are green and `git diff --stat` lists exactly those
   paths. Attach is the same list in reverse. Shared library code under
   `loading/` is untouched by construction of decision 4, and a detach
   that has to edit a shared module has found code that belonged in the
   block.

   A model PR runs the Architecture case on the model being added and
   expects nothing else.

## Consequences

- Threat model B2 and the Semgrep rules do not change: `loading/**`
  already covers the subpackages, and the loader remains the one package
  that may load a model. A model PR touches `loading/`, so it takes the
  B2 review lane; that is the intended review point for shipped inference
  code. `engine-no-dynamic-import` keeps the registry a static table.
