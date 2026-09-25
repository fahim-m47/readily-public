"""The store's derived graph is the pinned export plus a hand-encoded
protobuf tail. ONNX Runtime has to read that tail as a real graph output,
or every Kokoro Block would fall back to estimated word times."""

from pathlib import Path

import numpy as np
import pytest

from readily_engine.loading.onnx import onnx_session
from readily_engine.store.derived import output_tail, write_derived

# A two-node graph whose `duration` constant is not a graph output.
HIDDEN_DURATION = Path(__file__).parent / "fixtures" / "graphs" / "hidden-duration.onnx"


def test_the_tail_exposes_a_tensor_the_export_computes_but_hides(tmp_path: Path):
    derived = tmp_path / "timed.onnx"
    write_derived(HIDDEN_DURATION, derived, tail=output_tail(["duration"]))

    session = onnx_session(derived)
    audio, durations = session.run(None, {"input": np.array([0.1, 0.2], np.float32)})

    assert durations.tolist() == [2, 3]
    assert audio.tolist() == pytest.approx([0.1, 0.2])
    assert derived.read_bytes()[: HIDDEN_DURATION.stat().st_size] == (
        HIDDEN_DURATION.read_bytes()
    )


def test_a_name_no_node_produces_is_refused_at_load(tmp_path: Path):
    derived = tmp_path / "timed.onnx"
    write_derived(HIDDEN_DURATION, derived, tail=output_tail(["nowhere"]))

    with pytest.raises(Exception, match="nowhere"):
        onnx_session(derived)


def test_lengths_past_one_byte_encode_as_multi_byte_varints(tmp_path: Path):
    long_name = "x" * 300
    derived = tmp_path / "timed.onnx"
    write_derived(HIDDEN_DURATION, derived, tail=output_tail([long_name]))

    with pytest.raises(Exception, match=long_name):
        onnx_session(derived)
