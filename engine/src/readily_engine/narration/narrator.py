"""Production wiring for synthesis, serial generation, and Engine playback."""

import logging
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from readily_engine.audio.encoding import ExportFormat
from readily_engine.audio.playback import SoundDevicePlayback
from readily_engine.catalog import CatalogEntry, Manifest
from readily_engine.catalog.controls import Overrides
from readily_engine.catalog.manifest import Effective
from readily_engine.catalog.recipes import Mode
from readily_engine.loading import PromotedDirs, synthesizer_for
from readily_engine.loading.alignment import ForcedAligner
from readily_engine.narration.export import Exporter, ExportSnapshot
from readily_engine.narration.timeline import timeline_starts
from readily_engine.narration.words import words_for
from readily_engine.narration.worker import (
    GenerationWorker,
    NarrationRequest,
    NarrationSnapshot,
    TakeAction,
    VoiceModel,
)
from readily_engine.storage.storage import (
    DeletionResult,
    HistoryDetail,
    HistorySummary,
    NarrationStorage,
    RetentionApplied,
    RetentionState,
    VoiceSelection,
)

logger = logging.getLogger(__name__)


def _terminate() -> None:
    # os._exit, like the supervisor watchdog's: it ends the process without
    # running interpreter finalization, which is the step that segfaults
    # while a generation thread is still inside MLX or ONNX. A segfault
    # would reach the supervisor as an Engine crash and spend restart
    # budget on a process that was already on its way out.
    os._exit(0)


class SpeechRequest(Protocol):
    model: str
    input: str
    voice: str
    mode: Mode


class EngineNarrator:
    """Own the process-long worker and the one process-long audio stream.

    Every Catalog entry gets its Architecture's lazy synthesizer, loading only
    from the store's promoted directory (ADR 0003 §3) — lazily, because a
    directory may not exist until the user downloads that model.

    `default` is what this machine narrates with before anyone chooses, and
    the one model prewarmed at boot: the caller picks it for the Backends it
    has, so an Intel build never tries to make an MLX model resident.
    """

    def __init__(
        self,
        catalog: Manifest,
        store: PromotedDirs,
        storage: NarrationStorage,
        audio_folder: Path,
        default: CatalogEntry,
    ) -> None:
        self._playback = SoundDevicePlayback()
        self._storage = storage
        models = {}
        for entry in catalog.models:
            synthesizer = synthesizer_for(entry, store)
            models[entry.id] = VoiceModel(
                synthesizer, entry, synthesizer.prewarm, synthesizer.unload
            )
        self._worker = GenerationWorker(
            models,
            self._playback,
            storage,
            default_model=default.id,
            default_voice=default.default_voice,
            aligners={
                support.id: ForcedAligner(support, store)
                for support in catalog.support_models
            },
        )
        self._exporter = Exporter(storage, self._worker, audio_folder)

    def prewarm(self) -> None:
        """Preload + prewarm the default model — only the default: the
        expressive Tier loads lazily on first use, keeping its ~2.7GB load
        transient off the boot path. Queued on the worker thread, so boot
        itself is never blocked on the load."""
        self._worker.prewarm()

    def start(self, request: SpeechRequest) -> str:
        return self._worker.start(
            NarrationRequest(
                model=request.model,
                input=request.input,
                voice=request.voice,
                mode=request.mode,
            )
        )

    def stop(self) -> bool:
        return self._worker.stop()

    def set_speed(self, speed: float) -> None:
        self._worker.set_speed(speed)

    def pause(self) -> bool:
        return self._worker.pause()

    def play(self) -> bool:
        return self._worker.play()

    def select_take(self, narration_id: str, ordinal: int, action: TakeAction) -> bool:
        return self._worker.select_take(narration_id, ordinal, action)

    def seek(self, source_offset: int) -> bool:
        return self._worker.seek(source_offset)

    def seek_time(self, position_sec: float) -> bool:
        return self._worker.seek_time(position_sec)

    def resume(self, narration_id: str, *, paused: bool = False) -> str:
        return self._worker.resume(narration_id, paused=paused)

    def list(self) -> tuple[HistorySummary, ...]:
        return self._storage.history()

    def detail(
        self, narration_id: str, *, after_ordinal: int | None = None
    ) -> HistoryDetail | None:
        """One Narration's record, with each Block placed on its timeline.

        With `after_ordinal`, only the Blocks past it are returned, on the
        client's promise that it holds every Block up to it measured. An
        evicted artifact's re-synthesis can unmeasure one of those under
        the client (`Storage.store_audio`), so the promise is checked and
        the whole list returned when it no longer holds. The timeline is
        still walked from the first Block, because a start is a running sum
        of every earlier length; the stored word timings, which are a read
        per Block, are only fetched for the Blocks returned.
        """
        detail = self._storage.history_detail(narration_id)
        if detail is None:
            return None
        plan = self._storage.plan(narration_id)
        starts = timeline_starts(plan)
        prefix_measured = after_ordinal is not None and all(
            part.duration_sec is not None
            for part in detail.segments[: after_ordinal + 1]
        )
        return replace(
            detail,
            segments=tuple(
                replace(
                    part,
                    timeline_start_sec=start,
                    timings=words_for(self._storage, plan, segment, start),
                )
                for part, segment, start in zip(
                    detail.segments, plan.segments, starts, strict=True
                )
                if not prefix_measured or part.ordinal > after_ordinal
            ),
        )

    def delete(self, narration_id: str) -> DeletionResult | None:
        return self._worker.delete(narration_id)

    def retention(self) -> RetentionState:
        return self._storage.retention()

    def update_retention(
        self,
        *,
        segment_budget_bytes: int,
        keep_audio_days: int | None,
    ) -> RetentionApplied:
        return self._storage.update_retention(
            segment_budget_bytes=segment_budget_bytes,
            keep_audio_days=keep_audio_days,
        )

    def voice_selection(self) -> VoiceSelection:
        return self._storage.voice_selection()

    def effective_controls(self, entry: CatalogEntry, voice_id: str) -> Effective:
        return self._storage.effective_controls(entry, voice_id)

    def control_overrides(self, entry: CatalogEntry, voice_id: str) -> Overrides:
        return self._storage.control_overrides(entry, voice_id)

    def set_control_overrides(
        self, entry: CatalogEntry, voice_id: str, overrides: Overrides
    ) -> Effective:
        return self._storage.set_control_overrides(entry, voice_id, overrides)

    def select_voice(self, *, model_id: str, voice_id: str) -> VoiceSelection:
        return self._storage.select_voice(model_id=model_id, voice_id=voice_id)

    def export(self, narration_id: str, export_format: ExportFormat) -> None:
        return self._exporter.start(narration_id, export_format)

    def export_events(self) -> AsyncIterator[ExportSnapshot]:
        return self._exporter.events()

    def events(self) -> AsyncIterator[NarrationSnapshot]:
        return self._worker.events()

    def close(self, on_stuck: Callable[[], None] = _terminate) -> None:
        """Shut down generation, then the audio device — in that order, and
        only if generation actually stopped. A worker still inside a native
        synthesis call cannot be waited out any longer than
        `SHUTDOWN_GRACE_SECONDS`, and closing PortAudio or returning into
        interpreter shutdown around it is the crash; ending the process
        outright is not."""
        if self._worker.close():
            self._exporter.close()
            self._playback.close()
            return
        logger.warning("Generation did not stop in time; ending the Engine process")
        on_stuck()
