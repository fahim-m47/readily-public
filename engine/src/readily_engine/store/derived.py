"""Graph outputs the store derives beside a pinned ONNX export (ADR 0011).

Kokoro's pinned export computes the duration vector word times need but
does not list it as a graph output. ONNX Runtime loading a patched graph
from bytes holds about a gigabyte more resident memory than loading the
same graph from a file, so the store writes the patched graph as a file
once, at install, beside the pinned one. The pinned file is never touched:
the derived file is the pinned bytes followed by a protobuf tail naming
the extra outputs, which protobuf merges onto the graph already read.
"""

from collections.abc import Iterable
from pathlib import Path

_COPY_CHUNK_BYTES = 1 << 20
# Length-delimited field tags, `(field_number << 3) | 2`, for
# ModelProto.graph, GraphProto.output and ValueInfoProto.name.
_MODEL_GRAPH = b"\x3a"
_GRAPH_OUTPUT = b"\x62"
_VALUE_INFO_NAME = b"\x0a"


def _varint(value: int) -> bytes:
    encoded = bytearray()
    while value > 0x7F:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _field(tag: bytes, payload: bytes) -> bytes:
    return tag + _varint(len(payload)) + payload


def output_tail(outputs: Iterable[str]) -> bytes:
    """The bytes that, appended to a serialised ModelProto, add `outputs`
    to its graph's output list by name alone. ONNX Runtime reads each
    name's type and shape from the node that produces it, and refuses to
    load a graph naming an output no node produces."""
    graph = b"".join(
        _field(_GRAPH_OUTPUT, _field(_VALUE_INFO_NAME, name.encode("utf-8")))
        for name in outputs
    )
    return _field(_MODEL_GRAPH, graph)


def write_derived(source: Path, target: Path, *, tail: bytes) -> None:
    """Stream `source` into `target`, then append `tail`."""
    with source.open("rb") as reader, target.open("wb") as writer:
        while chunk := reader.read(_COPY_CHUNK_BYTES):
            writer.write(chunk)
        writer.write(tail)
