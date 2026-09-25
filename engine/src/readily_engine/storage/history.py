"""Migrated SQLite History: permanent Narrations over expendable audio."""

import json
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple

from pydantic import TypeAdapter

from readily_engine.catalog import PausePolicy
from readily_engine.catalog.controls import Overrides
from readily_engine.generation import GenerationRecord
from readily_engine.storage.takes import MIGRATION_8, BlockTakes

DEFAULT_SEGMENT_BUDGET_BYTES = 5 * 1024**3

# The retention knobs are user-facing numbers that end up in SQLite and in
# `datetime` arithmetic, so both need a ceiling the wire can reject before
# the value is committed. A budget above SQLite's signed 64-bit INTEGER
# cannot be stored at all, and an age past a century is `null` (Never) with
# extra steps — while a large enough one drives the eviction cutoff below
# `datetime.min` and raises.
MAX_SEGMENT_BUDGET_BYTES = 2**63 - 1
MAX_KEEP_AUDIO_DAYS = 36_500

LATEST_SCHEMA_VERSION = 8


class HistorySchemaError(RuntimeError):
    """The database schema is not one this Engine can safely open."""


class NarrationStatus(StrEnum):
    PREPARING = "preparing"
    PLAYING = "playing"
    INTERRUPTED = "interrupted"
    STOPPED = "stopped"
    FINISHED = "finished"
    FAILED = "failed"


@dataclass(frozen=True)
class SynthesisSettings:
    model_id: str
    catalog_version: int
    voice_id: str
    speed: float
    pause_policy: PausePolicy
    # The Control defaults to on; this default is the column's, for rows
    # written before it existed, all of which streamed.
    prepare_first: bool = field(default=False, kw_only=True)


@dataclass(frozen=True)
class RetentionSettings:
    selected_model_id: str | None
    selected_voice_id: str | None
    speed: float
    segment_budget_bytes: int
    keep_audio_days: int | None


@dataclass(frozen=True)
class _SegmentPlacement:
    """Where a Segment sits in its Narration and in the Source."""

    ordinal: int
    segment_hash: str
    source_start: int
    source_end: int
    boundary: str


@dataclass(frozen=True)
class NewStoredSegment(_SegmentPlacement):
    """A Segment on its way into History, which no path writes without a
    Generation Record: a row that cannot say how its audio was made can
    never be reproduced."""

    generation: GenerationRecord = field(kw_only=True)


@dataclass(frozen=True)
class StoredSegment(_SegmentPlacement):
    """A Segment read back. Rows written before Generation Records existed
    have none, and their audio is reported absent."""

    duration_sec: float | None
    sample_rate: int | None
    frame_count: int | None
    trim_start: int | None
    generation: GenerationRecord | None = field(default=None, kw_only=True)


class SegmentRange(NamedTuple):
    """The trimmed range one Block plays from its raw Segment."""

    sample_rate: int
    frame_count: int
    trim_start: int


@dataclass(frozen=True)
class GapRecord:
    ordinal: int
    source_start: int
    source_end: int
    error_code: str
    created_at: datetime


# How much of a Source the History list shows per Narration. The list
# reads only this much out of SQLite; a Source can be a million characters.
SOURCE_PREVIEW_CHARS = 160


@dataclass(frozen=True)
class NarrationListing:
    """What the History list reads of a Narration: everything but its Source,
    of which only the preview travels."""

    id: str
    source_preview: str
    settings: SynthesisSettings
    status: NarrationStatus
    playhead_sec: float
    total_duration_sec: float | None
    created_at: datetime
    updated_at: datetime
    last_played_at: datetime
    segments: tuple[StoredSegment, ...]
    gaps: tuple[GapRecord, ...]


@dataclass(frozen=True)
class StoredNarration(NarrationListing):
    source: str


@dataclass(frozen=True)
class SegmentRecency:
    """One stored Segment ordered by its newest referencing Narration."""

    segment_hash: str
    last_played_at: datetime


