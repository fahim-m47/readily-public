"""The Architecture interface (ADR 0014): what one Voice Model's code
declares to the Engine around it. An Architecture is a package under `loading/`,
registered in `registry.py` under the id a Catalog entry's `architecture`
field names; everything the rest of the Engine needs from an Architecture is a
member here, so adding a member is a change every Architecture must answer and
`tests/architectures/test_registry.py` makes a missing answer a test failure.
What an Architecture must *do* is `tests/architectures/conformance.py`."""

from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from readily_engine.catalog import CatalogEntry
from readily_engine.generation import Synthesizer
from readily_engine.loading.references import VoiceReferences

# How a Voice conditions the model: `preset` picks a bundled style by id,
# `reference` conditions on a Voice Reference clip every Voice must carry.
type Conditioning = Literal["preset", "reference"]


@runtime_checkable
class Architecture(Protocol):
    # Paths, relative to the promoted directory, the Architecture loads; a
    # committed entry's pinned and derived files must cover them.
    expected_files: frozenset[str]
    conditioning: Conditioning
    # The text `LazySynthesizer.prewarm` runs through the model at boot.
    warmup_text: str
    # The knobs `generate` reads, as the schema it parses the Record's
    # parameters with. `loading/parameters.py` checks an entry against it at
    # `readily-curate --write` and Engine boot.
    parameters: type[BaseModel]
    # The Block budgets a curator may sweep with `readily-curate
    # --chunk-budget`, or None when the Architecture qualifies at one budget.
    chunk_budget_candidates: range | None

    def load(
        self, model_dir: Path, entry: CatalogEntry, *, references: VoiceReferences
    ) -> Synthesizer:
        """Load the model from its promoted directory. `references` is the
        entry's Voice References, already verified and decoded — empty for a
        `preset` Architecture."""
        ...
