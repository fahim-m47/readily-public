"""The session contract and runtime policy shared by ONNX loaders."""

from pathlib import Path
from typing import Protocol

import numpy.typing as npt


class Session(Protocol):
    def run(
        self, outputs: None, inputs: dict[str, npt.NDArray]
    ) -> list[npt.NDArray]: ...


def onnx_session(path: Path) -> Session:
    import onnxruntime as ort

    ort.disable_telemetry_events()
    # The ORT arena stays at its defaults deliberately. Measured on macOS:
    # disabling it raises resident RSS (malloc keeps freed pages the
    # arena would reuse) and every cap knob — per-run shrinkage,
    # kSameAsRequested, device-allocator initializers — lands within noise
    # of the default's ~935MB. The real RSS lever is an int8 model, which
    # is a Catalog change, not a session option.
    # Loaded from its path, never from bytes: ORT given a serialised graph
    # holds about a gigabyte more resident memory for Kokoro than ORT
    # given the same graph's file, which is why the store writes the
    # duration-exposing graph to disk (`store/derived.py`).
    return ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