_MIGRATION_1 = (
    """
    CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE narrations (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        model_id TEXT NOT NULL,
        catalog_version INTEGER NOT NULL CHECK (catalog_version >= 1),
        voice_id TEXT NOT NULL,
        speed REAL NOT NULL CHECK (speed BETWEEN 0.5 AND 2.0),
        status TEXT NOT NULL CHECK (
            status IN (
                'preparing', 'playing', 'interrupted',
                'stopped', 'finished', 'failed'
            )
        ),
        playhead_sec REAL NOT NULL DEFAULT 0 CHECK (playhead_sec >= 0),
        total_duration_sec REAL CHECK (total_duration_sec >= 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_played_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE narration_segments (
        narration_id TEXT NOT NULL
            REFERENCES narrations(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        segment_hash TEXT NOT NULL CHECK (
            length(segment_hash) = 64
            AND segment_hash NOT GLOB '*[^0-9a-f]*'
        ),
        source_start INTEGER NOT NULL CHECK (source_start >= 0),
        source_end INTEGER NOT NULL CHECK (source_end >= source_start),
        boundary TEXT NOT NULL CHECK (
            boundary IN ('sentence', 'paragraph', 'mid-sentence')
        ),
        duration_sec REAL CHECK (duration_sec >= 0),
        sample_rate INTEGER CHECK (sample_rate > 0),
        frame_count INTEGER CHECK (frame_count >= 0),
        CHECK (
            (duration_sec IS NULL AND sample_rate IS NULL AND frame_count IS NULL)
            OR
            (duration_sec IS NOT NULL AND sample_rate IS NOT NULL
                AND frame_count IS NOT NULL)
        ),
        PRIMARY KEY (narration_id, ordinal)
    )
    """,
    """
    CREATE TABLE gaps (
        narration_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        source_start INTEGER NOT NULL CHECK (source_start >= 0),
        source_end INTEGER NOT NULL CHECK (source_end >= source_start),
        error_code TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (narration_id, ordinal),
        FOREIGN KEY (narration_id, ordinal)
            REFERENCES narration_segments(narration_id, ordinal)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE settings (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        selected_model_id TEXT,
        selected_voice_id TEXT,
        speed REAL NOT NULL CHECK (speed BETWEEN 0.5 AND 2.0),
        segment_budget_bytes INTEGER NOT NULL CHECK (segment_budget_bytes > 0),
        keep_audio_days INTEGER CHECK (keep_audio_days >= 0)
    )
    """,
    """
    INSERT INTO settings(
        singleton,
        selected_model_id,
        selected_voice_id,
        speed,
        segment_budget_bytes,
        keep_audio_days
    ) VALUES (1, NULL, NULL, 1.0, 5368709120, NULL)
    """,
    """
    CREATE INDEX narration_recency
    ON narrations(created_at DESC, id DESC)
    """,
)

_MIGRATION_2 = (
    """
    ALTER TABLE narration_segments
    ADD COLUMN timeline_start_sec REAL CHECK (timeline_start_sec >= 0)
    """,
)

_MIGRATION_3 = (
    """
    ALTER TABLE narrations ADD COLUMN pause_policy_version INTEGER NOT NULL
        DEFAULT 1 CHECK (pause_policy_version IN (1, 2))
    """,
    """
    ALTER TABLE narrations ADD COLUMN pause_sentence_ms INTEGER NOT NULL
        DEFAULT 80 CHECK (pause_sentence_ms >= 0)
    """,
    """
    ALTER TABLE narrations ADD COLUMN pause_paragraph_break_ms INTEGER NOT NULL
        DEFAULT 400 CHECK (pause_paragraph_break_ms >= 0)
    """,
)

_MIGRATION_4 = (
    "ALTER TABLE narration_segments ADD COLUMN generation_json TEXT",
    "ALTER TABLE narrations RENAME COLUMN speed TO legacy_synthesis_speed",
    "ALTER TABLE narrations ADD COLUMN speed REAL NOT NULL DEFAULT 1.0",
    "UPDATE narrations SET speed = legacy_synthesis_speed",
    "ALTER TABLE narrations DROP COLUMN legacy_synthesis_speed",
    "ALTER TABLE settings RENAME COLUMN speed TO legacy_synthesis_speed",
    "ALTER TABLE settings ADD COLUMN speed REAL NOT NULL DEFAULT 1.0",
    "UPDATE settings SET speed = legacy_synthesis_speed",
    "ALTER TABLE settings DROP COLUMN legacy_synthesis_speed",
)

