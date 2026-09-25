"""The process-long serial generation worker required by ADR 0002 §3."""

import logging
import queue
import secrets
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, replace
from fractions import Fraction
from typing import Literal, Protocol, TypedDict

import numpy as np

from readily_engine.alignment import Aligner
from readily_engine.audio import (
    AudioOutputUnavailable,
    FloatPcm,
    validate_playback_speed,
)
from readily_engine.catalog import CatalogEntry
from readily_engine.catalog.recipes import Mode, UnqualifiedRecipe, resolve_simple
from readily_engine.chunking import Boundary, chunk
from readily_engine.generation import GenerationRecord, Synthesizer
from readily_engine.narration.admission import require_simple_plan
from readily_engine.narration.measure import (
    cached_block,
    measure,
    recover_lengths,
    trimmed_audio,
)
from readily_engine.narration.preparation import Cancelled as _Cancelled
from readily_engine.narration.preparation import Preparation
from readily_engine.narration.readiness import Progress, Readiness
from readily_engine.narration.stream import assemble
from readily_engine.narration.timeline import (
    block_at,
    known_length,
    locate_source,
    locate_time,
    walk,
)
from readily_engine.narration.words import block_timings, word_route, words_for
from readily_engine.storage.history import NarrationStatus, SynthesisSettings
from readily_engine.storage.segments import StoredAudio
from readily_engine.storage.storage import (
    DeletionResult,
    NarrationPlan,
    NarrationStorage,
    PlannedSegment,
)
from readily_engine.timings import SEEK_LEAD_SECONDS
from readily_engine.wire import WIRE_VERSION, Observable, WireError, wire_error

logger = logging.getLogger(__name__)


def _generation_gap() -> WireError:
    return wire_error(
        "generation_gap",
        "Some words could not be read after two attempts. "
        "The gaps are marked in the text.",
    )


# How long shutdown waits for the worker to leave whatever native call it is
# in. Cancellation is observed between Blocks, so the wait has to cover one
# whole synthesis, and the slowest Architecture's single-chunk utterance measured
# 11-13s. A worker policy, not an Architecture member: it bounds
# a thread join, which has no text to budget by (ADR 0014). Past this the
# caller stops unwinding rather than waiting longer; see `close`.
SHUTDOWN_GRACE_SECONDS = 15

# How often `events` re-reads the snapshot. Narration state drives a position
# readout, so it is the fastest stream the Engine publishes.
_POLL_SECONDS = 0.05

_CHECKPOINT_SECONDS = 1.0

GENERATION_THREAD = "readily-generation"

# How often a blocked Export re-checks that the worker is still alive. It is
# waiting on a whole synthesis behind a whole Narration, so this only bounds
# how long shutdown leaves the Export thread parked.
_FILL_POLL_SECONDS = 0.1

# Ready listening time ahead of the source playhead, accounting for the
# current stretch rate and authored pause compression without changing PCM.
LOOKAHEAD_SECONDS = 60.0

# ADR 0002 §8: one retry per Block, then the Block becomes a recorded gap and
# playback continues. More retries would trade the listener's continuity for
# audio that already failed twice.
SYNTHESIS_ATTEMPTS = 2

TakeAction = Literal["reroll", "A", "B"]

NarrationPhase = Literal["idle", "preparing", "playing", "paused", "finished", "failed"]

# The phases in which a Narration is the active one — stoppable, seekable,
# and persisted as interrupted rather than left as-is on shutdown.
_ACTIVE_PHASES = frozenset({"preparing", "playing", "paused"})


class Diagnostics(Progress):
    """The `diagnostics` wire object: Readiness's Progress plus the device counters."""

    ringStarvations: int
    deviceUnderflows: int


class NarrationSnapshot(TypedDict):
    """The frozen v1 narration snapshot (`docs/wire.md`), as sent."""

    version: int
    phase: NarrationPhase
    narrationId: str | None
    modelId: str
    voiceId: str
    positionSec: float
    # Loudness of what the device is playing right now, 0-1; live like
    # `positionSec`, so the shell's orb can breathe with the voice.
    level: float
    totalSec: float
    speed: float
    diagnostics: Diagnostics
    generationBehind: bool
    lateCallbacks: int
    error: WireError | None


@dataclass(frozen=True)
class NarrationRequest:
    model: str
    input: str
    voice: str
    mode: Mode = "advanced"


@dataclass(frozen=True)
class VoiceModel:
    """A Catalog entry and the lane that generates its Segments."""

    synthesizer: Synthesizer
    entry: CatalogEntry
    prewarm: Callable[[], bool]


class Playback(Protocol):
    """Playback keyed by the worker's generation counter: the worker is the
    single source of truth for which Narration may be heard, so cancellation
    is one number rather than two clocks that can disagree."""

    late_callbacks: int
    ring_starvations: int
    underflows: int

    def feed(
        self, pcm: FloatPcm, sample_rate: int, generation: int, *, pause_frames: int = 0
    ) -> float: ...

    def drain(self, generation: int) -> float: ...

    def set_speed(self, speed: float) -> None: ...

    def stop(self, generation: int) -> None: ...

    def pause(self, generation: int) -> None: ...

    def unpause(self, generation: int) -> None: ...

    def position(self, generation: int) -> float: ...

    def level(self, generation: int) -> float: ...

    def starving(self, generation: int) -> bool: ...


@dataclass(frozen=True)
class Job:
    generation: int
    plan: NarrationPlan
    resume: bool
    source_offset: int | None = None
    fade_in: bool = False
    # Freeze the playhead the moment audio is ready, so the Narration lands
    # in `paused` rather than `playing`. Synthesis runs ahead regardless.
    paused: bool = False


@dataclass(frozen=True)
class Feed:
    """One Narration handed from the scheduler thread to the feeder thread.

    `first` is the Block the walk opens on; the playhead it starts inside
    is the plan's own, so the feeder derives it rather than being told.
    """

    job: Job
    ready: Preparation
    first: int


