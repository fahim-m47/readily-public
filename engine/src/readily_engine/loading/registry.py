"""Every Architecture the Engine ships, by the id a Catalog entry names.

A literal table, one line per Architecture, so `loading/` stays the only package
that loads a model (threat model B2) and the licence gate sees every shipped
line in-tree: a model ships by adding a line here, and that line is the
review point. Never populated by import machinery — the Semgrep rule
`engine-no-dynamic-import` forbids it.
"""

# Reads whether a distribution is installed; it loads nothing, so no string
# ever becomes a module here.
from importlib import metadata  # nosemgrep: engine-no-dynamic-import

from readily_engine.loading import (
    chatterbox_turbo,
    qwen3,
    supertonic,
    vibevoice,
    voxcpm2,
)
from readily_engine.loading.architecture import Architecture, Backend
from readily_engine.loading.styletts2 import kitten, kokoro

REGISTRY: dict[str, Architecture] = {
    "chatterbox_turbo": chatterbox_turbo.ARCHITECTURE,
    "kitten": kitten.ARCHITECTURE,
    "kokoro": kokoro.ARCHITECTURE,
    "qwen3": qwen3.ARCHITECTURE,
    "supertonic": supertonic.ARCHITECTURE,
    "vibevoice": vibevoice.ARCHITECTURE,
    "voxcpm2": voxcpm2.ARCHITECTURE,
}


class UnknownArchitecture(ValueError):
    """A Manifest `architecture` id the registry does not know. The Manifest
    parser cannot check this itself (`catalog/` imports nothing from
    `loading/`), so `readily-curate --write` and Engine boot refuse it."""


def architecture_named(architecture_id: str) -> Architecture:
    try:
        return REGISTRY[architecture_id]
    except KeyError:
        raise UnknownArchitecture(
            f"{architecture_id!r} names no Architecture; the registry knows "
            f"{', '.join(sorted(REGISTRY))}"
        ) from None


def available_backends() -> frozenset[Backend]:
    """The Backends this machine can run. onnxruntime ships everywhere;
    mlx-audio only where its wheel resolves, which pyproject.toml's marker
    limits to Apple silicon. Asked of the installed distributions, so
    nothing is imported to find out."""
    try:
        metadata.distribution("mlx-audio")
    except metadata.PackageNotFoundError:
        return frozenset({"onnxruntime"})
    return frozenset({"onnxruntime", "mlx-audio"})
