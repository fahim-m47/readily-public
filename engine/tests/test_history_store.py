"""SQLite keeps Narration History permanent and internally consistent."""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from generation_fakes import record
from storage_fakes import SETTINGS

from readily_engine.catalog import PausePolicy
from readily_engine.storage.history import (
    _MIGRATIONS,
    DEFAULT_SEGMENT_BUDGET_BYTES,
    SOURCE_PREVIEW_CHARS,
    HistorySchemaError,
    HistoryStore,
    NarrationStatus,
    NewStoredSegment,
    RetentionSettings,
)


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 26, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int = 1) -> None:
        self.value += timedelta(seconds=seconds)


def segment(
    ordinal: int = 0,
    segment_hash: str = "a" * 64,
    source_start: int = 0,
    source_end: int = 4,
    boundary: str = "paragraph",
) -> NewStoredSegment:
    return NewStoredSegment(
        ordinal=ordinal,
        segment_hash=segment_hash,
        source_start=source_start,
        source_end=source_end,
        boundary=boundary,
        generation=record(),
    )


def create_sample_narration(
    store: HistoryStore,
    *,
    narration_id: str = "n-1",
    source: str = "One.",
):
    return store.create_narration(
        narration_id=narration_id,
        source=source,
        settings=SETTINGS,
        segments=(segment(source_end=len(source)),),
    )


def test_open_migrates_once_and_enables_durable_connection_policy(tmp_path):
    database = tmp_path / "readily.db"
    first = HistoryStore.open(database)
    first.close()

    second = HistoryStore.open(database)
    try:
        assert second._connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert second._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert second._connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert second.migration_versions() == (1, 2, 3, 4, 5, 6, 7, 8)
        assert second.settings() == RetentionSettings(
            selected_model_id=None,
            selected_voice_id=None,
            speed=1.0,
            segment_budget_bytes=DEFAULT_SEGMENT_BUDGET_BYTES,
            keep_audio_days=None,
        )
    finally:
        second.close()


def test_a_narrations_pause_policy_survives_reopening_history(tmp_path):
    database = tmp_path / "readily.db"
    policy = PausePolicy(
        pause_policy_version=2, pause_sentence_ms=300, pause_paragraph_break_ms=800
    )
    store = HistoryStore.open(database)
    store.create_narration(
        narration_id="saved",
        source="One.",
        settings=replace(SETTINGS, pause_policy=policy),
        segments=(segment(),),
    )
    store.close()

    reopened = HistoryStore.open(database)
    try:
        assert reopened.get_narration("saved").settings.pause_policy == policy
    finally:
        reopened.close()


def test_migration_pins_old_narrations_to_the_original_pause_and_trim_policy(tmp_path):
    database = tmp_path / "readily.db"
    store = HistoryStore.open(database)
    create_sample_narration(store)
    store.close()
    # Recreate a v2 database: no assembly settings existed on its Narrations.
    with sqlite3.connect(database) as connection:
        for column in (
            "pause_policy_version",
            "pause_sentence_ms",
            "pause_paragraph_break_ms",
        ):
            connection.execute(f"ALTER TABLE narrations DROP COLUMN {column}")
        connection.execute("ALTER TABLE narration_segments DROP COLUMN trim_start")
        connection.execute(
            "ALTER TABLE narration_segments ADD COLUMN timeline_start_sec REAL"
        )
        connection.execute("ALTER TABLE narration_segments DROP COLUMN generation_json")
        connection.execute("ALTER TABLE settings DROP COLUMN control_overrides")
        connection.execute("ALTER TABLE narrations DROP COLUMN prepare_first")
        connection.execute("DROP VIEW segment_references")
        connection.execute("DROP TABLE block_takes")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 3")

    migrated = HistoryStore.open(database)
    try:
        policy = migrated.get_narration("n-1").settings.pause_policy
        assert policy.pause_policy_version == 1
        assert policy.pause_sentence_ms == 80
        assert policy.pause_paragraph_break_ms == 400
        assert policy.trim_margin_ms == 0
    finally:
        migrated.close()


