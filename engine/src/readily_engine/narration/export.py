"""Export: the one way Narration audio leaves Readily's own storage.

ADR 0004 §4 — a Narration is never materialized as an internal file. It is a
manifest of Segment hashes, and a single audio file exists only because the
user asked for one at a location they chose: decode, concat, encode once.

Because the Segment cache is lossless FLAC and the source of truth, exporting
audio that is already present costs no synthesis and no second lossy pass.
Audio a retention sweep has evicted is re-synthesized first — through the same
serial worker, queued behind whatever is playing, so pressing Export is always
instant and never interrupts the listener (ADR 0002 §3).
"""

import logging
import os
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Literal, Protocol, TypedDict

import numpy as np

from readily_engine.audio import FloatPcm
from readily_engine.audio.encoding import AudioEncodingError, encode_m4a, encode_wav
from readily_engine.narration.stream import assemble
from readily_engine.storage.segments import StoredAudio
from readily_engine.storage.storage import (
    NarrationPlan,
    NarrationStorage,
    PlannedSegment,
)
from readily_engine.wire import WIRE_VERSION, Observable, WireError, wire_error

logger = logging.getLogger(__name__)

# M4A first: it is the default because it is what a listener can actually hand
# to someone else. WAV is the lossless option. No MP3 (ADR 0004 §4).
ExportFormat = Literal["m4a", "wav"]

ExportPhase = Literal["idle", "preparing", "encoding", "finished", "failed"]

# Export progress moves once per Block, which for a long read is seconds
# apart; there is no playhead here to interpolate, so the stream can poll
# lazily compared with the narration stream's 50ms.
_POLL_SECONDS = 0.25

# How long `close` waits for an in-flight Export. An Export blocked on a Fill
# is already bounded by the worker's own shutdown, which releases it.
_SHUTDOWN_GRACE_SECONDS = 20

Encoder = Callable[[FloatPcm, int, Path], None]
_ENCODERS: dict[str, Encoder] = {"m4a": encode_m4a, "wav": encode_wav}


class ExportInProgress(RuntimeError):
    """An Export is already running; the Engine runs one at a time."""


class ExportSnapshot(TypedDict):
    """The v1 export snapshot (`docs/wire.md`), as sent."""

    version: int
    phase: ExportPhase
    narrationId: str | None
    format: str | None
    completedBlocks: int
    totalBlocks: int
    error: WireError | None


class Filler(Protocol):
    """What Export needs of the generation worker: the audio a Block may
    replay, and otherwise one Block, synchronously, without preempting the
    Narration being heard."""

    def cached(
        self, plan: NarrationPlan, segment: PlannedSegment
    ) -> StoredAudio | None: ...

    def fill(
        self, plan: NarrationPlan, segment: PlannedSegment
    ) -> StoredAudio | None: ...


def _idle(phase: ExportPhase = "idle") -> ExportSnapshot:
    return ExportSnapshot(
        version=WIRE_VERSION,
        phase=phase,
        narrationId=None,
        format=None,
        completedBlocks=0,
        totalBlocks=0,
        error=None,
    )


class ExportDestinationError(ValueError):
    """The requested destination is not somewhere the Engine will write.

    A `ValueError` like the Export request's other rejections, so the
    server maps every bad-request case through one branch.
    """


def check_destination(
    destination: Path, data_root: Path, export_format: ExportFormat
) -> Path:
    """Validate a user-chosen Export path before anything is written to it.

    The path arrives over the loopback API from the webview, which got it
    from the shell's native save panel (threat model B4). The Engine checks
    it rather than trusting it, and the checks are what they are: absolute,
    in a directory that already exists, carrying the chosen format's
    extension, and outside the Engine's own data tree — Export writes *out*
    of Readily's storage, never back into it.

    That narrows a compromised webview to overwriting a user-writable file
    that already ends in `.m4a` or `.wav`; it does not reduce the write to
    nothing. The Engine is the only process with a file-write capability
    here precisely so that this one guard is the whole surface, and
    widening what Export accepts is a threat-model amendment.
    """
    if not destination.is_absolute():
        raise ExportDestinationError("The Export destination must be an absolute path")
    if destination.name in {"", ".", ".."}:
        raise ExportDestinationError("The Export destination must name a file")
    if destination.suffix.lower() != f".{export_format}":
        raise ExportDestinationError(
            "The Export destination must carry the exported format's extension"
        )
    parent = destination.parent.resolve()
    if not parent.is_dir():
        raise ExportDestinationError("The Export destination directory does not exist")
    if destination.is_dir():
        raise ExportDestinationError("The Export destination is a directory")
    resolved_root = data_root.resolve()
    if parent == resolved_root or resolved_root in parent.parents:
        raise ExportDestinationError(
            "The Export destination may not be inside Readily's own storage"
        )
    return parent / destination.name


