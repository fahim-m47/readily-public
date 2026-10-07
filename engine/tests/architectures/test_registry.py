"""The registry is the one place a Manifest `architecture` id becomes code
(ADR 0014). Every Architecture it names must satisfy the Architecture interface in
full, because `synthesizer_for` reads the Architecture's attributes without checking
them and a partial Architecture would fail on the first Narration, not at boot."""

import ast
import platform
import re
import sys
from pathlib import Path

from pydantic import BaseModel

from readily_engine.catalog import load_manifest
from readily_engine.loading.architecture import Architecture
from readily_engine.loading.registry import REGISTRY, available_backends

ARCHITECTURE_ID = re.compile(r"^[a-z][a-z0-9_]*$")


def test_every_registered_architecture_declares_the_whole_interface():
    # Walks the table rather than naming its rows, so detaching a model
    # (ADR 0014) edits the registry and nothing here.
    assert REGISTRY
    for architecture_id, architecture in REGISTRY.items():
        assert ARCHITECTURE_ID.match(architecture_id), architecture_id
        assert isinstance(architecture, Architecture), architecture_id
        assert architecture.backend in {"onnxruntime", "mlx-audio"}, architecture_id
        assert callable(architecture.expected_files), architecture_id
        assert architecture.conditioning in {"preset", "reference"}, architecture_id
        assert isinstance(architecture.warmup_text, str) and architecture.warmup_text
        assert issubclass(architecture.parameters, BaseModel), architecture_id
        assert architecture.chunk_budget_candidates is None or isinstance(
            architecture.chunk_budget_candidates, range
        ), architecture_id
        assert callable(architecture.load), architecture_id


def test_every_committed_entry_pins_or_derives_what_its_architecture_loads():
    # An Architecture names the paths loading reads; the Catalog pins (or derives, ADR
    # 0011) what the store promotes. The two are written by hand in two
    # places, so this is where a renamed weight file, a dropped tokenizer
    # sidecar or a Voice without its style is caught, not at Engine boot or
    # on the first Narration after an install.
    for entry in load_manifest().models:
        expected = REGISTRY[entry.architecture].expected_files(entry)
        assert isinstance(expected, frozenset) and expected, entry.id
        assert all(isinstance(path, str) for path in expected), entry.id
        promoted = {file.path for file in entry.files} | {
            derived.path for derived in entry.derived_files
        }
        missing = expected - promoted
        assert not missing, f"{entry.id} promotes no {sorted(missing)}"


def test_every_registered_architecture_runs_conformance():
    # "Supported" means the conformance suite passes (ADR 0014 §4), so an
    # Architecture registered without a `Conformance` subclass is refused
    # here rather than shipping untested. Read from source, not imported,
    # so this file names no Architecture.
    entries = {entry.id: entry.architecture for entry in load_manifest().models}
    covered = set()
    for path in Path(__file__).parent.glob("test_*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef) and any(
                isinstance(base, ast.Name) and base.id == "Conformance"
                for base in node.bases
            ):
                model_id = next(
                    statement.value.value
                    for statement in node.body
                    if isinstance(statement, ast.Assign)
                    and [target.id for target in statement.targets] == ["model_id"]
                )
                covered.add(entries[model_id])
    assert covered == set(REGISTRY)


def test_the_instant_tier_runs_on_onnxruntime_so_every_platform_can_ship_it():
    # Intel Macs and Linux ship without mlx-audio, so an instant entry
    # that crossed onto the MLX lane would be one those builds cannot run.
    # The expressive Tier is the one allowed to need it.
    backends: dict[str, set[str]] = {}
    for entry in load_manifest().models:
        backend = REGISTRY[entry.architecture].backend
        backends.setdefault(entry.tier.value, set()).add(backend)
    assert backends == {"instant": {"onnxruntime"}, "expressive": {"mlx-audio"}}


def test_mlx_audio_is_available_exactly_where_its_wheel_resolves():
    # pyproject.toml's marker on the mlx-audio dependency: Apple silicon only.
    apple_silicon = sys.platform == "darwin" and platform.machine() == "arm64"
    assert ("mlx-audio" in available_backends()) == apple_silicon
    assert "onnxruntime" in available_backends()