def test_open_refuses_a_database_created_by_newer_engine_code(tmp_path):
    database = tmp_path / "readily.db"
    store = HistoryStore.open(database)
    store._connection.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
        (999, "2026-08-26T00:00:00+00:00"),
    )
    store._connection.commit()
    store.close()

    with pytest.raises(HistorySchemaError, match="newer schema"):
        HistoryStore.open(database)


@pytest.mark.parametrize(
    "active_status", [NarrationStatus.PREPARING, NarrationStatus.PLAYING]
)
def test_open_marks_abandoned_active_narrations_interrupted(tmp_path, active_status):
    database = tmp_path / "readily.db"
    store = HistoryStore.open(database)
    narration = create_sample_narration(store)
    store.set_status(narration.id, active_status)
    store.close()

    reopened = HistoryStore.open(database)
    try:
        assert (
            reopened.get_narration(narration.id).status is NarrationStatus.INTERRUPTED
        )
    finally:
        reopened.close()


def test_create_narration_commits_full_ordered_manifest(tmp_path):
    store = HistoryStore.open(tmp_path / "readily.db")
    try:
        created = store.create_narration(
            narration_id="n-1",
            source="One. Two.",
            settings=SETTINGS,
            segments=(
                segment(0, "a" * 64, 0, 4, "sentence"),
                segment(1, "b" * 64, 4, 9, "paragraph"),
            ),
        )

        assert created.source == "One. Two."
        assert created.settings == SETTINGS
        assert created.status is NarrationStatus.PREPARING
        assert [part.ordinal for part in created.segments] == [0, 1]
        assert created.segments[1].source_end == 9
        assert created.segments[1].boundary == "paragraph"
    finally:
        store.close()


def test_invalid_manifest_row_rolls_back_the_narration_and_every_segment(tmp_path):
    store = HistoryStore.open(tmp_path / "readily.db")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            store.create_narration(
                narration_id="n-bad",
                source="One. Two.",
                settings=SETTINGS,
                segments=(
                    segment(0, "a" * 64, 0, 4, "sentence"),
                    segment(1, "not-a-hash", 4, 9, "paragraph"),
                ),
            )

        assert store.get_narration("n-bad") is None
        count = store._connection.execute(
            "SELECT count(*) FROM narration_segments"
        ).fetchone()[0]
        assert count == 0
    finally:
        store.close()


def test_segment_completion_and_rekey_are_atomic_metadata_updates(tmp_path):
    store = HistoryStore.open(tmp_path / "readily.db")
    try:
        narration = create_sample_narration(store)
        store.complete_segment(
            narration.id,
            0,
            segment_hash=narration.segments[0].segment_hash,
            duration_sec=1.25,
            sample_rate=24_000,
            frame_count=30_000,
            trim_start=960,
        )
        completed = store.get_narration(narration.id).segments[0]
        assert completed.duration_sec == 1.25
        assert completed.sample_rate == 24_000
        assert completed.frame_count == 30_000

        store.replace_segment_key(
            narration.id, 0, "b" * 64, record("Rekeyed."), completed.segment_hash
        )

        rekeyed = store.get_narration(narration.id).segments[0]
        assert rekeyed.segment_hash == "b" * 64
        assert rekeyed.duration_sec is None
        assert rekeyed.sample_rate is None
        assert rekeyed.frame_count is None
    finally:
        store.close()


def test_finishing_sets_the_playhead_total_and_play_clock(tmp_path):
    clock = Clock()
    store = HistoryStore.open(tmp_path / "readily.db", clock=clock)
    try:
        narration = create_sample_narration(store)
        initial_last_played = narration.last_played_at
        clock.advance()
        store.set_status(
            narration.id,
            NarrationStatus.FINISHED,
            playhead_sec=4.0,
            total_duration_sec=4.0,
        )

        finished = store.get_narration(narration.id)
        assert finished.playhead_sec == 4.0
        assert finished.total_duration_sec == 4.0
        assert finished.last_played_at > initial_last_played
        assert finished.status is NarrationStatus.FINISHED
    finally:
        store.close()