class Exporter:
    """Run one Export at a time on its own thread, and publish its progress.

    Deliberately not on the generation thread: a fully cached Narration
    decodes and encodes with no synthesizer involved at all, so it can run
    while something else is playing. Only a Block whose audio has been
    evicted crosses over, one `fill` at a time, and waits its turn there.
    """

    def __init__(
        self,
        storage: NarrationStorage,
        worker: Filler,
        data_root: Path,
        *,
        encoders: dict[str, Encoder] | None = None,
    ) -> None:
        self._storage = storage
        self._worker = worker
        self._data_root = data_root
        self._encoders = dict(encoders) if encoders is not None else dict(_ENCODERS)
        self._state: Observable[ExportSnapshot] = Observable(
            _idle(), poll_seconds=_POLL_SECONDS
        )
        self._lock = self._state.lock
        self._thread: threading.Thread | None = None
        self._closed = threading.Event()

    def start(
        self, narration_id: str, destination: Path, export_format: ExportFormat
    ) -> None:
        """Accept an Export and return: the work happens on the thread.

        Raises before accepting — never mid-Export — so the button press is
        either a clean rejection the client can explain or a job that is
        now running. `KeyError` for an unknown Narration,
        `ExportDestinationError` for a path the Engine will not write,
        `ExportInProgress` when one is already under way, and `RuntimeError`
        once the Engine is shutting down.
        """
        if export_format not in self._encoders:
            raise ValueError("Readily exports M4A or WAV")
        target = check_destination(destination, self._data_root, export_format)
        plan = self._storage.plan(narration_id)
        with self._lock:
            if self._closed.is_set():
                # A `RuntimeError`, not `ExportInProgress`: nothing is in
                # progress, the Engine is going away, and the wire has a
                # code for that (`engine_unavailable`).
                raise RuntimeError("The Engine is shutting down")
            if self._thread is not None and self._thread.is_alive():
                raise ExportInProgress(narration_id)
            self._state.update(
                phase="preparing",
                narrationId=narration_id,
                format=export_format,
                completedBlocks=0,
                totalBlocks=len(plan.segments),
                error=None,
            )
            self._thread = threading.Thread(
                target=self._run,
                args=(plan, target, export_format),
                daemon=True,
                name="readily-export",
            )
            self._thread.start()

    def snapshot(self) -> ExportSnapshot:
        return self._state.snapshot()

    def events(self) -> AsyncIterator[ExportSnapshot]:
        return self._state.events()

    def close(self) -> None:
        """Stop accepting Exports and wait out the one in flight.

        An Export that is still running when the grace expires is left to
        the process exit; it writes only to its own temporary file, so a
        half-written Export can never be mistaken for a finished one.
        """
        self._closed.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=_SHUTDOWN_GRACE_SECONDS)

    def _run(
        self,
        plan: NarrationPlan,
        destination: Path,
        export_format: ExportFormat,
    ) -> None:
        try:
            pcm, sample_rate = self._assemble(plan)
            if not len(pcm):
                raise AudioEncodingError("The Narration produced no audio to export")
            with self._lock:
                self._state.update(phase="encoding")
            self._encode(pcm, sample_rate, destination, export_format)
        except Exception as error:
            # The type and not the traceback: an OSError from the rename
            # or a CalledProcessError from afconvert quotes the destination,
            # the path the reader chose, and the log file never holds one.
            logger.error(
                "Exporting Narration %s failed: %s", plan.id, type(error).__name__
            )
            with self._lock:
                self._state.update(
                    phase="failed",
                    error=wire_error(
                        "export_failed",
                        "The Engine could not write this Narration to a file.",
                    ),
                )
            return
        with self._lock:
            self._state.update(phase="finished", error=None)

    def _assemble(self, plan: NarrationPlan) -> tuple[FloatPcm, int]:
        """Rebuild the whole Narration exactly as playback assembled it.

        The same `Assembler` with the saved pause policy, in Block order
        from the start — not from the playhead. Anything else and the file
        would not be what the listener heard: assembly trims each Segment's
        model padding, authors the pause at every seam, and crossfades the
        mid-sentence ones, so a hand-rolled concatenation is a different
        recording of the same words.
        """
        pieces: list[FloatPcm] = []
        sample_rate = 0
        completed = 0

        def resolve(segment: PlannedSegment) -> StoredAudio | None:
            nonlocal sample_rate, completed
            audio = self._worker.cached(plan, segment)
            if audio is None and not plan.is_gap(segment.ordinal):
                audio = self._worker.fill(plan, segment)
                if audio is None:
                    raise AudioEncodingError(
                        "A Block of this Narration could not be synthesized"
                    )
            if audio is not None:
                if sample_rate and audio.sample_rate != sample_rate:
                    raise AudioEncodingError(
                        "This Narration mixes sample rates and cannot be one file"
                    )
                sample_rate = audio.sample_rate
            completed += 1
            with self._lock:
                self._state.update(completedBlocks=completed)
            return audio

        for piece in assemble(plan, resolve):
            pieces.append(piece.pcm)
        if not sample_rate:
            raise AudioEncodingError("This Narration has no audio to export")
        return np.concatenate(pieces), sample_rate

    def _encode(
        self,
        pcm: FloatPcm,
        sample_rate: int,
        destination: Path,
        export_format: ExportFormat,
    ) -> None:
        """Encode once, into a temporary beside the destination, then rename.

        The rename is atomic within the destination directory, so the user
        never finds a truncated file under the name they chose — an Export
        interrupted by a crash or a shutdown leaves a dot-file they can
        delete, not a broken recording they might send to someone.
        """
        temporary = destination.parent / f".readily-export-{uuid.uuid4().hex}.tmp"
        try:
            self._encoders[export_format](pcm, sample_rate, temporary)
            if not temporary.is_file():
                raise AudioEncodingError("The Export encoder produced no file")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
