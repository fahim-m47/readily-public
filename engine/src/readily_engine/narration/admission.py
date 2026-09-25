"""Simple-mode admission of History.

Lives beside the worker rather than in `catalog.recipes` because chunking
imports the Catalog, so recipes importing chunking would be a cycle.
"""

from dataclasses import replace

from readily_engine.catalog import CatalogEntry
from readily_engine.catalog.recipes import UnqualifiedRecipe, resolve_simple
from readily_engine.chunking import chunk
from readily_engine.generation import GenerationRecord
from readily_engine.storage.storage import NarrationPlan


def require_simple_plan(entry: CatalogEntry, plan: NarrationPlan) -> None:
    """Admit History whose frozen Blocks match the qualified recipe.

    The draw is not the recipe: a Block a retry rescued, or one rescued
    again after eviction, is stored under another seed, and the
    qualification digest ignores `word_timing`, so both are admitted.
    """
    voice = plan.settings.voice_id
    controls = resolve_simple(entry, voice)
    expected = chunk(plan.source, controls.entry.tunables).blocks
    if (
        plan.settings.pause_policy != controls.entry.tunables.pause_policy
        or len(expected) != len(plan.segments)
        or any(
            not _same_draw(
                segment.generation,
                GenerationRecord.for_entry(controls.entry, voice, block.text),
            )
            or segment.source_start != block.start
            or segment.source_end != block.end
            or segment.boundary != block.boundary.value
            for segment, block in zip(plan.segments, expected, strict=True)
        )
    ):
        raise UnqualifiedRecipe(
            "This Narration does not use a qualified recipe. Open it in Advanced."
        )


def _same_draw(stored: GenerationRecord | None, expected: GenerationRecord) -> bool:
    if stored is None:
        return False
    return replace(stored, seed=None, word_timing=expected.word_timing) == expected