def test_selected_synthesis_settings_do_not_replace_retention_knobs(tmp_path):
    store = HistoryStore.open(tmp_path / "readily.db")
    try:
        store.update_retention(segment_budget_bytes=123_456, keep_audio_days=30)
        store.set_playback_speed(1.2)
        selected = replace(SETTINGS, model_id="qwen3-tts:0.6b", voice_id="Chelsie")
        store.select_synthesis_settings(selected)

        assert store.settings() == RetentionSettings(
            selected_model_id="qwen3-tts:0.6b",
            selected_voice_id="Chelsie",
            speed=1.2,
            segment_budget_bytes=123_456,
            keep_audio_days=30,
        )
    finally:
        store.close()


def test_a_chosen_voice_survives_a_restart_and_leaves_speed_alone(tmp_path):
    """Choosing a Voice without narrating is the case the picker has to
    survive: the app can be quit before a single Narration is made."""
    database = tmp_path / "readily.db"
    store = HistoryStore.open(database)
    try:
        store.set_playback_speed(1.5)
        store.select_voice(model_id="qwen3-tts:0.6b", voice_id="Chelsie")
    finally:
        store.close()

    reopened = HistoryStore.open(database)
    try:
        settings = reopened.settings()
    finally:
        reopened.close()

    assert settings.selected_model_id == "qwen3-tts:0.6b"
    assert settings.selected_voice_id == "Chelsie"
    # Speed is a separate choice, and picking a Voice is not a reason to
    # forget it.
    assert settings.speed == 1.5


def test_gaps_persist_with_source_ranges_and_history_is_newest_first(tmp_path):
    clock = Clock()
    store = HistoryStore.open(tmp_path / "readily.db", clock=clock)
    try:
        older = create_sample_narration(store, narration_id="older")
        clock.advance()
        newer = create_sample_narration(store, narration_id="newer")
        store.record_gap(
            newer.id,
            ordinal=0,
            segment_hash=newer.segments[0].segment_hash,
            source_start=0,
            source_end=4,
            error_code="generation_failed",
        )

        assert [item.id for item in store.list_narrations()] == [newer.id, older.id]
        gap = store.get_narration(newer.id).gaps[0]
        assert (gap.ordinal, gap.source_start, gap.source_end) == (0, 0, 4)
        assert gap.error_code == "generation_failed"
    finally:
        store.close()


def test_listing_the_history_does_not_cost_a_query_per_narration(tmp_path):
    """The History screen asks for every Narration at once. Reading them
    one at a time makes a page render cost three queries per row, so the
    screen gets slower the longer the user has been using Readily."""
    store = HistoryStore.open(tmp_path / "readily.db")
    try:
        for index in range(12):
            store.create_narration(
                narration_id=f"n-{index}",
                source="Read locally.",
                settings=SETTINGS,
                segments=[
                    segment(
                        ordinal=ordinal,
                        segment_hash=f"{index:02x}{ordinal:02x}" + "0" * 60,
                        source_start=ordinal,
                        source_end=ordinal + 1,
                        boundary="sentence",
                    )
                    for ordinal in range(3)
                ],
            )
        statements: list[str] = []
        store._connection.set_trace_callback(statements.append)

        listed = store.list_narrations()

        store._connection.set_trace_callback(None)
        assert len(listed) == 12
        assert [item.id for item in listed] == [
            f"n-{index}" for index in range(11, -1, -1)
        ]
        assert [len(item.segments) for item in listed] == [3] * 12
        assert len(statements) == 3
    finally:
        store.close()


