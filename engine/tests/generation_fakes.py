"""Catalog and record builders for tests that supply a fake synthesis lane."""

from dataclasses import replace

from conftest import make_entry

from readily_engine.generation import GenerationRecord


def record(text="Hello", voice="narrator", **changes):
    return replace(
        GenerationRecord("acme:1m", 1, voice, None, "streaming", {}, text),
        **changes,
    )


def entry_for(settings):
    name, tag = settings.model_id.split(":")
    return make_entry(
        name=name,
        tag=tag,
        version=settings.catalog_version,
        default_voice=settings.voice_id,
        voices=[{"id": settings.voice_id, "name": "Test Voice", "language": "en"}],
    )
