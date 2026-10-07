"""Export: the one way Narration audio leaves Readily's own storage.

ADR 0004 §4 — a Narration is never materialized as an internal file. It is a
manifest of Segment hashes, and a single audio file exists only because the
user asked for one: decode, concat, encode once, into the Readily folder in
Documents, so every Export sits in one place the reader can find.

Because the Segment cache is lossless and the source of truth, exporting
audio that is already present costs no synthesis and no second lossy pass.
Audio a retention sweep has evicted is re-synthesized first — through the same
serial worker, queued behind whatever is playing, so pressing Export is always
instant and never interrupts the listener (ADR 0002 §3).
"""

import logging
import os
import re
import shutil
import threading
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal, Protocol, TypedDict

import numpy as np

from readily_engine.audio import FloatPcm
from readily_engine.audio.encoding import (
    AudioEncodingError,
    Encoder,
    ExportFormat,
    export_encoders,
)
from readily_engine.narration.stream import assemble
from readily_engine.storage.segments import StoredAudio
from readily_engine.storage.storage import (
    NarrationPlan,
    NarrationStorage,
    PlannedSegment,
)
from readily_engine.wire import WIRE_VERSION, Observable, WireError, wire_error

logger = logging.getLogger(__name__)

ExportPhase = Literal["idle", "preparing", "encoding", "finished", "failed"]

# Export progress moves once per Block, which for a long read is seconds
# apart; there is no playhead here to interpolate, so the stream can poll
# lazily compared with the narration stream's 50ms.
_POLL_SECONDS = 0.25

# How long `close` waits for an in-flight Export. An Export blocked on a Fill
# is already bounded by the worker's own shutdown, which releases it.
_SHUTDOWN_GRACE_SECONDS = 20


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


# The longest name an Export takes from its Source, in code points. Long
# enough to tell two reads apart in the Finder, short enough to fit its column.
_NAME_LENGTH = 48

# How many " 2", " 3"… suffixes Export tries before it gives up on a name.
_NAME_ATTEMPTS = 1000


def default_audio_folder() -> Path:
    """Where every Export lands: `Readily` in the reader's Documents.

    The shell resolves the platform's Documents folder, which on Linux can
    be anywhere the desktop says, and names the result in READILY_AUDIO_DIR
    (`src-tauri/src/data.rs`). It opens the same folder for its "Open audio
    folder" button, so the two cannot disagree. A standalone run falls back
    to `~/Documents`.
    """
    override = os.environ.get("READILY_AUDIO_DIR")
    if override:
        return Path(override)
    return Path.home() / "Documents" / "Readily"


def file_stem(source: str) -> str:
    """An Export's file name, without its extension: the Source's own first
    words, flattened to one line and stripped of what a file name cannot
    carry. Never empty, never a hidden file, and never ending in a dot, which
    would double the one before the extension.
    """
    cleaned = re.sub(r"[/\\:]", " ", source)
    cleaned = "".join(
        " " if character.isspace() or not character.isprintable() else character
        for character in cleaned
    )
    cleaned = " ".join(cleaned.split()).lstrip(". ")
    return cleaned[:_NAME_LENGTH].rstrip(". ") or "Narration"


def _publish(temporary: Path, folder: Path, stem: str, extension: str) -> Path:
    """Move a finished Export to the first free name in `folder`.

    Never over an existing file: a reader's earlier Export of the same
    Source is theirs to keep. The finished file is hard-linked to the name,
    which claims it and publishes it in one atomic step, so the name never
    exists holding less than the whole Export, even if the Engine is killed.
    """
    for attempt in range(1, _NAME_ATTEMPTS + 1):
        suffix = "" if attempt == 1 else f" {attempt}"
        candidate = folder / f"{stem}{suffix}.{extension}"
        try:
            os.link(temporary, candidate)
        except FileExistsError:
            continue
        return candidate
    raise AudioEncodingError("Every name for this Export is already taken")


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
        folder: Path,
        *,
        encoders: dict[ExportFormat, Encoder] | None = None,
    ) -> None:
        self._storage = storage
        self._worker = worker
        self._folder = folder
        self._encoders = encoders if encoders is not None else export_encoders()
        self._state: Observable[ExportSnapshot] = Observable(
            _idle(), poll_seconds=_POLL_SECONDS
        )
        self._lock = self._state.lock
        self._thread: threading.Thread | None = None
        self._closed = threading.Event()

    def start(self, narration_id: str, export_format: ExportFormat) -> None:
        """Accept an Export and return: the work happens on the thread.

        Raises before accepting — never mid-Export — so the button press is
        either a clean rejection the client can explain or a job that is
        now running. `KeyError` for an unknown Narration, `ExportInProgress`
        when one is already under way, and `RuntimeError` once the Engine is
        shutting down.
        """
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
                args=(plan, export_format),
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
        the process exit; it writes only to its own staging folder, so a
        half-written Export can never be mistaken for a finished one.
        """
        self._closed.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=_SHUTDOWN_GRACE_SECONDS)

    def _run(self, plan: NarrationPlan, export_format: ExportFormat) -> None:
        try:
            pcm, sample_rate = self._assemble(plan)
            if not len(pcm):
                raise AudioEncodingError("The Narration produced no audio to export")
            with self._lock:
                self._state.update(phase="encoding")
            self._encode(pcm, sample_rate, file_stem(plan.source), export_format)
        except Exception as error:
            # The type and not the traceback: an OSError from the rename
            # or a CalledProcessError from afconvert quotes the destination,
            # whose name is the Source's first words, and the log file never
            # holds a Source.
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
        stem: str,
        export_format: ExportFormat,
    ) -> None:
        """Encode once, into a staging folder inside the audio folder, then
        publish.

        Publishing is atomic, so the reader never finds a truncated file
        under an Export's name — one interrupted by a crash or a shutdown
        leaves a dot-folder they can delete, not a broken recording they
        might send to someone. The staging folder is named `.nosync` because
        a reader may sync Documents to iCloud, which leaves everything under
        such a folder on this Mac, so neither a half-written Export nor an
        encoder's own intermediate (afconvert's input WAV sits beside its
        output) is ever uploaded. A `.nosync` file name alone would not do:
        the encoder derives its intermediate's name by appending to it.
        """
        folder = self._folder
        folder.mkdir(parents=True, exist_ok=True)
        staging = folder / f".readily-export-{uuid.uuid4().hex}.nosync"
        staging.mkdir()
        try:
            temporary = staging / f"export.{export_format}"
            self._encoders[export_format](pcm, sample_rate, temporary)
            if not temporary.is_file():
                raise AudioEncodingError("The Export encoder produced no file")
            _publish(temporary, folder, stem, export_format)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