def test_listing_the_history_reads_a_preview_of_each_source_and_not_the_source(
    tmp_path,
):
    """A Source can be a million characters and the History list shows 160
    of it, so the list asks SQLite for the preview alone."""
    store = HistoryStore.open(tmp_path / "readily.db")
    source = "".join(f"Sentence {n} of a very long Source. " for n in range(2000))
    try:
        created = store.create_narration(
            narration_id="long", source=source, settings=SETTINGS, segments=[segment()]
        )
        statements: list[str] = []
        store._connection.set_trace_callback(statements.append)

        (listed,) = store.list_narrations()

        store._connection.set_trace_callback(None)
        assert created.source == source
        assert created.source_preview == source[:SOURCE_PREVIEW_CHARS]
        assert listed.source_preview == source[:SOURCE_PREVIEW_CHARS]
        assert not hasattr(listed, "source")
        narrations_query = next(sql for sql in statements if "FROM narrations" in sql)
        assert "substr(source, 1," in narrations_query
        assert "*" not in narrations_query
    finally:
        store.close()


def test_the_playhead_follows_a_seek_backwards(tmp_path):
    """The stored playhead is where Resume starts, so it has to be able to
    move back — a clamp here means a listener who seeks back and stops
    resumes at the position they were trying to leave."""
    store = HistoryStore.open(tmp_path / "readily.db")
    try:
        narration = store.create_narration(
            narration_id="n-1",
            source="Read locally.",
            settings=SETTINGS,
            segments=[segment(source_end=13)],
        )
        store.checkpoint(narration.id, 30.0)
        store.checkpoint(narration.id, 5.0)

        assert store.get_narration(narration.id).playhead_sec == 5.0

        store.set_status(narration.id, NarrationStatus.STOPPED, playhead_sec=2.0)

        assert store.get_narration(narration.id).playhead_sec == 2.0
    finally:
        store.close()


def test_completing_a_block_clears_the_gap_it_was_recorded_as(tmp_path):
    """One Block cannot be both spoken and missing.

    A Block that failed, was recorded as a gap, and then succeeded on a
    retry or a later Resume would otherwise keep reporting both for the
    life of the Narration.
    """
    store = HistoryStore.open(tmp_path / "readily.db")
    try:
        narration = create_sample_narration(store)
        store.record_gap(
            narration.id,
            ordinal=0,
            segment_hash=narration.segments[0].segment_hash,
            source_start=0,
            source_end=4,
            error_code="generation_failed",
        )
        assert store.get_narration(narration.id).gaps

        store.complete_segment(
            narration.id,
            0,
            segment_hash=narration.segments[0].segment_hash,
            duration_sec=1.0,
            sample_rate=24_000,
            frame_count=24_000,
            trim_start=0,
        )

        assert store.get_narration(narration.id).gaps == ()
    finally:
        store.close()


def test_a_reroll_beats_the_outgoing_generation_to_every_write_on_the_block(tmp_path):
    """The Block the Reader just chose is not overwritten by the one it replaced.

    Selecting a take re-addresses the row, so anything still in flight for
    the outgoing generation — its measurements, a re-address of its own, or
    the gap its failure records — arrives naming a key the Block no longer
    has. Each such write says so instead of landing.
    """
    store = HistoryStore.open(tmp_path / "readily.db")
    try:
        narration = create_sample_narration(store)
        outgoing = narration.segments[0].segment_hash
        chosen = record(text="Rerolled")
        store.select_take(narration.id, 0, chosen, original=record())
        assert store.get_narration(narration.id).segments[0].segment_hash == chosen.key

        assert not store.complete_segment(
            narration.id,
            0,
            segment_hash=outgoing,
            duration_sec=1.0,
            sample_rate=24_000,
            frame_count=24_000,
            trim_start=0,
        )
        assert not store.replace_segment_key(
            narration.id, 0, "b" * 64, record(text="Restyled"), outgoing
        )
        assert not store.record_gap(
            narration.id,
            ordinal=0,
            segment_hash=outgoing,
            source_start=0,
            source_end=4,
            error_code="generation_failed",
        )

        block = store.get_narration(narration.id).segments[0]
        assert block.segment_hash == chosen.key
        assert block.duration_sec is None
        assert store.get_narration(narration.id).gaps == ()
    finally:
        store.close()