@dataclass(frozen=True)
class Prewarm:
    """Boot-time model load + dummy inference, run like any other job so
    synthesis never leaves the worker's one thread."""


class Fill:
    """One Block Export needs synthesized, queued like any other job.

    Export never preempts the Narration being heard: a Fill waits its turn
    behind whatever the worker is already doing, and the Exporter's own
    thread blocks on `done` until the worker answers. Mutable, because the
    worker writes the answer back into the request the caller is holding.
    """

    def __init__(self, plan: NarrationPlan, segment: PlannedSegment) -> None:
        self.plan = plan
        self.segment = segment
        self.audio: StoredAudio | None = None
        self.done = threading.Event()


class GenerationWorker:
    """Runs every synthesis job on one thread for the Engine's full lifetime.

    A new Narration invalidates older work before it enters the audio buffer.
    The worker itself is never replaced: MLX tears down a per-thread compile
    cache when a generation thread exits, and that path has been observed
    segfaulting the process.
    """

    def __init__(
        self,
        models: Mapping[str, VoiceModel],
        playback: Playback,
        storage: NarrationStorage,
        *,
        default_model: str,
        default_voice: str,
        checkpoint_seconds: float = _CHECKPOINT_SECONDS,
        aligners: Mapping[str, Aligner] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._models = dict(models)
        self._aligners = dict(aligners or {})
        self._default_model = default_model
        self._playback = playback
        self._storage = storage
        self._speed = validate_playback_speed(storage.playback_speed())
        playback.set_speed(self._speed)
        self._checkpoint_seconds = checkpoint_seconds
        self._clock = clock
        self._readiness = Readiness(clock)
        self._jobs: queue.Queue[Job | Prewarm | Fill | None] = queue.Queue()
        self._feeds: queue.Queue[Feed | None] = queue.Queue()
        # Set once the worker thread has been asked to stop and waited for.
        # A Fill queued behind the shutdown sentinel is never consumed, so
        # `fill` watches this rather than blocking an Export thread forever.
        self._closed = threading.Event()
        self._state: Observable[NarrationSnapshot] = Observable(
            NarrationSnapshot(
                version=WIRE_VERSION,
                phase="idle",
                narrationId=None,
                modelId=default_model,
                voiceId=default_voice,
                positionSec=0.0,
                totalSec=0.0,
                speed=self._speed,
                generationBehind=False,
                diagnostics=self._diagnostics(0.0),
                level=0.0,
                lateCallbacks=0,
                error=None,
            ),
            poll_seconds=_POLL_SECONDS,
        )
        # One lock for the generation counter and the snapshot alike: a
        # caller that takes a generation and publishes it in two steps lets
        # a racing caller interleave, and the loser's stop then silences the
        # winner's Narration.
        self._lock = self._state.lock
        # Serializes every durable status/playhead write with the check
        # that its generation is still the current one. Without it a
        # `PLAYING` computed just before a Stop lands just after Stop's
        # `STOPPED`, and the History claims a Narration is playing for as
        # long as the Engine stays up. Always taken *outside* `_lock`.
        self._durable_lock = threading.Lock()
        self._generation = 0
        self._resume_base_sec = 0.0
        # The plan a seek re-queues. Guarded by `_lock`; staleness is safe
        # because seek is gated on an active phase, and only `_admit` moves
        # the phase back into one.
        self._active_plan: NarrationPlan | None = None
        # Reserves a terminal transition while its History write is in
        # flight. Controls can answer without waiting on SQLite, but cannot
        # overwrite the outcome or expose its phase before durability returns.
        self._terminal_generation: int | None = None
        self._checkpoint_target: tuple[int, str, float] | None = None
        self._checkpoint_stop = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=GENERATION_THREAD,
        )
        self._checkpoint_thread = threading.Thread(
            target=self._checkpoint_loop,
            daemon=True,
            name="readily-checkpoint",
        )
        self._feeder_thread = threading.Thread(
            target=self._feed_loop, daemon=True, name="readily-feeder"
        )
        self._feeder_thread.start()
        self.thread.start()
        self._checkpoint_thread.start()

    def prewarm(self) -> None:
        """Queue a boot-time prewarm; observable state is untouched."""
        self._jobs.put(Prewarm())

    def start(self, request: NarrationRequest) -> str:
        """Create a durable manifest, replace the active Narration, and queue it."""
        model = self._models.get(request.model)
        if model is None:
            return self._fail_unknown_model(request)
        controls = (
            resolve_simple(model.entry, request.voice)
            if request.mode == "simple"
            else self._storage.effective_controls(model.entry, request.voice)
        )
        chunked = chunk(request.input, controls.entry.tunables)
        if not chunked.blocks:
            # Nothing speakable survived chunking. Writing the manifest
            # anyway would leave a permanent History row with no Blocks:
            # it can never play, never resume, and never be explained.
            raise ValueError("A Narration requires speakable text")
        plan = self._storage.create(
            chunked,
            SynthesisSettings(
                request.model,
                model.entry.version,
                request.voice,
                self._source_speed(),
                pause_policy=controls.entry.tunables.pause_policy,
                prepare_first=controls.prepare_first,
            ),
            controls.entry,
            seed=controls.seed,
        )
        return self._admit(plan, resume=False)

    def set_speed(self, speed: float) -> None:
        """Change and persist the speed without replacing the active Narration."""
        with self._durable_lock:
            self._storage.set_playback_speed(speed)
            self._apply_speed(speed)

    def _apply_speed(self, speed: float) -> None:
        with self._lock:
            self._speed = speed
            self._playback.set_speed(speed)
            self._state.update(speed=speed)

    def _source_speed(self) -> float:
        with self._lock:
            return self._speed

    def resume(
        self, narration_id: str, *, mode: Mode = "advanced", paused: bool = False
    ) -> str:
        """Resume an unfinished Narration, or replay a finished one.

        `paused` opens it with the playhead frozen at the resume point:
        the Narration becomes the active one, ready for `play`, without a
        sound.
        """
        with self._durable_lock:
            if mode == "simple":
                plan = self._storage.plan(narration_id)
                model = self._models.get(plan.settings.model_id)
                if model is None:
                    raise UnqualifiedRecipe(
                        "This Narration has no qualified Simple-mode recipe."
                    )
                require_simple_plan(model.entry, plan)
            plan = recover_lengths(self._storage, self._storage.resume(narration_id))
            return self._admit_locked(plan, resume=True, paused=paused)

    def fill(self, plan: NarrationPlan, segment: PlannedSegment) -> StoredAudio | None:
        """Synthesize and store one evicted Block, for Export.

        Blocks the calling thread — the Exporter's, never a request
        thread — until the one generation thread reaches it. That wait is
        the design: ADR 0002 §3 allows exactly one synthesizer on exactly
        one process-long thread, so an Export that needs synthesis has to
        queue behind the Narration the listener is hearing rather than
        interrupt it. `None` means the Block could not be produced.
        """
        request = Fill(plan, segment)
        if self._closed.is_set():
            return None
        self._jobs.put(request)
        while not request.done.wait(timeout=_FILL_POLL_SECONDS):
            if self._closed.is_set():
                return None
        return request.audio

    def cached(
        self, plan: NarrationPlan, segment: PlannedSegment
    ) -> StoredAudio | None:
        """Audio this Block replays without synthesis, for Export's gap check.

        Recording a length it had to measure is fine; rekeying belongs to
        the generation thread. Storage reports recordless audio absent.
        """
        return trimmed_audio(self._storage, plan, segment)

    def _rekeyed(
        self, model: VoiceModel, plan: NarrationPlan, segment: PlannedSegment
    ) -> tuple[PlannedSegment, GenerationRecord]:
        """Keep cached audio, or rekey a missing Segment onto the loaded model."""
        record = segment.generation
        if record is None or (
            record.catalog_version != model.entry.version
            and not self._storage.has_verified_audio(segment)
        ):
            voice_id = plan.settings.voice_id
            controls = self._storage.effective_controls(model.entry, voice_id)
            record = GenerationRecord.for_entry(
                controls.entry, voice_id, segment.text, seed=controls.seed
            )
            segment = self._storage.rekey_segment(plan.id, segment, record)
        elif not self._storage.has_verified_audio(segment):
            redraw = record.redraw()
            rescued = replace(segment, key=redraw.key, generation=redraw)
            if self._storage.has_verified_audio(rescued):
                record = redraw
                segment = self._storage.rekey_segment(plan.id, segment, record)
        return segment, record

    def _fill(self, request: Fill) -> None:
        """Run one Fill on the generation thread and answer its waiter."""
        try:
            plan = request.plan
            model = self._models[plan.settings.model_id]
            # A Fill runs on this thread, so it cannot interleave with a
            # generation whose plan would need the new key. The rekeyed
            # Block goes back on the request only so a failure names the
            # Block it failed on.
            request.segment, record = self._rekeyed(model, plan, request.segment)
            segment = cached_block(self._storage, plan, request.segment)
            if segment is None:
                # Never cancelled: a Fill belongs to an Export, not to a
                # generation, so a Narration starting alongside it must not
                # discard work the Export is blocked on.
                segment = self._produce(
                    model, plan, request.segment, record, lambda: False, None
                )
            if segment is not None:
                request.audio = trimmed_audio(self._storage, plan, segment)
        except Exception:
            logger.exception(
                "Block %d could not be filled for Export", request.segment.ordinal
            )
        finally:
            request.done.set()

    def delete(self, narration_id: str) -> DeletionResult | None:
        """Cancel this Narration if active, then delete its record and audio."""
        with self._durable_lock:
            with self._lock:
                snapshot = self._state.snapshot()
                if (
                    snapshot["narrationId"] == narration_id
                    and snapshot["phase"] in _ACTIVE_PHASES
                ):
                    self._generation += 1
                    self._terminal_generation = None
                    self._checkpoint_target = None
                    self._state.update(
                        phase="idle",
                        narrationId=None,
                        positionSec=0.0,
                        totalSec=0.0,
                        level=0.0,
                        lateCallbacks=self._playback.late_callbacks,
                        error=None,
                    )
                    self._playback.stop(self._generation)
                if (
                    self._active_plan is not None
                    and self._active_plan.id == narration_id
                ):
                    self._active_plan = None
            return self._storage.delete(narration_id)

    def stop(self) -> bool:
        """Cancel current/queued work and silence playback immediately."""
        # Device cancellation is deliberately ahead of durable I/O. A slow
        # PLAYING write may already hold `_durable_lock`; the listener must
        # not hear seconds more audio while Stop waits for SQLite. The later
        # durable section still orders STOPPED after that older write.
        with self._lock:
            if self._terminal_generation == self._generation:
                return False
            if self._state.snapshot()["phase"] not in _ACTIVE_PHASES:
                return False
            outgoing = self._capture_outgoing_locked()
            self._generation += 1
            self._terminal_generation = None
            self._checkpoint_target = None
            self._state.update(
                phase="idle",
                narrationId=None,
                positionSec=0.0,
                totalSec=0.0,
                level=0.0,
                lateCallbacks=self._playback.late_callbacks,
                error=None,
            )
            self._playback.stop(self._generation)
        with self._durable_lock:
            self._persist(outgoing, NarrationStatus.STOPPED)
        return True

    def pause(self) -> bool:
        """Freeze the playhead without abandoning the Narration.

        Durable status is untouched: a paused Narration is still the
        playing one, so a crash while paused resumes from the pause point
        like any other interruption.
        """
        with self._lock:
            if self._terminal_generation == self._generation:
                return False
            if self._state.snapshot()["phase"] != "playing":
                return False
            self._playback.pause(self._generation)
            self._state.update(phase="paused")
        return True

    def play(self) -> bool:
        """Release a pause exactly where it froze."""
        with self._lock:
            if self._terminal_generation == self._generation:
                return False
            if self._state.snapshot()["phase"] != "paused":
                return False
            self._playback.unpause(self._generation)
            self._state.update(phase="playing")
        return True

    def select_take(self, narration_id: str, ordinal: int, action: TakeAction) -> bool:
        """Select a Block's take, then resume the Narration at that Block.

        Resolving the take reads the store and may decode audio, so like
        `seek` it runs before any Engine lock is taken.
        """
        try:
            plan = self._storage.plan(narration_id)
        except KeyError:
            return False
        if not 0 <= ordinal < len(plan.segments):
            return False
        record = plan.segments[ordinal].generation
        if record is None or plan.settings.model_id not in self._models:
            return False
        if action == "reroll":
            seed = secrets.randbits(32)
            while seed == record.rng_seed:
                seed = secrets.randbits(32)
            record = replace(record, seed=seed)
        else:
            takes = self._storage.block_takes(narration_id, ordinal)
            if action not in takes:
                return False
            record = takes[action]
        plan = recover_lengths(self._storage, plan, through=ordinal - 1)
        if any(
            not part.measured and not plan.is_gap(part.ordinal)
            for part in plan.segments[:ordinal]
        ):
            return False
        plan = self._storage.select_take(narration_id, ordinal, record)
        plan = recover_lengths(self._storage, plan, through=ordinal)
        position = float(walk(plan, ordinal).position)
        with self._durable_lock:
            self._admit_locked(
                replace(plan, playhead_sec=position),
                resume=True,
                source_offset=plan.segments[ordinal].source_start,
            )
        return True

    def seek(self, source_offset: int) -> bool:
        """Move the playhead by replacing the generation, resume-style.

        The active plan is re-queued at the target, so cached Segments
        decode from the store rather than re-synthesize, audio already
        queued for the old position is discarded, and a Narration paused
        when the seek arrived starts playing at the target.
        """
        return self._seek(source_offset=source_offset)

    def seek_time(self, position_sec: float) -> bool:
        """Seek to an exact source time, clamped to the measured audio prefix."""
        return self._seek(position_sec=position_sec)

    def _seek(
        self, *, source_offset: int | None = None, position_sec: float = 0.0
    ) -> bool:
        with self._lock:
            if (
                self._terminal_generation == self._generation
                or self._state.snapshot()["phase"] not in _ACTIVE_PHASES
                or self._active_plan is None
            ):
                return False
            narration_id = self._active_plan.id
            heard = self._generation
        plan = self._storage.plan(narration_id)
        if source_offset is not None:
            segment = block_at(plan, source_offset)
            if segment is None:
                return False
            plan = recover_lengths(self._storage, plan, through=segment.ordinal)
            target = self._resolve_target(plan, segment.ordinal, source_offset)
            if target is None:
                return False
        else:
            plan = recover_lengths(self._storage, plan)
            count = 0
            for part in plan.segments:
                if not plan.is_gap(part.ordinal) and (
                    part.generation is None or not part.measured
                ):
                    break
                count += 1
            timeline = walk(plan, count)
            timeline.flush()
            target = min(max(0.0, position_sec), float(timeline.position))
        with self._lock:
            if (
                self._generation != heard
                or self._terminal_generation == heard
                or self._state.snapshot()["phase"] not in _ACTIVE_PHASES
            ):
                return False
            self._generation += 1
            generation = self._generation
            self._terminal_generation = None
            # The assembled audio outlives the seek — nothing is unassembled
            # by moving through it — so the length a client hears stays
            # where it was rather than collapsing and regrowing.
            self._state.update(
                phase="preparing",
                positionSec=target,
                level=0.0,
                lateCallbacks=self._playback.late_callbacks,
                error=None,
            )
            self._playback.stop(generation)
        if not self._commit_playhead(generation, plan, target):
            return False
        self._jobs.put(
            Job(
                generation,
                replace(plan, playhead_sec=target),
                resume=True,
                source_offset=source_offset,
                fade_in=True,
            )
        )
        return True

    def _resolve_target(
        self, plan: NarrationPlan, ordinal: int, source_offset: int
    ) -> float | None:
        """Where on the timeline listening from `source_offset` should start.

        The word holding the offset, led by its provenance's margin, or the
        Block's own start when its words are not known yet. None while a
        Block before the target is unrecorded or unmeasured, because the
        walk cannot place the target until then.
        """
        segment = plan.segments[ordinal]
        start = locate_source(
            plan, segment, invalid=lambda part: part.generation is None
        )
        if start is None:
            through = plan.segments[: ordinal + 1]
            if any(part.generation is None for part in through) or any(
                not part.measured and not plan.is_gap(part.ordinal)
                for part in through[:-1]
            ):
                return None
            start = walk(plan, ordinal).position
        start = float(start)
        word = next(
            (
                word
                for word in words_for(self._storage, plan, segment, start)
                if word.source_start <= source_offset < word.source_end
            ),
            None,
        )
        if word is None:
            return start
        return max(start, word.start_sec - SEEK_LEAD_SECONDS[word.provenance])

    def _commit_playhead(
        self, generation: int, plan: NarrationPlan, target: float
    ) -> bool:
        """Park `generation` at `target`, in memory and in the store.

        Seeking backwards lowers the stored playhead, which is what makes a
        seek survive a Stop or a crash, so the write happens under the
        durable lock and only while `generation` is still the one being
        heard. False means a Stop or a newer Narration raced it.
        """
        with self._durable_lock:
            with self._lock:
                if (
                    self._generation != generation
                    or self._terminal_generation == generation
                ):
                    return False
                self._resume_base_sec = target
                self._checkpoint_target = (generation, plan.id, target)
                self._state.update(positionSec=target)
            self._storage.checkpoint(plan.id, target)
        return True

    def snapshot(self) -> NarrationSnapshot:
        with self._lock:
            snapshot = self._state.snapshot()
            if snapshot["phase"] in _ACTIVE_PHASES:
                snapshot["positionSec"] = (
                    self._resume_base_sec + self._playback.position(self._generation)
                )
                snapshot["level"] = self._playback.level(self._generation)
            snapshot["generationBehind"] = snapshot[
                "phase"
            ] == "playing" and self._readiness.behind(
                snapshot["positionSec"],
                self._speed,
                LOOKAHEAD_SECONDS,
                starving=self._playback.starving(self._generation),
            )
            snapshot["diagnostics"] = self._diagnostics(snapshot["positionSec"])
            return snapshot

    def _diagnostics(self, position: float) -> Diagnostics:
        return {
            **self._readiness.progress(position, self._speed),
            "ringStarvations": self._playback.ring_starvations,
            "deviceUnderflows": self._playback.underflows,
        }

    def events(self) -> AsyncIterator[NarrationSnapshot]:
        """Yield changed snapshots, plus a periodic snapshot as an SSE heartbeat.

        The playhead is patched in live rather than published as a revision
        per tick: `positionSec` moves with every audio callback, and the UI's
        position readout reads it from here — the Engine's playhead is the
        only clock (ADR 0002).
        """

        return self._state.events(lambda _snapshot: self.snapshot())

    def close(self) -> bool:
        """End the worker only as the containing Engine process shuts down.

        Returns whether the worker thread actually stopped. `False` means
        synthesis is still inside a native call: nothing that follows may
        tear down the audio device or let the interpreter finalize, because
        finalizing under a live MLX/ONNX call is the segfault class this
        worker exists to avoid.
        """
        with self._durable_lock:
            with self._lock:
                outgoing = self._capture_outgoing_locked()
                self._generation += 1
                self._terminal_generation = None
                self._checkpoint_target = None
                self._playback.stop(self._generation)
            self._persist(outgoing, NarrationStatus.INTERRUPTED)
        self._jobs.put(None)
        self._feeds.put(None)
        self._checkpoint_stop.set()
        self.thread.join(timeout=SHUTDOWN_GRACE_SECONDS)
        self._checkpoint_thread.join(timeout=SHUTDOWN_GRACE_SECONDS)
        self._feeder_thread.join(timeout=SHUTDOWN_GRACE_SECONDS)
        # After the join, so a Fill released here is one the worker will
        # certainly never reach: everything queued behind the sentinel is
        # dead work, and an Export waiting on it has to be told so.
        self._closed.set()
        self._release_pending_fills()
        return not self.thread.is_alive() and not self._feeder_thread.is_alive()

    def _release_pending_fills(self) -> None:
        while True:
            try:
                pending = self._jobs.get_nowait()
            except queue.Empty:
                return
            if isinstance(pending, Fill):
                pending.done.set()

    def _fail_unknown_model(self, request: NarrationRequest) -> str:
        """Report a model reference the worker cannot resolve."""
        narration_id = str(uuid.uuid4())
        with self._durable_lock:
            with self._lock:
                outgoing = self._capture_outgoing_locked()
                self._generation += 1
                self._terminal_generation = None
                self._checkpoint_target = None
                self._state.update(
                    phase="failed",
                    narrationId=narration_id,
                    modelId=request.model,
                    voiceId=request.voice,
                    positionSec=0.0,
                    totalSec=0.0,
                    level=0.0,
                    lateCallbacks=self._playback.late_callbacks,
                    error=wire_error(
                        "generation_failed",
                        "The Engine could not generate this Narration.",
                    ),
                )
                self._playback.stop(self._generation)
            self._persist(outgoing, NarrationStatus.STOPPED)
        return narration_id

    def _admit(self, plan: NarrationPlan, *, resume: bool) -> str:
        with self._durable_lock:
            return self._admit_locked(plan, resume=resume)

    def _admit_locked(
        self,
        plan: NarrationPlan,
        *,
        resume: bool,
        source_offset: int | None = None,
        paused: bool = False,
    ) -> str:
        """Replace the active Narration while `_durable_lock` is held.

        Resume also validates eligibility under this lock, making its
        STOPPED/INTERRUPTED/FINISHED read and PREPARING write one admission
        step. Finished Narrations arrive with a zero playhead for replay.
        """
        resume_base = plan.playhead_sec if resume else 0.0
        with self._lock:
            outgoing = self._capture_outgoing_locked()
            self._generation += 1
            generation = self._generation
            self._terminal_generation = None
            self._active_plan = plan
            self._readiness = Readiness(self._clock)
            self._resume_base_sec = resume_base
            self._checkpoint_target = (generation, plan.id, resume_base)
            self._state.update(
                phase="preparing",
                narrationId=plan.id,
                modelId=plan.settings.model_id,
                voiceId=plan.settings.voice_id,
                positionSec=resume_base,
                totalSec=0.0,
                level=0.0,
                lateCallbacks=self._playback.late_callbacks,
                error=None,
            )
            # Under the lock that numbered this generation: two callers
            # racing here would otherwise stop playback in the opposite
            # order they took their generations, and the loser's stop
            # would silence the winner's Narration.
            self._playback.stop(generation)
        self._persist(outgoing, NarrationStatus.STOPPED)
        self._storage.set_status(
            plan.id,
            NarrationStatus.PREPARING,
            playhead_sec=resume_base if resume else None,
        )
        self._jobs.put(Job(generation, plan, resume, source_offset, paused=paused))
        return plan.id

    def _capture_outgoing_locked(self) -> tuple[str, float] | None:
        snapshot = self._state.snapshot()
        narration_id = snapshot["narrationId"]
        if snapshot["phase"] not in _ACTIVE_PHASES or narration_id is None:
            return None
        playhead = self._resume_base_sec + self._playback.position(self._generation)
        return (narration_id, playhead)

    def _persist(
        self,
        outgoing: tuple[str, float] | None,
        status: NarrationStatus,
        *,
        total_duration_sec: float | None = None,
    ) -> None:
        if outgoing is None:
            return
        narration_id, playhead = outgoing
        try:
            self._storage.checkpoint(narration_id, playhead)
            self._storage.set_status(
                narration_id,
                status,
                playhead_sec=playhead,
                total_duration_sec=total_duration_sec,
            )
        except KeyError:
            # The Narration was deleted between the snapshot this outcome
            # was captured from and this write. There is no row left to
            # record a playhead on, and the caller asked for it to be gone
            # anyway — so the Stop, close, or terminal transition that got
            # here has still done everything it was asked to do. Raising
            # would surface a successful Stop to the client as a 500.
            logger.info(
                "Narration %s was deleted before its outcome could be recorded",
                narration_id,
            )

    def _transition_terminal(
        self,
        generation: int,
        narration_id: str,
        status: NarrationStatus,
        *,
        position_sec: float | None = None,
        total_duration_sec: float | None = None,
        storage_error_message: str | None = None,
        silence: bool = False,
        **changes: object,
    ) -> float | None:
        """Publish one terminal durable + observable outcome.

        An internal reservation makes controls answer without waiting on
        SQLite while preventing them from overwriting the outcome. The wire
        phase remains unchanged until its matching History write returns, so
        observing a terminal phase still means observing durable state.

        Failure outcomes may be best-effort durable: recording trouble means
        writing to the storage the trouble may have come from, and losing
        that record must not also lose the phase the listener is watching.
        """
        with self._durable_lock:
            with self._lock:
                if generation != self._generation:
                    return None
                playhead = (
                    position_sec if position_sec is not None else self._resume_base_sec
                )
                if position_sec is None:
                    try:
                        playhead += self._playback.position(generation)
                    except Exception:
                        if storage_error_message is None:
                            raise
                        logger.exception("The final playhead could not be read")
                if silence:
                    self._generation += 1
                    generation = self._generation
                    try:
                        self._playback.stop(generation)
                    except Exception:
                        logger.exception("Failed playback could not be silenced")
                self._checkpoint_target = None
                self._terminal_generation = generation
            try:
                self._persist(
                    (narration_id, playhead),
                    status,
                    total_duration_sec=total_duration_sec,
                )
            except Exception:
                if storage_error_message is None:
                    raise
                logger.exception(storage_error_message)
            with self._lock:
                if (
                    generation != self._generation
                    or self._terminal_generation != generation
                ):
                    return None
                self._state.update(
                    positionSec=playhead,
                    level=0.0,
                    lateCallbacks=self._playback.late_callbacks,
                    **changes,
                )
                self._terminal_generation = None
            return playhead

    def _set_status_if_current(
        self, generation: int, narration_id: str, status: NarrationStatus
    ) -> None:
        """Write a durable status that carries no playhead, or none at all."""
        with self._durable_lock:
            if not self._is_current(generation):
                return
            self._storage.set_status(narration_id, status)

    def _is_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation

    def _checkpoint_loop(self) -> None:
        """Persist the playhead about once a second, for the whole process.

        This thread exists so a crash costs at most one second of a
        listener's place, which makes its own durability the point: one
        transient storage error must not be the end of it, and it must
        never be the writer that resurrects a stale position on top of a
        Stop or a backward seek.
        """
        written: tuple[int, float] | None = None
        while not self._checkpoint_stop.wait(timeout=self._checkpoint_seconds):
            try:
                written = self._checkpoint_once(written)
            except Exception:
                logger.exception("Checkpointing the playhead failed")

    def _checkpoint_once(
        self, written: tuple[int, float] | None
    ) -> tuple[int, float] | None:
        """Write the current playhead unless it is stale or unchanged.

        A paused Narration holds one position for as long as the pause
        lasts; rewriting it every second is a disk write per second that
        says nothing new.
        """
        with self._durable_lock:
            with self._lock:
                target = self._checkpoint_target
                if target is None:
                    return None
                generation, narration_id, resume_base = target
                if generation != self._generation:
                    return written
            playhead = resume_base + self._playback.position(generation)
            if written == (generation, playhead):
                return written
            self._storage.checkpoint(narration_id, playhead)
            return (generation, playhead)

    def _run(self) -> None:
        """Consume jobs for the Engine's whole life.

        Nothing a job raises may leave this loop. The worker thread is
        never replaced — MLX tears down a per-thread compile cache when a
        generation thread exits, which has been observed segfaulting the
        process — so a thread that dies here leaves every later Narration
        queued behind a consumer that no longer exists, accepted with a
        202 and stuck in `preparing` until the Engine restarts.
        """
        while True:
            job = self._jobs.get()
            if job is None:
                return
            try:
                self._handle(job)
            except Exception:
                logger.exception("The generation worker could not handle a job")

    def _handle(self, job: Job | Prewarm | Fill) -> None:
        if isinstance(job, Prewarm):
            try:
                self._models[self._default_model].prewarm()
            except Exception:
                logger.exception("Model prewarm failed")
            return
        if isinstance(job, Fill):
            self._fill(job)
            return
        if not self._is_current(job.generation):
            return
        try:
            with self._storage.hold_audio(job.plan.id):
                self._narrate(job)
        except Exception:
            logger.exception("Narration generation failed")
            self._record_failure(job)

    def _record_failure(self, job: Job) -> None:
        """Report a failed Narration at the playhead it reached."""
        self._transition_terminal(
            job.generation,
            job.plan.id,
            NarrationStatus.FAILED,
            storage_error_message="The end of the Narration could not be recorded",
            silence=True,
            phase="failed",
            error=wire_error(
                "generation_failed",
                "The Engine could not generate this Narration.",
            ),
        )

    def _produce(
        self,
        model: VoiceModel,
        plan: NarrationPlan,
        segment: PlannedSegment,
        record: GenerationRecord,
        cancelled: Callable[[], bool],
        progress: Readiness | None,
    ) -> PlannedSegment | None:
        """Synthesize, store, and measure one Block with one retry.

        `None` means both attempts failed and the Block is now a gap; the
        caller records it and playback continues. Cancellation propagates —
        a replaced generation is not a failure of this Block. The retry
        redraws, since the lanes reseed from the record and would replay a
        degenerate draw, and a rescued Block is rekeyed onto the redraw so
        the cached key describes the audio actually requested.
        """
        for attempt in range(SYNTHESIS_ATTEMPTS):
            drawn = record if attempt == 0 else record.redraw()
            if progress is not None:
                progress.attempted(retry=attempt > 0)
            try:
                result = model.synthesizer.generate(drawn)
                if cancelled():
                    raise _Cancelled
                if progress is not None:
                    progress.attempted(cutoffs=result.cutoffs)
                pcm, sample_rate = result.pcm, result.sample_rate
            except _Cancelled:
                raise
            except Exception:
                logger.exception(
                    "Synthesis attempt %d of Block %d failed",
                    attempt + 1,
                    segment.ordinal,
                )
                if cancelled():
                    raise _Cancelled from None
                continue
            route = word_route(model.entry, record)
            try:
                timings = block_timings(
                    route,
                    result.timings,
                    aligner=self._aligners.get(record.word_timing),
                    record=record,
                    plan=plan,
                    segment=segment,
                    pcm=pcm,
                    sample_rate=sample_rate,
                )
            except Exception:
                logger.exception(
                    "Word alignment failed for Narration %s Block %d; using estimated",
                    plan.id,
                    segment.ordinal,
                )
                timings = ()
            if cancelled():
                raise _Cancelled
            if route == "spoken" and not timings:
                logger.warning(
                    "Narration %s Block %s word timing mapping failed; using estimated",
                    plan.id,
                    segment.ordinal,
                )
            if drawn != record:
                segment = self._storage.rekey_segment(plan.id, segment, drawn)
            # Deliberately outside the retry: publication is storage, not
            # synthesis. A Block that was spoken but could not be stored is
            # a failure of the Narration, and swallowing it here would
            # re-synthesize working audio and then record a gap claiming
            # the words were never spoken — while the Narration goes on to
            # report `finished` with no error at all.
            raw = self._storage.store_audio(
                segment.key, pcm, sample_rate, timings=timings
            )
            return measure(self._storage, plan, segment, raw)
        return None

    def _narrate(self, job: Job) -> None:
        """Prepare a contiguous cache prefix independently of device backpressure."""
        plan = job.plan
        model = self._models[plan.settings.model_id]
        skip_until = plan.playhead_sec if job.resume else 0.0
        first = (
            locate_time(plan, skip_until, invalid=lambda part: part.generation is None)
            if job.resume
            else 0
        )
        if job.source_offset is not None:
            first = next(
                part.ordinal
                for part in plan.segments
                if part.source_end > job.source_offset
            )
        ready = Preparation(lambda: self._is_current(job.generation))
        progress = Readiness(self._clock)
        with self._lock:
            if not self._is_current(job.generation):
                return
            self._readiness = progress
        timeline = walk(plan, first)
        # The walk from a seek target starts short of what is assembled. The
        # length a client hears never drops below that until the walk has
        # gone past it; the final publish, after the whole walk, is exact.
        assembled = known_length(plan)
        stored_takes = self._storage.narration_takes(plan.id)
        prepare_first = plan.settings.prepare_first
        if not prepare_first and job.source_offset is None:
            self._feeds.put(Feed(job, ready, first))
        try:
            for planned in plan.segments[first:]:
                if ready.cancelled():
                    raise _Cancelled
                planned, record = self._rekeyed(model, plan, planned)
                segment = cached_block(self._storage, plan, planned)
                generated = segment is None
                if segment is None:
                    while (
                        not prepare_first
                        and progress.seconds_ahead(
                            skip_until + self._playback.position(job.generation),
                            self._source_speed(),
                        )
                        >= LOOKAHEAD_SECONDS
                    ):
                        if ready.cancelled():
                            raise _Cancelled
                        ready.wait_finished(0.05)
                    if ready.cancelled():
                        raise _Cancelled
                    progress.preparing(planned.ordinal)
                    with progress.generating():
                        segment = self._produce(
                            model, plan, planned, record, ready.cancelled, progress
                        )
                progress.preparing(None)
                boundary = Boundary(planned.boundary)
                if segment is None:
                    if not self._storage.record_gap(
                        plan.id, planned, "generation_failed"
                    ):
                        logger.debug(
                            "dropped a gap for a Block that was rerolled: "
                            "%s ordinal %d",
                            plan.id,
                            planned.ordinal,
                        )
                    timeline.gap(boundary)
                else:
                    seam = timeline.add(
                        segment.frame_count, segment.sample_rate, boundary
                    )
                    progress.add(
                        start=float(seam.start),
                        pause=(
                            seam.lead_frames / segment.sample_rate
                            if seam.lead_is_pause
                            else 0.0
                        ),
                        seconds=segment.frame_count / segment.sample_rate,
                        generated=generated,
                    )
                    timings = self._storage.timings(segment)
                    provenance = timings[0].provenance if timings else "estimated"
                    takes = stored_takes.get(segment.ordinal, {})
                    produced = segment.generation
                    progress.block(
                        float(seam.start),
                        float(timeline.position),
                        {
                            "ordinal": segment.ordinal,
                            "recordHash": produced.key,
                            "seed": produced.rng_seed,
                            "cacheHit": not generated,
                            "wordTiming": provenance,
                            "supportModel": produced.word_timing
                            if provenance == "matched"
                            else None,
                            "take": "B" if takes.get("B") == produced else "A",
                            "hasComparison": "B" in takes,
                        },
                    )
                progress.advance(float(timeline.position))
                if planned.ordinal == first and job.source_offset is not None:
                    plan = self._storage.plan(plan.id)
                    target = self._resolve_target(plan, first, job.source_offset)
                    if target is None:
                        raise RuntimeError("Prepared seek target has no timeline start")
                    skip_until = target
                    plan = replace(plan, playhead_sec=skip_until)
                    job = replace(job, plan=plan)
                    if not self._commit_playhead(job.generation, plan, skip_until):
                        raise _Cancelled
                    if not prepare_first:
                        self._feeds.put(Feed(job, ready, first))
                self._update_if_current(
                    job.generation,
                    totalSec=max(float(timeline.position), assembled),
                )
                ready.publish(
                    planned if segment is None else segment, gap=segment is None
                )
            timeline.flush()
            progress.finish(float(timeline.position))
            self._update_if_current(job.generation, totalSec=float(timeline.position))
            if prepare_first:
                self._feeds.put(Feed(job, ready, first))
            # Preserve Export's ordering behind the Narration being heard.
            while not ready.wait_finished(0.05):
                if ready.cancelled():
                    raise _Cancelled
        except _Cancelled:
            pass
        finally:
            progress.preparing(None)
            ready.abort()

    def _feed_loop(self) -> None:
        """Own assembly and device writes on one process-long feeder thread."""
        while True:
            feed = self._feeds.get()
            if feed is None:
                return
            job, ready = feed.job, feed.ready
            try:
                if not ready.cancelled():
                    self._feed_blocks(job, ready, feed.first)
            except _Cancelled:
                pass
            except AudioOutputUnavailable:
                ready.abort()
                self._park_on_lost_output(job)
            except Exception:
                ready.abort()
                logger.exception("Narration playback failed")
                self._record_failure(job)
            finally:
                ready.finish()

    def _park_on_lost_output(self, job: Job) -> None:
        """Stop a Narration the output device stopped accepting.

        Durable status is INTERRUPTED, not FINISHED: nothing past the
        playhead was ever audible, and interrupted is a status Resume
        takes. The playhead is where the device actually stopped, because
        `position` freezes on the last frame it handed over — which is
        exactly what racing to the end of the plan would have thrown away.

        Playback refreshes the stream before raising, so the next Narration
        gets a fresh output without the worker calling `drain` on a device
        it already knows did not play this one.
        """
        logger.error("The output device was lost; the Narration is interrupted")
        self._transition_terminal(
            job.generation,
            job.plan.id,
            NarrationStatus.INTERRUPTED,
            storage_error_message="The interrupted Narration could not be recorded",
            phase="failed",
            error=wire_error(
                "audio_output_unavailable",
                "The Engine lost the audio output device.",
            ),
        )

    def _prepared_audio(
        self, ready: Preparation, plan: NarrationPlan, generation: int
    ) -> StoredAudio | None:
        """The next published Block's trimmed audio, or None where it gapped."""
        prepared = ready.take()
        if prepared.gap:
            self._update_if_current(generation, error=_generation_gap())
            return None
        audio = trimmed_audio(self._storage, plan, prepared.segment)
        if audio is None:
            raise RuntimeError("A prepared Segment disappeared before playback")
        return audio

    def _feed_blocks(self, job: Job, ready: Preparation, first: int) -> None:
        """Feed everything past the playhead, then finish the Narration."""
        generation = job.generation
        plan = job.plan
        skip = Fraction(plan.playhead_sec) if job.resume else Fraction()
        playing = False
        end = skip
        try:
            for piece in assemble(
                plan,
                lambda _segment: self._prepared_audio(ready, plan, generation),
                first=first,
                start=skip,
            ):
                if ready.cancelled():
                    return
                end = piece.end
                if len(piece.pcm) == 0:
                    continue
                if not playing:
                    if job.fade_in or job.source_offset is not None:
                        fade = min(len(piece.pcm), round(0.015 * piece.sample_rate))
                        pcm = piece.pcm.copy()
                        pcm[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
                        piece = replace(piece, pcm=pcm)
                    playing = True
                    self._set_status_if_current(
                        generation, plan.id, NarrationStatus.PLAYING
                    )
                    if job.paused:
                        # Pause before the first frame is fed: the device
                        # then holds the ring, and `play` releases it here.
                        self._playback.pause(generation)
                        self._update_if_current(generation, phase="paused")
                    else:
                        self._update_if_current(generation, phase="playing")
                self._playback.feed(
                    piece.pcm,
                    piece.sample_rate,
                    generation,
                    pause_frames=piece.pause_frames,
                )
        except _Cancelled:
            return
        if ready.cancelled():
            return
        total = float(end)
        if not playing:
            self._update_if_current(generation, phase="playing")
        self._playback.drain(generation)
        if ready.cancelled():
            return
        self._transition_terminal(
            generation,
            plan.id,
            NarrationStatus.FINISHED,
            position_sec=total,
            total_duration_sec=total,
            phase="finished",
            totalSec=total,
            error=(
                _generation_gap() if self._storage.plan(plan.id).gap_ordinals else None
            ),
        )

    def _update_if_current(self, generation: int, **changes: object) -> None:
        with self._lock:
            if generation == self._generation:
                self._state.update(**changes)