_MIGRATION_5 = (
    "ALTER TABLE narration_segments DROP COLUMN timeline_start_sec",
    "ALTER TABLE narration_segments ADD COLUMN trim_start INTEGER "
    "CHECK (trim_start >= 0)",
    # Raw lengths must be measured with the saved Pause Policy before seeking.
    "UPDATE narration_segments SET duration_sec = NULL, "
    "sample_rate = NULL, frame_count = NULL",
)

_MIGRATION_6 = (
    # Schema 4 deliberately removed the synthesis-era ceiling. Clamp values
    # accepted by that release to the range qualified by live time stretching.
    "UPDATE settings SET speed = MIN(MAX(speed, 0.5), 3.0)",
)

_MIGRATIONS = {
    1: _MIGRATION_1,
    2: _MIGRATION_2,
    3: _MIGRATION_3,
    4: _MIGRATION_4,
    5: _MIGRATION_5,
    6: _MIGRATION_6,
    7: (
        "ALTER TABLE settings ADD COLUMN control_overrides TEXT NOT NULL DEFAULT '{}'",
        "ALTER TABLE narrations ADD COLUMN prepare_first INTEGER NOT NULL DEFAULT 0 "
        "CHECK (prepare_first IN (0, 1))",
    ),
    8: MIGRATION_8,
}

_OVERRIDES = TypeAdapter(dict[str, dict[str, Overrides]])


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _by_narration(rows: Sequence[sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
    """Bucket child rows by the Narration they belong to, order preserved."""
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["narration_id"]), []).append(row)
    return grouped


