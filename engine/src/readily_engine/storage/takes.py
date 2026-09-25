"""Block takes: the two immutable candidates a reroll leaves behind.

Rerolling a Block keeps the generation it replaced, so `block_takes` holds A
and B side by side while `narration_segments` names whichever one the Block
currently plays. The Segment cache is swept against `segment_references`, so
a take's audio survives for as long as the take does.
"""

import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager

from readily_engine.generation import GenerationRecord

MIGRATION_8 = (
    "CREATE TABLE block_takes ("
    "narration_id TEXT NOT NULL, ordinal INTEGER NOT NULL, "
    "take TEXT NOT NULL CHECK (take IN ('A', 'B')), "
    "segment_hash TEXT NOT NULL, generation_json TEXT NOT NULL, "
    "PRIMARY KEY (narration_id, ordinal, take), "
    "FOREIGN KEY (narration_id, ordinal) "
    "REFERENCES narration_segments(narration_id, ordinal) ON DELETE CASCADE)",
    "CREATE VIEW segment_references AS "
    "SELECT narration_id, segment_hash FROM narration_segments UNION "
    "SELECT narration_id, segment_hash FROM block_takes",
)


class BlockTakes:
    """The take half of `HistoryStore`, over the same connection and lock."""

    _connection: sqlite3.Connection
    _lock: AbstractContextManager[bool]
    _transaction: Callable[[], AbstractContextManager[None]]

    def block_takes(
        self, narration_id: str, ordinal: int
    ) -> dict[str, GenerationRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT take, generation_json FROM block_takes "
                "WHERE narration_id = ? AND ordinal = ?",
                (narration_id, ordinal),
            ).fetchall()
            return {
                row["take"]: GenerationRecord.from_json(row["generation_json"])
                for row in rows
            }

    def narration_takes(
        self, narration_id: str
    ) -> dict[int, dict[str, GenerationRecord]]:
        """Every Block's stored takes, keyed by ordinal then take name."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT ordinal, take, generation_json FROM block_takes "
                "WHERE narration_id = ?",
                (narration_id,),
            ).fetchall()
        takes: dict[int, dict[str, GenerationRecord]] = {}
        for row in rows:
            takes.setdefault(row["ordinal"], {})[row["take"]] = (
                GenerationRecord.from_json(row["generation_json"])
            )
        return takes

    def select_take(
        self,
        narration_id: str,
        ordinal: int,
        record: GenerationRecord,
        *,
        original: GenerationRecord,
    ) -> None:
        """Save both immutable takes and change the Block's selection atomically."""
        with self._transaction():
            self._connection.execute(
                "INSERT OR IGNORE INTO block_takes VALUES (?, ?, 'A', ?, ?)",
                (narration_id, ordinal, original.key, original.canonical_json()),
            )
            takes = self.block_takes(narration_id, ordinal)
            if record != takes["A"]:
                self._connection.execute(
                    "INSERT INTO block_takes VALUES (?, ?, 'B', ?, ?) "
                    "ON CONFLICT(narration_id, ordinal, take) DO UPDATE SET "
                    "segment_hash = excluded.segment_hash, "
                    "generation_json = excluded.generation_json",
                    (narration_id, ordinal, record.key, record.canonical_json()),
                )
            self._connection.execute(
                "UPDATE narration_segments SET segment_hash = ?, generation_json = ?, "
                "duration_sec = NULL, sample_rate = NULL, "
                "frame_count = NULL, trim_start = NULL "
                "WHERE narration_id = ? AND ordinal = ?",
                (record.key, record.canonical_json(), narration_id, ordinal),
            )
            self._connection.execute(
                "UPDATE narrations SET total_duration_sec = NULL WHERE id = ?",
                (narration_id,),
            )

    def replace_segment_key(
        self,
        narration_id: str,
        ordinal: int,
        segment_hash: str,
        generation: GenerationRecord,
        expected_key: str,
    ) -> bool:
        """Re-address one Block under new settings; did the row still match?

        False means the Block no longer answers to `expected_key`: a reroll
        or a take selection swapped it while this generation was in flight,
        so the settings this call carries are for a Block nobody is playing
        any more and must not overwrite the take the Reader chose. Raises
        `KeyError` when the Narration has no such Block at all.
        """
        with self._transaction():
            cursor = self._connection.execute(
                """
                UPDATE narration_segments
                SET segment_hash = ?, generation_json = ?, duration_sec = NULL,
                    sample_rate = NULL, frame_count = NULL,
                    trim_start = NULL
                WHERE narration_id = ? AND ordinal = ? AND segment_hash = ?
                """,
                (
                    segment_hash,
                    generation.canonical_json(),
                    narration_id,
                    ordinal,
                    expected_key,
                ),
            )
            if cursor.rowcount == 1:
                return True
            exists = self._connection.execute(
                "SELECT 1 FROM narration_segments WHERE narration_id = ? "
                "AND ordinal = ?",
                (narration_id, ordinal),
            ).fetchone()
            if exists is None:
                raise KeyError(narration_id)
            return False
