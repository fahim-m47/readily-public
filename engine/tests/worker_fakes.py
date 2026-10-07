"""Doubles the Narration worker tests share: a synthesizer, a playback
device, and a storage wrapper that record what the worker asked of them.

Kept beside `storage_fakes` rather than in any one test module because the
generation, playback-control, and lifecycle suites all drive the same worker
and would otherwise each grow their own subtly different device.
"""

import sqlite3
import threading
import time

import numpy as np
from conftest import ENTRY, make_entry

from readily_engine.catalog import Tunables
from readily_engine.generation import GeneratedAudio
from readily_engine.narration.worker import (
    GENERATION_THREAD,
    GenerationWorker,
    NarrationRequest,
    VoiceModel,
)
from readily_engine.storage.storage import NarrationStorage

KOKORO = Tunables.model_validate(ENTRY["tunables"])


class RecordingSynthesizer:
    def __init__(self, hold: threading.Event | None = None) -> None:
        self.hold = hold
        self.thread_ids: list[int] = []
        self.inputs: list[str] = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def generate(self, record):
        text = record.text
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.thread_ids.append(threading.get_ident())
            self.inputs.append(text)
            if self.hold is not None:
                self.hold.wait(timeout=2)
            return GeneratedAudio(np.ones(240, dtype=np.float32), 24_000)
        finally:
            with self.lock:
                self.active -= 1


class RecordingPlayback:
    """A double that keeps the real playback's rule: audio is admitted for
    exactly the generation the last stop opened, and an older generation's
    stop is ignored. Playout is instant — `position` reports everything fed
    as already heard — so the lookahead gate never parks generic tests.

    Instant playout still stops at a pause. The real callback emits silence
    and consumes nothing while paused, so audio fed during a pause is
    queued rather than heard and the playhead does not move; a double that
    kept counting would report a playhead the device never reached.

    `drain` returns at once even while paused, though, so a Narration this
    double holds paused still finishes. Behaviour that depends on a pause
    outlasting the last fed frame — a `paused` resume — needs the real
    player with a fake stream.
    """

    late_callbacks = 0
    ring_starvations = 0
    underflows = 0

    def __init__(self) -> None:
        self.played: list[tuple[np.ndarray, int]] = []
        self.drains = 0
        self.stop_calls = 0
        self.paused = False
        self.generation = 0
        self.frozen_sec: float | None = None

    def feed(self, pcm, sample_rate: int, generation: int, *, pause_frames: int = 0):
        if generation != self.generation:
            return 0.0
        self.played.append((pcm, sample_rate))
        return len(pcm) / sample_rate

    def set_speed(self, speed: float) -> None:
        self.speed = speed

    def starving(self, generation: int) -> bool:
        return False

    def level(self, generation: int) -> float:
        return 0.0

    def drain(self, generation: int) -> float:
        self.drains += 1
        return 0.0

    def stop(self, generation: int) -> None:
        if generation < self.generation:
            return
        self.generation = generation
        self.paused = False
        self.frozen_sec = None
        self.stop_calls += 1

    def pause(self, generation: int) -> None:
        if generation == self.generation:
            self.paused = True
            self.frozen_sec = self._heard_sec()

    def unpause(self, generation: int) -> None:
        if generation == self.generation:
            self.paused = False
            self.frozen_sec = None

    def wait(self, generation: int, ready) -> bool:
        while True:
            if generation != self.generation:
                return False
            if ready():
                return True
            time.sleep(0.001)

    def _heard_sec(self) -> float:
        return sum(len(pcm) / rate for pcm, rate in self.played)

    def position(self, generation: int) -> float:
        if generation != self.generation:
            return 0.0
        if self.frozen_sec is not None:
            return self.frozen_sec
        return self._heard_sec()


class RecordingStorage:
    """Passes everything through to the real storage while remembering
    which Block each audio key belongs to, so a test can target the raw
    read of "the second Block" although reads are addressed by key."""

    def __init__(self, wrapped: NarrationStorage) -> None:
        self.wrapped = wrapped
        self.checkpoints = []
        self.checkpoint_thread_ids = []
        self.ordinals: dict[str, int] = {}

    def __getattr__(self, name):
        return getattr(self.wrapped, name)

    def _remember(self, plan):
        self.ordinals.update({part.key: part.ordinal for part in plan.segments})
        return plan

    def create(self, source, settings, entry, *, seed=None):
        return self._remember(self.wrapped.create(source, settings, entry, seed=seed))

    def plan(self, narration_id):
        return self._remember(self.wrapped.plan(narration_id))

    def resume(self, narration_id):
        return self._remember(self.wrapped.resume(narration_id))

    def rekey_segment(self, narration_id, segment, settings):
        rekeyed = self.wrapped.rekey_segment(narration_id, segment, settings)
        self.ordinals[rekeyed.key] = rekeyed.ordinal
        return rekeyed

    def checkpoint(self, narration_id: str, position_sec: float) -> None:
        self.checkpoints.append((narration_id, position_sec))
        self.checkpoint_thread_ids.append(threading.get_ident())
        self.wrapped.checkpoint(narration_id, position_sec)