class HistoryStore(BlockTakes):
    """The Engine's single process-owned SQLite connection."""

    def __init__(
        self,
        database: Path,
        connection: sqlite3.Connection,
        clock: Callable[[], datetime],
    ) -> None:
        self.database = database
        self._connection = connection
        self._clock = clock
        self._lock = threading.RLock()

    @classmethod
    def open(
        cls,
        database: Path,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> "HistoryStore":
        database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            database,
            check_same_thread=False,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        store = cls(database, connection, clock)
        try:
            store._configure()
            store._migrate()
            store._interrupt_abandoned_narrations()
        except Exception:
            connection.close()
            raise
        return store

    def _configure(self) -> None:
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=5000")

    def _migrate(self) -> None:
        exists = self._connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'schema_migrations'
            """
        ).fetchone()
        versions = self.migration_versions() if exists else ()
        if versions and versions[-1] > LATEST_SCHEMA_VERSION:
            raise HistorySchemaError(
                "The database uses a newer schema than this Engine supports"
            )
        current = versions[-1] if versions else 0
        for version in range(current + 1, LATEST_SCHEMA_VERSION + 1):
            with self._transaction():
                for statement in _MIGRATIONS[version]:
                    self._connection.execute(statement)
                self._connection.execute(
                    """
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (?, ?)
                    """,
                    (version, self._timestamp()),
                )

    def _interrupt_abandoned_narrations(self) -> None:
        now = self._timestamp()
        with self._transaction():
            self._connection.execute(
                """
                UPDATE narrations
                SET status = ?, updated_at = ?
                WHERE status IN (?, ?)
                """,
                (
                    NarrationStatus.INTERRUPTED.value,
                    now,
                    NarrationStatus.PREPARING.value,
                    NarrationStatus.PLAYING.value,
                ),
            )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    def migration_versions(self) -> tuple[int, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        return tuple(int(row[0]) for row in rows)

    def settings(self) -> RetentionSettings:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT selected_model_id, selected_voice_id, speed,
                       segment_budget_bytes, keep_audio_days
                FROM settings WHERE singleton = 1
                """
            ).fetchone()
        if row is None:
            raise HistorySchemaError("The settings singleton is missing")
        return RetentionSettings(
            selected_model_id=row["selected_model_id"],
            selected_voice_id=row["selected_voice_id"],
            speed=float(row["speed"]),
            segment_budget_bytes=int(row["segment_budget_bytes"]),
            keep_audio_days=(
                None if row["keep_audio_days"] is None else int(row["keep_audio_days"])
            ),
        )

    def select_voice(self, *, model_id: str, voice_id: str) -> None:
        """Record the Voice a user chose, without touching the speed they
        chose separately. Narrating writes the same two columns through
        `select_synthesis_settings`; this is the same choice made without
        narrating, which is the only way it survives a quit."""
        with self._transaction():
            self._connection.execute(
                """
                UPDATE settings
                SET selected_model_id = ?, selected_voice_id = ?
                WHERE singleton = 1
                """,
                (model_id, voice_id),
            )

    def control_overrides(self, model_id: str, voice_id: str) -> Overrides:
        with self._lock:
            row = self._connection.execute(
                "SELECT control_overrides FROM settings WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise HistorySchemaError("The settings singleton is missing")
        return _OVERRIDES.validate_json(row[0]).get(model_id, {}).get(voice_id, {})

    def set_control_overrides(
        self, model_id: str, voice_id: str, overrides: Overrides
    ) -> None:
        """Replace one Voice's overrides atomically, preserving every other Voice."""
        with self._transaction():
            row = self._connection.execute(
                "SELECT control_overrides FROM settings WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise HistorySchemaError("The settings singleton is missing")
            stored = _OVERRIDES.validate_json(row[0])
            voices = stored.setdefault(model_id, {})
            if overrides:
                voices[voice_id] = overrides
            else:
                voices.pop(voice_id, None)
                if not voices:
                    stored.pop(model_id, None)
            self._connection.execute(
                "UPDATE settings SET control_overrides = ? WHERE singleton = 1",
                (json.dumps(stored, allow_nan=False),),
            )

    def set_playback_speed(self, speed: float) -> None:
        """Persist playback speed independently of Voice selection and audio keys."""
        with self._transaction():
            self._connection.execute(
                "UPDATE settings SET speed = ? WHERE singleton = 1", (speed,)
            )

    def select_synthesis_settings(self, settings: SynthesisSettings) -> None:
        with self._transaction():
            self._connection.execute(
                """
                UPDATE settings
                SET selected_model_id = ?, selected_voice_id = ?
                WHERE singleton = 1
                """,
                (settings.model_id, settings.voice_id),
            )

    def update_retention(
        self,
        *,
        segment_budget_bytes: int,
        keep_audio_days: int | None,
    ) -> None:
        with self._transaction():
            self._connection.execute(
                """
                UPDATE settings
                SET segment_budget_bytes = ?, keep_audio_days = ?
                WHERE singleton = 1
                """,
                (segment_budget_bytes, keep_audio_days),
            )

    def create_narration(
        self,
        *,
        narration_id: str,
        source: str,
        settings: SynthesisSettings,
        segments: Sequence[NewStoredSegment],
    ) -> StoredNarration:
        now = self._timestamp()
        with self._transaction():
            self._connection.execute(
                """
                INSERT INTO narrations(
                    id, source, model_id, catalog_version, voice_id, speed,
                    status, playhead_sec, total_duration_sec,
                    created_at, updated_at, last_played_at,
                    pause_policy_version, pause_sentence_ms, pause_paragraph_break_ms,
                    prepare_first
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    narration_id,
                    source,
                    settings.model_id,
                    settings.catalog_version,
                    settings.voice_id,
                    settings.speed,
                    NarrationStatus.PREPARING.value,
                    now,
                    now,
                    now,
                    settings.pause_policy.pause_policy_version,
                    settings.pause_policy.pause_sentence_ms,
                    settings.pause_policy.pause_paragraph_break_ms,
                    settings.prepare_first,
                ),
            )
            self._connection.executemany(
                """
                INSERT INTO narration_segments(
                    narration_id, ordinal, segment_hash, source_start,
                    source_end, boundary, generation_json, duration_sec,
                    sample_rate, frame_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
                """,
                (
                    (
                        narration_id,
                        part.ordinal,
                        part.segment_hash,
                        part.source_start,
                        part.source_end,
                        part.boundary,
                        part.generation.canonical_json(),
                    )
                    for part in segments
                ),
            )
        created = self.get_narration(narration_id)
        if created is None:
            raise RuntimeError("The committed Narration could not be read back")
        return created

    def get_narration(self, narration_id: str) -> StoredNarration | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT *, substr(source, 1, ?) AS source_preview"
                " FROM narrations WHERE id = ?",
                (SOURCE_PREVIEW_CHARS, narration_id),
            ).fetchone()
            if row is None:
                return None
            segments = self._connection.execute(
                """
                SELECT ordinal, segment_hash, source_start, source_end, boundary,
                       duration_sec, sample_rate, frame_count, trim_start,
                       generation_json
                FROM narration_segments
                WHERE narration_id = ? ORDER BY ordinal
                """,
                (narration_id,),
            ).fetchall()
            gaps = self._connection.execute(
                """
                SELECT ordinal, source_start, source_end, error_code, created_at
                FROM gaps WHERE narration_id = ? ORDER BY ordinal
                """,
                (narration_id,),
            ).fetchall()
        return self._stored_narration(row, segments, gaps)

    def segment_range(
        self, narration_id: str, ordinal: int, segment_hash: str
    ) -> SegmentRange | None:
        """One Block's recorded range, or None while it is unmeasured."""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT sample_rate, frame_count, trim_start
                FROM narration_segments
                WHERE narration_id = ? AND ordinal = ? AND segment_hash = ?
                """,
                (narration_id, ordinal, segment_hash),
            ).fetchone()
        if row is None or row["trim_start"] is None:
            return None
        return SegmentRange(
            int(row["sample_rate"]), int(row["frame_count"]), int(row["trim_start"])
        )

    def list_narrations(self) -> tuple[NarrationListing, ...]:
        """Every Narration, newest first, in three queries rather than
        three per Narration, and without any Narration's Source.

        The History screen is the one caller that wants all of them at
        once, so reading them one at a time turns a page render into a
        query count that grows with the user's whole History, and reading
        every Source makes it cost the size of everything ever narrated.
        """
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, substr(source, 1, ?) AS source_preview, model_id,
                       catalog_version, voice_id, speed, prepare_first,
                       pause_policy_version, pause_sentence_ms,
                       pause_paragraph_break_ms, status, playhead_sec,
                       total_duration_sec, created_at, updated_at,
                       last_played_at
                FROM narrations ORDER BY created_at DESC, id DESC
                """,
                (SOURCE_PREVIEW_CHARS,),
            ).fetchall()
            segment_rows = self._connection.execute(
                """
                SELECT narration_id, ordinal, segment_hash, source_start,
                       source_end, boundary, duration_sec, sample_rate,
                       frame_count, trim_start, generation_json
                FROM narration_segments
                ORDER BY narration_id, ordinal
                """
            ).fetchall()
            gap_rows = self._connection.execute(
                """
                SELECT narration_id, ordinal, source_start, source_end,
                       error_code, created_at
                FROM gaps ORDER BY narration_id, ordinal
                """
            ).fetchall()
        segments = _by_narration(segment_rows)
        gaps = _by_narration(gap_rows)
        return tuple(
            self._listing(
                row,
                segments.get(str(row["id"]), ()),
                gaps.get(str(row["id"]), ()),
            )
            for row in rows
        )

    def segment_recency(self) -> tuple[SegmentRecency, ...]:
        """Distinct Segment keys, oldest effective use first.

        Shared audio belongs to every Narration that references it, so its
        retention clock is the newest of their play clocks. Replaying either
        Narration therefore protects the shared Segment from eviction.
        """
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT segment_references.segment_hash,
                       MAX(narrations.last_played_at) AS last_played_at
                FROM segment_references
                JOIN narrations
                  ON narrations.id = segment_references.narration_id
                GROUP BY segment_references.segment_hash
                ORDER BY last_played_at, segment_references.segment_hash
                """
            ).fetchall()
        return tuple(
            SegmentRecency(
                segment_hash=str(row["segment_hash"]),
                last_played_at=datetime.fromisoformat(row["last_played_at"]),
            )
            for row in rows
        )

    def delete_narration(self, narration_id: str) -> tuple[str, ...] | None:
        """Drop one permanent record and return its distinct Segment keys."""
        with self._transaction():
            exists = self._connection.execute(
                "SELECT 1 FROM narrations WHERE id = ?", (narration_id,)
            ).fetchone()
            if exists is None:
                return None
            rows = self._connection.execute(
                """
                SELECT DISTINCT segment_hash
                FROM segment_references
                WHERE narration_id = ?
                ORDER BY segment_hash
                """,
                (narration_id,),
            ).fetchall()
            self._connection.execute(
                "DELETE FROM narrations WHERE id = ?", (narration_id,)
            )
        return tuple(str(row["segment_hash"]) for row in rows)

    def set_status(
        self,
        narration_id: str,
        status: NarrationStatus,
        *,
        playhead_sec: float | None = None,
        total_duration_sec: float | None = None,
    ) -> None:
        now = self._timestamp()
        assignments = ["status = ?", "updated_at = ?"]
        values: list[object] = [status.value, now]
        if playhead_sec is not None:
            assignments.extend(["playhead_sec = ?", "last_played_at = ?"])
            values.extend([playhead_sec, now])
        if total_duration_sec is not None:
            assignments.append("total_duration_sec = ?")
            values.append(total_duration_sec)
        values.append(narration_id)
        with self._transaction():
            cursor = self._connection.execute(
                f"UPDATE narrations SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
            self._require_update(cursor, narration_id)

    def checkpoint(self, narration_id: str, playhead_sec: float) -> None:
        """Record where the playhead is — not how far it has ever been.

        The stored playhead is where Resume starts, so a seek backwards has
        to be able to lower it. Ordering stale writes out is the caller's
        job (`GenerationWorker` gates every durable write on the generation
        that produced it); a `max` here would trade a backward seek that
        never survives a Stop for a guard that belongs upstream anyway.
        """
        now = self._timestamp()
        with self._transaction():
            cursor = self._connection.execute(
                """
                UPDATE narrations
                SET playhead_sec = ?, updated_at = ?, last_played_at = ?
                WHERE id = ?
                """,
                (playhead_sec, now, now, narration_id),
            )
            self._require_update(cursor, narration_id)

    def invalidate_segment_audio(self, segment_hash: str) -> None:
        """Forget shared audio facts before replacing an evicted artifact.

        Only the Block rows: a Narration's total is what its listener heard
        and stays true whichever artifact now backs one of its Blocks.
        """
        with self._transaction():
            self._connection.execute(
                """
                UPDATE narration_segments
                SET duration_sec = NULL, sample_rate = NULL,
                    frame_count = NULL, trim_start = NULL
                WHERE segment_hash = ?
                """,
                (segment_hash,),
            )

    def complete_segment(
        self,
        narration_id: str,
        ordinal: int,
        *,
        segment_hash: str,
        duration_sec: float,
        sample_rate: int,
        frame_count: int,
        trim_start: int,
    ) -> bool:
        """Record one Block's audio facts and clear any gap it had; did it land?

        A Block with audio is by definition not a gap, so the two writes are
        one transaction: a Block that failed, was recorded as a gap, and then
        succeeded on a retry or a later Resume would otherwise keep saying
        both — `audioPresent` true and a `gaps` entry for the same ordinal —
        for the life of the Narration. Clearing here rather than at the call
        sites covers every path that can produce audio: first synthesis,
        retry, and the duration backfill a cached decode performs.

        False means the Block no longer answers to `segment_hash`, which is
        legitimate: a reroll or a take selection can swap the row's key while
        the outgoing generation is still being measured. Those audio facts
        describe a Block nobody is playing any more, so they are dropped
        rather than written over the take the Reader chose. Raises `KeyError`
        when the Narration has no such Block at all.
        """
        with self._transaction():
            cursor = self._connection.execute(
                """
                UPDATE narration_segments
                SET duration_sec = ?, sample_rate = ?, frame_count = ?, trim_start = ?
                WHERE narration_id = ? AND ordinal = ? AND segment_hash = ?
                """,
                (
                    duration_sec,
                    sample_rate,
                    frame_count,
                    trim_start,
                    narration_id,
                    ordinal,
                    segment_hash,
                ),
            )
            if cursor.rowcount == 0:
                exists = self._connection.execute(
                    "SELECT 1 FROM narration_segments WHERE narration_id = ? "
                    "AND ordinal = ?",
                    (narration_id, ordinal),
                ).fetchone()
                if exists is None:
                    raise KeyError(narration_id)
                return False
            self._connection.execute(
                "DELETE FROM gaps WHERE narration_id = ? AND ordinal = ?",
                (narration_id, ordinal),
            )
            return True

    def record_gap(
        self,
        narration_id: str,
        *,
        ordinal: int,
        segment_hash: str,
        source_start: int,
        source_end: int,
        error_code: str,
    ) -> bool:
        """Mark one Block unspoken, keeping its source span; did it land?

        Keyed on `segment_hash` for the same reason `complete_segment` is: a
        reroll or a take selection can swap the row's key while the outgoing
        generation is still in flight, and a failure arriving after that swap
        describes a Block nobody is playing any more. Writing it would show
        the Reader a gap over the take they just chose. Raises `KeyError`
        when the Narration has no such Block at all.
        """
        with self._transaction():
            cursor = self._connection.execute(
                """
                UPDATE narration_segments
                SET duration_sec = NULL, sample_rate = NULL,
                    frame_count = NULL, trim_start = NULL
                WHERE narration_id = ? AND ordinal = ? AND segment_hash = ?
                """,
                (narration_id, ordinal, segment_hash),
            )
            if cursor.rowcount == 0:
                exists = self._connection.execute(
                    "SELECT 1 FROM narration_segments WHERE narration_id = ? "
                    "AND ordinal = ?",
                    (narration_id, ordinal),
                ).fetchone()
                if exists is None:
                    raise KeyError(narration_id)
                return False
            self._connection.execute(
                """
                INSERT INTO gaps(
                    narration_id, ordinal, source_start, source_end,
                    error_code, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(narration_id, ordinal) DO UPDATE SET
                    source_start = excluded.source_start,
                    source_end = excluded.source_end,
                    error_code = excluded.error_code,
                    created_at = excluded.created_at
                """,
                (
                    narration_id,
                    ordinal,
                    source_start,
                    source_end,
                    error_code,
                    self._timestamp(),
                ),
            )
            return True

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _stored_narration(
        self,
        row: sqlite3.Row,
        segment_rows: Sequence[sqlite3.Row],
        gap_rows: Sequence[sqlite3.Row],
    ) -> StoredNarration:
        listing = self._listing(row, segment_rows, gap_rows)
        return StoredNarration(
            source=str(row["source"]),
            **vars(listing),
        )

    def _listing(
        self,
        row: sqlite3.Row,
        segment_rows: Sequence[sqlite3.Row],
        gap_rows: Sequence[sqlite3.Row],
    ) -> NarrationListing:
        return NarrationListing(
            id=str(row["id"]),
            source_preview=str(row["source_preview"]),
            settings=SynthesisSettings(
                model_id=str(row["model_id"]),
                catalog_version=int(row["catalog_version"]),
                voice_id=str(row["voice_id"]),
                speed=float(row["speed"]),
                prepare_first=bool(row["prepare_first"]),
                pause_policy=PausePolicy(
                    pause_policy_version=row["pause_policy_version"],
                    pause_sentence_ms=row["pause_sentence_ms"],
                    pause_paragraph_break_ms=row["pause_paragraph_break_ms"],
                ),
            ),
            status=NarrationStatus(row["status"]),
            playhead_sec=float(row["playhead_sec"]),
            total_duration_sec=(
                None
                if row["total_duration_sec"] is None
                else float(row["total_duration_sec"])
            ),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            last_played_at=datetime.fromisoformat(row["last_played_at"]),
            segments=tuple(
                StoredSegment(
                    ordinal=int(part["ordinal"]),
                    segment_hash=str(part["segment_hash"]),
                    source_start=int(part["source_start"]),
                    source_end=int(part["source_end"]),
                    boundary=str(part["boundary"]),
                    generation=(
                        GenerationRecord.from_json(part["generation_json"])
                        if part["generation_json"]
                        else None
                    ),
                    duration_sec=(
                        None
                        if part["duration_sec"] is None
                        else float(part["duration_sec"])
                    ),
                    sample_rate=(
                        None
                        if part["sample_rate"] is None
                        else int(part["sample_rate"])
                    ),
                    frame_count=(
                        None
                        if part["frame_count"] is None
                        else int(part["frame_count"])
                    ),
                    trim_start=(
                        None if part["trim_start"] is None else int(part["trim_start"])
                    ),
                )
                for part in segment_rows
            ),
            gaps=tuple(
                GapRecord(
                    ordinal=int(gap["ordinal"]),
                    source_start=int(gap["source_start"]),
                    source_end=int(gap["source_end"]),
                    error_code=str(gap["error_code"]),
                    created_at=datetime.fromisoformat(gap["created_at"]),
                )
                for gap in gap_rows
            ),
        )

    def _timestamp(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("History timestamps must be timezone-aware")
        return value.astimezone(UTC).isoformat()

    @staticmethod
    def _require_update(cursor: sqlite3.Cursor, identity: str) -> None:
        if cursor.rowcount != 1:
            raise KeyError(identity)
