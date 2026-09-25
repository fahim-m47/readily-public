# Declared controls

A control is one input a user may change, and the Catalog Manifest names
the ones each entry exposes in `parameters`, keyed by the input's name. A
declaration carries `label`, `unit`, `description` and `kind`. A numeric one adds
`hard_range`, the bounds an override must satisfy, and optionally
`recommended_range`, the narrower band worth exploring first; a range with
no upper limit writes `max` as `null`. Choices list their accepted values
and booleans accept only JSON booleans. Only the hard range refuses an
override.

A declaration carries no default. The default is the entry field the
declaration names: `decode_mode` for the decode mode, and the matching
`generation_parameters` key for every model control, so an entry cannot
declare a parameter it does not itself set. Which keys an entry may pin,
and which ranges its controls may admit, is its Architecture's `Parameters`
schema to say: `readily-curate --write` and Engine boot refuse an entry whose
knobs or declared ranges that schema would not accept in full (ADR 0014).
Readily's four shared controls default to the entry's Tunables, which is
why `chunk_budget_chars`, `first_block_chars`, `pause_sentence_ms` and
`pause_paragraph_break_ms` need no declaration of their own. They come
from `READILY_CONTROLS` in `catalog/controls.py` along with `seed`, which
defaults to null so the Generation Record derives it, and `prepare_first`,
which defaults to true.
The app accepts 1-4096 characters and 0-60000 ms; the first Block still
cannot exceed the Block budget.

`CatalogEntry.compose` takes an override record, checks every value against
its declaration, and returns one `Effective` snapshot: the entry with those
values folded into its own fields, plus the resolved `seed` and
`prepare_first`. Everything downstream reads the composed entry, so a
Narration is planned from a single resolved shape rather than from defaults
plus a diff. Supertonic's control is named `steps`, the Generation Record
field it sets, so saved records and Segment keys are untouched.

Settings schema 7 stores overrides as a JSON record keyed first by entry id,
then Voice id. `PATCH /v1/settings/controls` replaces one Voice's record and
`GET /v1/catalog` projects the declarations and each Voice's effective
values; `docs/wire.md` has both. A stored override outlives the Manifest
that accepted it, so a value a later entry no longer declares or no longer
admits is dropped on read, with a warning, instead of refusing the
Narration.

New Narrations snapshot controls before chunking. Sampling, decode mode and
seed become Generation Record inputs; changing them changes Segment keys.
Pauses and prepare-first are saved with the Narration, outside its Segment
keys. Prepare-first delays feeding the playback device until every Block has
been prepared, including gaps for failed Blocks. Every Segment of that
Narration is written and held before playback starts, so the Segment
budget cannot reclaim any of it until the Narration ends. That is the
default posture, and every Simple Narration's. Saved Narrations keep their
records when the user changes settings, and a Catalog bump recomposes the
Voice's current overrides onto the new entry.

Qwen's repetition penalty is declared with a hard minimum of 1.5 because the
pinned mlx-audio backend clamps it there, so a lower value would be accepted
and then ignored.

Controls touch trust boundaries B1, the Manifest reader, and B3, the
authenticated settings API. They add no download targets or network egress.
