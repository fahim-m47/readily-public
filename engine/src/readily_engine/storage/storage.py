"""One storage boundary: durable History plus expendable Segment audio.

Callers describe Narrations and Blocks. This module hashes, joins, and
checks presence; it never puts a content hash or filesystem path on a
History view (threat model B3).
"""

import logging
import threading
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from readily_engine.audio import FloatPcm
from readily_engine.catalog import CatalogEntry
from readily_engine.catalog.controls import Overrides
from readily_engine.catalog.manifest import Effective
from readily_engine.chunking import ChunkedSource
from readily_engine.generation import GenerationRecord
from readily_engine.storage.history import (
    MAX_KEEP_AUDIO_DAYS,
    GapRecord,
    HistoryStore,
    NarrationListing,
    NarrationStatus,
    NewStoredSegment,
    RetentionSettings,
    SegmentRange,
    StoredNarration,
    StoredSegment,
    SynthesisSettings,
)
from readily_engine.storage.segments import (
    SegmentStore,
    StoredAudio,
)
from readily_engine.timings import SourceTiming, Timing

logger = logging.getLogger(__name__)

_REPLAYABLE = frozenset(
    {
        NarrationStatus.INTERRUPTED,
        NarrationStatus.STOPPED,
        NarrationStatus.FINISHED,
    }
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _reproducible(generation: GenerationRecord | None) -> bool:
    """Audio stored before Generation Records existed cannot be reproduced,
    so storage reports it absent and the worker rekeys the Segment (ADR 0004).
    """
    return generation is not None


class NarrationNotResumable(RuntimeError):
    """The Narration exists but is active or failed, so it cannot replay."""

    def __init__(self, narration_id: str) -> None:
        super().__init__(narration_id)
        self.narration_id = narration_id


@dataclass(frozen=True)
class PlannedSegment:
    """One Block in a Narration plan, with the store key the worker uses."""

    ordinal: int
    key: str
    text: str
    source_start: int
    source_end: int
    boundary: str
    sample_rate: int | None
    frame_count: int | None
    trim_start: int | None
    generation: GenerationRecord | None

    @property
    def measured(self) -> bool:
        """Whether this Block has a reproducible, trimmed range on record."""
        return self.generation is not None and self.trim_start is not None


@dataclass(frozen=True)
class NarrationPlan:
    """The worker's view of a Narration: settings, playhead, and Blocks."""

    id: str
    source: str
    settings: SynthesisSettings
    status: NarrationStatus
    playhead_sec: float
    total_duration_sec: float | None
    segments: tuple[PlannedSegment, ...]
    gap_ordinals: frozenset[int] = frozenset()

    def is_gap(self, ordinal: int) -> bool:
        return ordinal in self.gap_ordinals


@dataclass(frozen=True)
class HistorySegment:
    ordinal: int
    source_start: int
    source_end: int
    boundary: str
    duration_sec: float | None
    audio_present: bool
    # Derived from trimmed lengths and the Narration's pause policy, by the
    # Narrator; None while an earlier Block's length is unknown.
    timeline_start_sec: float | None = None
    timings: tuple[SourceTiming, ...] = ()
    # The Block's frozen timing choice; None for audio stored before
    # Generation Records existed, which cannot be regenerated at all.
    word_timing: str | None = None


@dataclass(frozen=True)
class HistoryGap:
    ordinal: int
    source_start: int
    source_end: int
    error_code: str
    created_at: datetime


@dataclass(frozen=True)
class HistorySummary:
    id: str
    source_preview: str
    model_id: str
    voice_id: str
    speed: float
    status: NarrationStatus
    created_at: datetime
    updated_at: datetime
    last_played_at: datetime
    playhead_sec: float
    total_duration_sec: float | None
    audio_present: bool
    has_gaps: bool


@dataclass(frozen=True)
class HistoryDetail(HistorySummary):
    source: str
    segments: tuple[HistorySegment, ...]
    gaps: tuple[HistoryGap, ...]


@dataclass(frozen=True)
class RetentionState:
    segment_budget_bytes: int
    keep_audio_days: int | None
    audio_bytes: int


@dataclass(frozen=True)
class RetentionApplied:
    """A policy change and what applying it actually removed.

    `evicted_bytes` is counted by the sweep as it deletes, so it is what
    *this* application of the policy freed. A client cannot work that out by
    subtracting two disk readings: the Janitor sweeps on its own schedule
    between any two of them, and a Narration being synthesized is adding
    audio the whole time.
    """

    state: RetentionState
    evicted_bytes: int


@dataclass(frozen=True)
class Sweep:
    """What one pass of retention left behind and what it took."""

    kept_bytes: int
    evicted_bytes: int


@dataclass(frozen=True)
class VoiceSelection:
    """The Voice a user last chose, as stored — `None` before they have
    chosen one. Resolving it against the Catalog (an entry can leave the
    Catalog between releases) belongs to the route that answers with it,
    not to storage, which has never heard of the Manifest."""

    model_id: str | None
    voice_id: str | None


@dataclass(frozen=True)
class DeletionResult:
    narration_id: str
    audio_bytes_freed: int


class NarrationStorage:
    """Join History records to Segment files without leaking store details."""

    def __init__(
        self,
        history: HistoryStore,
        segments: SegmentStore,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._history = history
        self._segments = segments
        self._clock = clock
        self._lock = threading.Lock()
        self._audio_holds: Counter[str] = Counter()

    @classmethod
    def open(
        cls,
        database: Path,
        segments: Path,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> "NarrationStorage":
        history = HistoryStore.open(database, clock=clock)
        try:
            return cls(history, SegmentStore(segments), clock=clock)
        except Exception:
            history.close()
            raise

    def close(self) -> None:
        self._history.close()

    def retention(self) -> RetentionState:
        """Report the policy and what Segment audio currently costs.

        The disk walk stays outside the lock: it is a measurement of a tree
        that publishing and eviction are already moving underneath it, and
        holding the lock for it would make rendering Settings block
        synthesis.
        """
        with self._lock:
            settings = self._history.settings()
        return RetentionState(
            segment_budget_bytes=settings.segment_budget_bytes,
            keep_audio_days=settings.keep_audio_days,
            audio_bytes=self._segments.disk_usage(),
        )

    def update_retention(
        self,
        *,
        segment_budget_bytes: int,
        keep_audio_days: int | None,
    ) -> RetentionApplied:
        """Replace the policy and apply it before answering."""
        with self._lock:
            self._history.update_retention(
                segment_budget_bytes=segment_budget_bytes,
                keep_audio_days=keep_audio_days,
            )
            settings = self._history.settings()
            sweep = self._sweep_locked(settings)
        return RetentionApplied(
            state=RetentionState(
                segment_budget_bytes=settings.segment_budget_bytes,
                keep_audio_days=settings.keep_audio_days,
                audio_bytes=sweep.kept_bytes,
            ),
            evicted_bytes=sweep.evicted_bytes,
        )

    def playback_speed(self) -> float:
        with self._lock:
            return self._history.settings().speed

    def effective_controls(self, entry: CatalogEntry, voice_id: str) -> Effective:
        """Compose the Voice's valid saved Overrides over the current Catalog entry."""
        return entry.compose(self.control_overrides(entry, voice_id))

    def control_overrides(self, entry: CatalogEntry, voice_id: str) -> Overrides:
        with self._lock:
            stored = self._history.control_overrides(entry.id, voice_id)
            return self._valid_control_overrides(entry, voice_id, stored)

    def _valid_control_overrides(
        self, entry: CatalogEntry, voice_id: str, stored: Overrides
    ) -> Overrides:
        """Keep saved Overrides that compose against the current Catalog entry.

        Saved overrides outlive the Manifest that accepted them, so a control
        the entry no longer declares — or a value its range no longer
        admits — is dropped rather than allowed to refuse the Narration.
        """
        schema = entry.control_schema
        kept: Overrides = {}
        for name, value in stored.items():
            try:
                schema[name].validate_value(value)
            except (KeyError, ValueError):
                logger.warning(
                    "Dropping stored control %s=%r: %s no longer accepts it for %s",
                    name,
                    value,
                    entry.id,
                    voice_id,
                )
                continue
            kept[name] = value
        try:
            entry.compose(kept)
        except ValueError:
            logger.warning(
                "Dropping every stored control for %s and %s: they no longer "
                "compose against this entry",
                entry.id,
                voice_id,
            )
            return {}
        return kept

    def set_control_overrides(
        self, entry: CatalogEntry, voice_id: str, overrides: Overrides
    ) -> Effective:
        with self._lock:
            controls = entry.compose(overrides)
            self._history.set_control_overrides(entry.id, voice_id, overrides)
        return controls

    def set_playback_speed(self, speed: float) -> None:
        with self._lock:
            self._history.set_playback_speed(speed)

    def voice_selection(self) -> VoiceSelection:
        """What the settings singleton holds as the chosen Voice."""
        with self._lock:
            settings = self._history.settings()
        return VoiceSelection(
            model_id=settings.selected_model_id,
            voice_id=settings.selected_voice_id,
        )

    def select_voice(self, *, model_id: str, voice_id: str) -> VoiceSelection:
        """Store a Voice choice made without narrating, and hand back what
        is now stored."""
        with self._lock:
            self._history.select_voice(model_id=model_id, voice_id=voice_id)
        return VoiceSelection(model_id=model_id, voice_id=voice_id)

    @contextmanager
    def hold_audio(self, narration_id: str) -> Iterator[None]:
        """Keep a Narration's prepared cache available until its feeder exits."""
        with self._lock:
            self._audio_holds[narration_id] += 1
        try:
            yield
        finally:
            with self._lock:
                self._audio_holds[narration_id] -= 1
                if not self._audio_holds[narration_id]:
                    del self._audio_holds[narration_id]

    def run_retention(self) -> None:
        """Apply the stored policy once. The Janitor's unit of work."""
        with self._lock:
            self._sweep_locked(self._history.settings())

    def _sweep_locked(self, settings: RetentionSettings) -> Sweep:
        """Evict orphaned, then aged-out, then over-budget Segment audio,
        and report both what survived and what was taken.

        One pass per rule over one snapshot, each rule handing the next the
        keys it kept — so no rule re-deletes what an earlier one removed,
        and both totals are measured from the same walk rather than
        re-derived by a second one.
        """
        recency = self._history.segment_recency()
        referenced = {item.segment_hash for item in recency}
        stored = self._segments.stored_keys()
        evicted_bytes = 0
        for key in stored - referenced:
            evicted_bytes += self._segments.delete(key)

        live = [item for item in recency if item.segment_hash in stored]
        held = {
            segment.segment_hash
            for narration_id in self._audio_holds
            if (narration := self._history.get_narration(narration_id)) is not None
            for segment in narration.segments
        }

        held.update(
            record.key
            for narration_id in self._audio_holds
            if (narration := self._history.get_narration(narration_id)) is not None
            for segment in narration.segments
            for record in self._history.block_takes(
                narration_id, segment.ordinal
            ).values()
        )

        if settings.keep_audio_days is not None:
            days = max(0, min(settings.keep_audio_days, MAX_KEEP_AUDIO_DAYS))
            cutoff = self._clock() - timedelta(days=days)
            expired = [
                item
                for item in live
                if item.last_played_at <= cutoff and item.segment_hash not in held
            ]
            for item in expired:
                evicted_bytes += self._segments.delete(item.segment_hash)
            live = [
                item
                for item in live
                if item.last_played_at > cutoff or item.segment_hash in held
            ]

        audio_bytes = sum(
            self._segments.disk_usage_for(item.segment_hash) for item in live
        )
        for item in live:
            if audio_bytes <= settings.segment_budget_bytes:
                break
            if item.segment_hash in held:
                continue
            removed = self._segments.delete(item.segment_hash)
            audio_bytes -= removed
            evicted_bytes += removed
        return Sweep(kept_bytes=audio_bytes, evicted_bytes=evicted_bytes)

    def delete(self, narration_id: str) -> DeletionResult | None:
        """Delete one Narration and collect only newly unreferenced audio."""
        with self._lock:
            candidates = self._history.delete_narration(narration_id)
            if candidates is None:
                return None
            referenced = {item.segment_hash for item in self._history.segment_recency()}
            freed = sum(
                self._segments.delete(key)
                for key in candidates
                if key not in referenced
            )
            return DeletionResult(narration_id, freed)

    def create(
        self,
        chunked: ChunkedSource,
        settings: SynthesisSettings,
        entry: CatalogEntry,
        *,
        seed: int | None = None,
    ) -> NarrationPlan:
        narration_id = str(uuid.uuid4())
        parts = []
        for index, block in enumerate(chunked.blocks):
            record = GenerationRecord.for_entry(
                entry, settings.voice_id, block.text, seed=seed
            )
            parts.append(
                NewStoredSegment(
                    ordinal=index,
                    segment_hash=record.key,
                    generation=record,
                    source_start=block.start,
                    source_end=block.end,
                    boundary=block.boundary.value,
                )
            )
        stored = self._history.create_narration(
            narration_id=narration_id,
            source=chunked.source,
            settings=settings,
            segments=tuple(parts),
        )
        self._history.select_synthesis_settings(settings)
        return self._plan(stored)

    def resume(self, narration_id: str) -> NarrationPlan:
        narration = self._history.get_narration(narration_id)
        if narration is None:
            raise KeyError(narration_id)
        if narration.status not in _REPLAYABLE:
            raise NarrationNotResumable(narration_id)
        plan = self._plan(narration)
        if narration.status is NarrationStatus.FINISHED:
            return replace(plan, playhead_sec=0.0)
        return plan

    def plan(self, narration_id: str) -> NarrationPlan:
        """Read one Narration's Blocks without admitting it for playback.

        Export needs the same plan the worker gets, for a Narration in any
        status — including one still playing. `resume` cannot serve that:
        it is an admission check, and rejecting an active Narration is the
        point of it.
        """
        narration = self._history.get_narration(narration_id)
        if narration is None:
            raise KeyError(narration_id)
        return self._plan(narration)

    def raw_audio(self, segment: PlannedSegment) -> StoredAudio | None:
        """Decode reproducible raw audio without touching History."""
        if not _reproducible(segment.generation):
            return None
        return self._segments.read(segment.key)

    def has_verified_audio(self, segment: PlannedSegment) -> bool:
        """Whether the Block replays from a Segment whose sidecar vouches for
        its bytes. Digests the FLAC; the History list asks the cheaper
        `_audio_files_present` instead."""
        return _reproducible(segment.generation) and self._segments.has_verified(
            segment.key
        )

    def timings(self, segment: PlannedSegment) -> tuple[Timing, ...]:
        if segment.sample_rate is None or not self.has_verified_audio(segment):
            return ()
        return self._segments.timings(segment.key, segment.sample_rate)

    def store_audio(
        self,
        key: str,
        pcm: FloatPcm,
        sample_rate: int,
        *,
        timings: tuple[Timing, ...] = (),
    ) -> StoredAudio:
        """Keep raw synthesis under its key, or hand back what is already
        there, adopting the new words when the stored pair has none.

        Keys are shared by every Block with the same settings and text, so
        a second synthesis of an evicted Segment replaces audio that other
        Narrations measured: their recorded ranges describe the old
        artifact and are cleared before the new one lands.
        """
        with self._lock:
            existing = self._segments.read(key)
            if existing is None:
                self._history.invalidate_segment_audio(key)
            elif existing.timings or not timings:
                return existing
            return self._segments.write(key, pcm, sample_rate, timings=timings)

    def segment_range(
        self, narration_id: str, ordinal: int, key: str
    ) -> SegmentRange | None:
        """The range History holds for one Block right now.

        History is the one authority for a range: a plan is a snapshot,
        and `store_audio` can clear ranges under it whenever a shared key
        is regenerated.
        """
        with self._lock:
            return self._history.segment_range(narration_id, ordinal, key)

    def record_length(
        self,
        narration_id: str,
        ordinal: int,
        *,
        key: str,
        sample_rate: int,
        frame_count: int,
        trim_start: int,
    ) -> None:
        """Record one Block's trimmed range once the narration has measured it."""
        with self._lock:
            written = self._history.complete_segment(
                narration_id,
                ordinal,
                segment_hash=key,
                duration_sec=frame_count / sample_rate,
                sample_rate=sample_rate,
                frame_count=frame_count,
                trim_start=trim_start,
            )
        if not written:
            logger.debug(
                "dropped measurements for a Block that was rerolled: %s ordinal %d",
                narration_id,
                ordinal,
            )

    def block_takes(
        self, narration_id: str, ordinal: int
    ) -> dict[str, GenerationRecord]:
        return self._history.block_takes(narration_id, ordinal)

    def narration_takes(
        self, narration_id: str
    ) -> dict[int, dict[str, GenerationRecord]]:
        return self._history.narration_takes(narration_id)

    def select_take(
        self, narration_id: str, ordinal: int, record: GenerationRecord
    ) -> NarrationPlan:
        with self._lock:
            plan = self.plan(narration_id)
            original = plan.segments[ordinal].generation
            if original is None:
                raise ValueError("Block has no Generation Record")
            self._history.select_take(narration_id, ordinal, record, original=original)
            return self.plan(narration_id)

    def rekey_segment(
        self,
        narration_id: str,
        segment: PlannedSegment,
        generation: GenerationRecord,
    ) -> PlannedSegment:
        """Re-address one Block under new settings and hand it back.

        The new Block is the old one with a new key and no audio facts —
        which is exactly what the row now says, so reading the whole
        Narration back to find one Block would only re-derive it.
        """
        key = generation.key
        if not self._history.replace_segment_key(
            narration_id, segment.ordinal, key, generation, segment.key
        ):
            logger.debug(
                "dropped a re-address for a Block that was rerolled: %s ordinal %d",
                narration_id,
                segment.ordinal,
            )
        return replace(
            segment,
            key=key,
            generation=generation,
            sample_rate=None,
            frame_count=None,
            trim_start=None,
        )

    def set_status(
        self,
        narration_id: str,
        status: NarrationStatus,
        *,
        playhead_sec: float | None = None,
        total_duration_sec: float | None = None,
    ) -> None:
        self._history.set_status(
            narration_id,
            status,
            playhead_sec=playhead_sec,
            total_duration_sec=total_duration_sec,
        )

    def checkpoint(self, narration_id: str, position_sec: float) -> None:
        self._history.checkpoint(narration_id, position_sec)

    def record_gap(
        self, narration_id: str, segment: PlannedSegment, error_code: str
    ) -> bool:
        """Mark one Block as unspoken, keeping the source span it covered.

        The Block comes from the caller's plan rather than a re-read: the
        span is immutable once the Narration is created, and the caller is
        holding the very Block it just failed to synthesize. False when that
        Block has since been rerolled out from under the caller.
        """
        return self._history.record_gap(
            narration_id,
            ordinal=segment.ordinal,
            segment_hash=segment.key,
            source_start=segment.source_start,
            source_end=segment.source_end,
            error_code=error_code,
        )

    def history(self) -> tuple[HistorySummary, ...]:
        return tuple(self._summary(item) for item in self._history.list_narrations())

    def history_detail(self, narration_id: str) -> HistoryDetail | None:
        stored = self._history.get_narration(narration_id)
        if stored is None:
            return None
        return self._detail(stored)

    def _plan(self, stored: StoredNarration) -> NarrationPlan:
        return NarrationPlan(
            id=stored.id,
            source=stored.source,
            settings=stored.settings,
            status=stored.status,
            playhead_sec=stored.playhead_sec,
            total_duration_sec=stored.total_duration_sec,
            segments=tuple(
                PlannedSegment(
                    ordinal=part.ordinal,
                    key=part.segment_hash,
                    generation=part.generation,
                    text=stored.source[part.source_start : part.source_end].strip(),
                    source_start=part.source_start,
                    source_end=part.source_end,
                    boundary=part.boundary,
                    sample_rate=part.sample_rate,
                    frame_count=part.frame_count,
                    trim_start=part.trim_start,
                )
                for part in stored.segments
            ),
            gap_ordinals=frozenset(gap.ordinal for gap in stored.gaps),
        )

    def _summary(
        self,
        stored: NarrationListing,
        views: tuple[HistorySegment, ...] | None = None,
    ) -> HistorySummary:
        """`views` lets the detail path reuse the Segment views it has
        already built, so a detail read stats each Block once, not twice."""
        return HistorySummary(
            id=stored.id,
            source_preview=stored.source_preview,
            model_id=stored.settings.model_id,
            voice_id=stored.settings.voice_id,
            speed=stored.settings.speed,
            status=stored.status,
            created_at=stored.created_at,
            updated_at=stored.updated_at,
            last_played_at=stored.last_played_at,
            playhead_sec=stored.playhead_sec,
            total_duration_sec=stored.total_duration_sec,
            audio_present=(
                self._audio_files_present(stored.segments)
                if views is None
                else bool(views) and all(view.audio_present for view in views)
            ),
            has_gaps=bool(stored.gaps),
        )

    def _audio_files_present(self, parts: tuple[StoredSegment, ...]) -> bool:
        """Cheap file presence for the History list; detail verifies the bytes.

        A Narration with no Blocks is not: there is nothing to play, and
        reporting `True` for it would tell the History screen that an empty
        row is ready to resume.
        """
        return bool(parts) and all(
            _reproducible(part.generation) and self._segments.has(part.segment_hash)
            for part in parts
        )

    def _detail(self, stored: StoredNarration) -> HistoryDetail:
        views = tuple(self._history_segment(part) for part in stored.segments)
        summary = self._summary(stored, views)
        return HistoryDetail(
            id=summary.id,
            source_preview=summary.source_preview,
            model_id=summary.model_id,
            voice_id=summary.voice_id,
            speed=summary.speed,
            status=summary.status,
            created_at=summary.created_at,
            updated_at=summary.updated_at,
            last_played_at=summary.last_played_at,
            playhead_sec=summary.playhead_sec,
            total_duration_sec=summary.total_duration_sec,
            audio_present=summary.audio_present,
            has_gaps=summary.has_gaps,
            source=stored.source,
            segments=views,
            gaps=tuple(self._history_gap(gap) for gap in stored.gaps),
        )

    def _history_segment(self, part: StoredSegment) -> HistorySegment:
        return HistorySegment(
            ordinal=part.ordinal,
            source_start=part.source_start,
            source_end=part.source_end,
            boundary=part.boundary,
            duration_sec=part.duration_sec,
            word_timing=part.generation.word_timing if part.generation else None,
            audio_present=_reproducible(part.generation)
            and self._segments.has_verified(part.segment_hash),
        )

    @staticmethod
    def _history_gap(gap: GapRecord) -> HistoryGap:
        return HistoryGap(
            ordinal=gap.ordinal,
            source_start=gap.source_start,
            source_end=gap.source_end,
            error_code=gap.error_code,
            created_at=gap.created_at,
        )