class FixedSynthesizer(RecordingSynthesizer):
    """Returns one Block of `seconds` of audible PCM.

    `hold` parks synthesis until it is set — on every Block, or on only the
    one whose text is `hold_text` — which is how a test opens the window
    where a Narration is playing with work still in flight.
    """

    def __init__(
        self,
        seconds: float,
        *,
        value: float = 1.0,
        sample_rate: int = 24_000,
        hold: threading.Event | None = None,
        hold_text: str | None = None,
    ) -> None:
        super().__init__(hold)
        self.seconds = seconds
        self.value = value
        self.sample_rate = sample_rate
        self.hold_text = hold_text

    def generate(self, record):
        text = record.text
        self.inputs.append(text)
        if self.hold is not None and self.hold_text in (None, text):
            self.hold.wait(timeout=2)
        frames = round(self.seconds * self.sample_rate)
        return GeneratedAudio(
            np.full(frames, self.value, dtype=np.float32), self.sample_rate
        )


class PositionedPlayback(RecordingPlayback):
    """Reports one fixed playhead, and counts who asked for it.

    The generation thread asks only while parked at the lookahead gate, so
    a rising `lookahead_polls` is a test's positive signal that preparation
    has stopped rather than merely gone quiet for a moment.
    """

    def __init__(self, position_sec: float) -> None:
        super().__init__()
        self.position_sec = position_sec
        self.position_thread_ids = []
        self.lookahead_polls = 0

    def position(self, generation: int) -> float:
        self.position_thread_ids.append(threading.get_ident())
        if threading.current_thread().name == GENERATION_THREAD:
            self.lookahead_polls += 1
        return self.position_sec if generation == self.generation else 0.0


class HeldPlayback(PositionedPlayback):
    """Parks every `feed` until `release`, holding the playhead still — the
    window where audio has reached the device and gone no further."""

    def __init__(self, release: threading.Event, *, position_sec: float = 0.0) -> None:
        super().__init__(position_sec)
        self.release = release
        self.entered = threading.Event()

    def feed(self, pcm, sample_rate: int, generation: int, *, pause_frames: int = 0):
        self.entered.set()
        self.wait(generation, self.release.is_set)
        return super().feed(pcm, sample_rate, generation)


class HoldSecondBlockSynthesizer:
    """Yields the first Block instantly, then parks synthesis on `release` —
    the window where a Narration is audibly playing with work in flight."""

    def __init__(self, release: threading.Event) -> None:
        self.release = release
        self.inputs: list[str] = []

    def generate(self, record):
        text = record.text
        self.inputs.append(text)
        if len(self.inputs) > 1:
            self.release.wait(timeout=2)
        return GeneratedAudio(np.ones(240, dtype=np.float32), 24_000)


class FailingStorage(RecordingStorage):
    """Storage that fails whichever writes the test names, the way a full
    disk or a locked database fails: after the Narration is already under
    way, and on the very call the worker makes to record the trouble."""

    def __init__(self, wrapped: NarrationStorage, *, failing: set[str]) -> None:
        super().__init__(wrapped)
        self.failing = failing
        self.attempts: list[str] = []

    def _guard(self, name: str) -> None:
        self.attempts.append(name)
        if name in self.failing:
            raise sqlite3.OperationalError("database is locked")

    def raw_audio(self, segment):
        self._guard("raw_audio")
        return self.wrapped.raw_audio(segment)

    def checkpoint(self, narration_id: str, position_sec: float) -> None:
        self._guard("checkpoint")
        super().checkpoint(narration_id, position_sec)

    def set_status(self, narration_id, status, **changes) -> None:
        self._guard("set_status")
        self.wrapped.set_status(narration_id, status, **changes)


def voice_model(synth, entry, prewarm=None, unload=None) -> VoiceModel:
    return VoiceModel(
        synth, entry, prewarm or (lambda: False), unload or (lambda: None)
    )


def worker_for(
    synth,
    playback,
    storage,
    checkpoint_seconds=1.0,
    prewarm=None,
    clock=time.monotonic,
    streaming=True,
):
    """A worker over `storage` for the Kokoro fake. Worker tests are about
    streaming — Blocks reaching the device while later ones synthesize — so
    the Voice's prepare-first Control is switched off here. That Control
    defaults to on; a test of prepare-first itself passes `streaming=False`
    and keeps whatever overrides it set. The write replaces af_heart's whole
    record, so set any other override for that Voice after this returns."""
    if streaming:
        storage.set_control_overrides(
            make_entry(), "narrator", {"prepare_first": False}
        )
    return GenerationWorker(
        {"acme:1m": voice_model(synth, make_entry(), prewarm)},
        playback,
        storage,
        default_model="acme:1m",
        default_voice="narrator",
        checkpoint_seconds=checkpoint_seconds,
        clock=clock,
    )


def request(text: str) -> NarrationRequest:
    return NarrationRequest(model="acme:1m", input=text, voice="narrator")