def test_writing_to_a_block_no_narration_has_is_a_bug_not_a_race(tmp_path):
    """A missing Block is a caller error, unlike a Block that moved on.

    All three predicated writes draw the line in the same place, so a caller
    reading one of them has read all three.
    """
    store = HistoryStore.open(tmp_path / "readily.db")
    try:
        narration = create_sample_narration(store)
        with pytest.raises(KeyError):
            store.record_gap(
                narration.id,
                ordinal=7,
                segment_hash="a" * 64,
                source_start=0,
                source_end=4,
                error_code="generation_failed",
            )
        with pytest.raises(KeyError):
            store.complete_segment(
                narration.id,
                7,
                segment_hash="a" * 64,
                duration_sec=1.0,
                sample_rate=24_000,
                frame_count=24_000,
                trim_start=0,
            )
        with pytest.raises(KeyError):
            store.replace_segment_key(narration.id, 7, "b" * 64, record(), "a" * 64)
    finally:
        store.close()


def build_schema_3_history(database, *, speed: float) -> None:
    """Build a legacy History with the shipped migrations, not hand-written SQL."""
    with sqlite3.connect(database) as connection:
        for version in (1, 2, 3):
            for statement in _MIGRATIONS[version]:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "2026-08-26T00:00:00+00:00"),
            )
        connection.execute("UPDATE settings SET speed = ?", (speed,))
        connection.execute(
            """
            INSERT INTO narrations(
                id, source, model_id, catalog_version, voice_id, speed, status,
                total_duration_sec, created_at, updated_at, last_played_at
            ) VALUES ('n-1', 'One.', 'kokoro', 1, 'af_heart', ?, 'finished',
                      1.0, '2026-08-26T00:00:00+00:00', '2026-08-26T00:00:00+00:00',
                      '2026-08-26T00:00:00+00:00')
            """,
            (speed,),
        )
        connection.execute(
            """
            INSERT INTO narration_segments(
                narration_id, ordinal, segment_hash, source_start, source_end,
                boundary, duration_sec, sample_rate, frame_count, timeline_start_sec
            ) VALUES ('n-1', 0, ?, 0, 4, 'paragraph', 1.0, 24000, 24000, 0.0)
            """,
            ("a" * 64,),
        )
    connection.close()


def build_schema_5_history(database, *, speed: float) -> None:
    """Build the unconstrained settings schema shipped before live speed."""
    with sqlite3.connect(database) as connection:
        for version in (1, 2, 3, 4, 5):
            for statement in _MIGRATIONS[version]:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "2026-08-26T00:00:00+00:00"),
            )
        connection.execute("UPDATE settings SET speed = ?", (speed,))


@pytest.mark.parametrize("saved, expected", [(0.1, 0.5), (4.0, 3.0)])
def test_live_speed_migration_clamps_legacy_settings(tmp_path, saved, expected):
    database = tmp_path / "readily.db"
    build_schema_5_history(database, speed=saved)

    migrated = HistoryStore.open(database)
    try:
        assert migrated.migration_versions() == (1, 2, 3, 4, 5, 6, 7, 8)
        assert migrated.settings().speed == expected
    finally:
        migrated.close()


def test_generation_migration_preserves_saved_speeds_and_removes_old_bounds(tmp_path):
    database = tmp_path / "readily.db"
    build_schema_3_history(database, speed=1.5)
    migrated = HistoryStore.open(database)
    try:
        assert migrated.migration_versions() == (1, 2, 3, 4, 5, 6, 7, 8)
        assert migrated.settings().speed == 1.5
        assert migrated.get_narration("n-1").settings.speed == 1.5
        assert migrated.get_narration("n-1").segments[0].generation is None
        migrated.set_playback_speed(3.0)
        migrated.create_narration(
            narration_id="fast",
            source="One.",
            settings=replace(SETTINGS, speed=3.0),
            segments=(segment(),),
        )
        assert migrated.settings().speed == 3.0
        assert migrated.get_narration("fast").settings.speed == 3.0
    finally:
        migrated.close()
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()
