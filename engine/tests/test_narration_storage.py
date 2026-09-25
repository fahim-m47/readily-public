"""Callers describe Narrations and Blocks; History views never expose hashes."""

from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
import pytest
from generation_fakes import entry_for
from storage_fakes import (
    SETTINGS,
    AdvancingClock,
    create_plan,
    decode_npz,
    encode_npz,
    open_test_storage,
    publish_one,
    publish_segment,
    strip_generation_records,
)

from readily_engine.chunking import Block, Boundary, ChunkedSource
from readily_engine.narration.measure import (
    cached_block,
    recover_lengths,
    trimmed_audio,
)
from readily_engine.narration.timeline import timeline_starts
from readily_engine.storage.history import (
    DEFAULT_SEGMENT_BUDGET_BYTES,
    HistoryStore,
    NarrationStatus,
)
from readily_engine.storage.segments import SegmentStore
from readily_engine.storage.storage import NarrationNotResumable, NarrationStorage


def test_replaced_audio_is_a_cache_miss_even_when_history_has_a_measured_range(
    tmp_path,
):
    storage = open_test_storage(tmp_path)
    try:
        plan = create_plan(storage, "Shared.")
        segment = plan.segments[0]
        publish_segment(storage, plan, segment, np.ones(2400, dtype=np.float32), 24_000)
        assert cached_block(storage, plan, segment) is not None
        flac = next((tmp_path / "segments").rglob("*.flac"))
        encode_npz(np.zeros(1200, dtype=np.float32), 24_000, flac)

        assert cached_block(storage, plan, segment) is None
        assert not storage.has_verified_audio(segment)
        detail = storage.history_detail(plan.id)
        assert not detail.audio_present
        assert not detail.segments[0].audio_present

        replacement = np.ones(1200, dtype=np.float32)
        publish_segment(storage, plan, segment, replacement, 24_000)
        assert cached_block(storage, plan, segment).frame_count == 1200
        assert storage.history_detail(plan.id).audio_present
    finally:
        storage.close()


def test_history_list_only_checks_file_presence(tmp_path):
    class CountingSegments(SegmentStore):
        verifications = 0

        def has_verified(self, key: str) -> bool:
            self.verifications += 1
            return super().has_verified(key)

    segments = CountingSegments(
        tmp_path / "segments", encoder=encode_npz, decoder=decode_npz
    )
    storage = NarrationStorage(HistoryStore.open(tmp_path / "readily.db"), segments)
    try:
        plan = create_plan(storage, "Shared.")
        publish_one(storage, plan)
        segments.verifications = 0

        assert storage.history()[0].audio_present
        assert segments.verifications == 0
        assert storage.history_detail(plan.id).audio_present
        assert segments.verifications > 0
    finally:
        storage.close()


def test_published_lengths_and_timeline_use_trimmed_audio_before_playback(tmp_path):
    storage = open_test_storage(tmp_path)
    try:
        plan = storage.create(
            ChunkedSource(
                "One. Two.",
                (
                    Block("One.", 0, 4, Boundary.SENTENCE),
                    Block("Two.", 5, 9, Boundary.PARAGRAPH),
                ),
            ),
            SETTINGS,
            entry_for(SETTINGS),
        )
        pcm = np.concatenate((np.zeros(2400), np.ones(2400), np.zeros(2400))).astype(
            np.float32
        )
        for segment in plan.segments:
            publish_segment(storage, plan, segment, pcm, 24_000)
        saved = storage.plan(plan.id)
        # Version 2 retains 40ms on each side of 100ms speech.
        assert [segment.frame_count for segment in saved.segments] == [4320, 4320]
        assert timeline_starts(saved) == (0.0, 0.26)
        assert len(trimmed_audio(storage, saved, saved.segments[0]).pcm) == 4320
        original_bytes = storage.retention().audio_bytes
        changed = storage.create(
            ChunkedSource(
                "One. Two.",
                (
                    Block("One.", 0, 4, Boundary.SENTENCE),
                    Block("Two.", 5, 9, Boundary.PARAGRAPH),
                ),
            ),
            replace(
                SETTINGS,
                pause_policy=SETTINGS.pause_policy.model_copy(
                    update={"pause_sentence_ms": 300}
                ),
            ),
            entry_for(
                replace(
                    SETTINGS,
                    pause_policy=SETTINGS.pause_policy.model_copy(
                        update={"pause_sentence_ms": 300}
                    ),
                )
            ),
        )
        for segment in changed.segments:
            trimmed_audio(storage, changed, segment)
        assert timeline_starts(storage.plan(changed.id)) == (0.0, 0.48)
        assert storage.retention().audio_bytes == original_bytes

    finally:
        storage.close()


def test_playback_recovers_trimmed_lengths_from_shared_cache_without_playback(tmp_path):
    storage = open_test_storage(tmp_path)
    try:
        original = create_plan(storage, "Shared.")
        pcm = np.concatenate((np.zeros(2400), np.ones(2400), np.zeros(2400))).astype(
            np.float32
        )
        publish_segment(storage, original, original.segments[0], pcm, 24_000)
        shared = create_plan(storage, "Shared.")
        recovered = recover_lengths(storage, storage.plan(shared.id))
        assert recovered.segments[0].frame_count == 4320
        assert timeline_starts(recovered) == (0.0,)
    finally:
        storage.close()


def test_failed_replacement_does_not_keep_the_missing_audio_length(tmp_path):
    storage = open_test_storage(tmp_path)
    try:
        plan = storage.create(
            ChunkedSource(
                "One. Two.",
                (
                    Block("One.", 0, 4, Boundary.SENTENCE),
                    Block("Two.", 5, 9, Boundary.PARAGRAPH),
                ),
            ),
            SETTINGS,
            entry_for(SETTINGS),
        )
        for part in plan.segments:
            publish_segment(
                storage, plan, part, np.ones(2400, dtype=np.float32), 24_000
            )
        for audio in (tmp_path / "segments").rglob("*.flac"):
            audio.unlink()
        storage.record_gap(plan.id, plan.segments[0], "generation_failed")
        saved = storage.plan(plan.id)
        assert saved.segments[0].frame_count is None
        assert timeline_starts(saved)[1] == 0.08
    finally:
        storage.close()


def test_create_hashes_every_block_and_history_hides_hashes(tmp_path):
    storage = open_test_storage(tmp_path)
    try:
        chunked = ChunkedSource(
            "One. Two.",
            (
                Block("One.", 0, 4, Boundary.SENTENCE),
                Block(" Two.", 4, 9, Boundary.PARAGRAPH),
            ),
        )
        plan = storage.create(chunked, SETTINGS, entry_for(SETTINGS))

        assert [part.text for part in plan.segments] == ["One.", "Two."]
        detail = storage.history_detail(plan.id)
        assert detail is not None
        assert detail.source == "One. Two."
        assert detail.segments[0].audio_present is False
        assert "hash" not in repr(detail)
    finally:
        storage.close()


def test_publishing_one_shared_key_makes_both_narrations_present(tmp_path):
    storage = open_test_storage(tmp_path)
    try:
        first = create_plan(storage, "Shared.")
        second = create_plan(storage, "Shared.")
        publish_segment(
            storage,
            first,
            first.segments[0],
            np.ones(240, dtype=np.float32),
            24_000,
        )

        assert storage.history_detail(first.id).segments[0].audio_present is True
        assert storage.history_detail(second.id).segments[0].audio_present is True
        assert storage.history_detail(second.id).segments[0].duration_sec is None
        audio = trimmed_audio(storage, second, second.segments[0])
        assert audio is not None
        duration = storage.history_detail(second.id).segments[0].duration_sec
        assert duration == pytest.approx(len(audio.pcm) / audio.sample_rate)
        assert len(list((tmp_path / "segments").rglob("*.flac"))) == 1
    finally:
        storage.close()


def test_audio_is_absent_when_the_frame_count_sidecar_is_missing(tmp_path):
    """The read path needs the FLAC *and* its frame count; without the
    sidecar it decodes to the encoder's padding, so a lone FLAC is not
    audio the History may promise."""
    storage = open_test_storage(tmp_path)
    try:
        plan = create_plan(storage, "Halved.")
        publish_segment(
            storage, plan, plan.segments[0], np.ones(240, dtype=np.float32), 24_000
        )
        assert storage.history_detail(plan.id).audio_present is True

        for sidecar in (tmp_path / "segments").rglob("*.frames"):
            sidecar.unlink()

        detail = storage.history_detail(plan.id)
        assert detail.segments[0].audio_present is False
        assert detail.audio_present is False
        assert trimmed_audio(storage, plan, plan.segments[0]) is None
    finally:
        storage.close()


def test_a_measured_block_reads_without_a_history_write(tmp_path, monkeypatch):
    storage = open_test_storage(tmp_path)
    try:
        plan = create_plan(storage, "Measured.")
        storage.store_audio(
            plan.segments[0].key, np.ones(2400, dtype=np.float32), 24_000
        )
        writes: list[int] = []
        record_length = storage.record_length

        def counting_record_length(*args, **changes):
            writes.append(changes["frame_count"])
            record_length(*args, **changes)

        monkeypatch.setattr(storage, "record_length", counting_record_length)
        first = trimmed_audio(storage, plan, plan.segments[0])
        measured = storage.plan(plan.id)
        assert measured.segments[0].measured
        assert (
            cached_block(storage, measured, measured.segments[0])
            == measured.segments[0]
        )
        second = trimmed_audio(storage, measured, measured.segments[0])

        assert writes == [len(first.pcm)]
        np.testing.assert_array_equal(first.pcm, second.pcm)
    finally:
        storage.close()


def test_a_narration_with_no_blocks_has_no_audio_to_report(tmp_path):
    """`all(())` is True, which would report an empty Narration as fully
    present — and offer the user a row with nothing to play."""
    storage = open_test_storage(tmp_path)
    try:
        plan = storage.create(ChunkedSource("", ()), SETTINGS, entry_for(SETTINGS))

        assert storage.history_detail(plan.id).audio_present is False
        assert storage.history()[0].audio_present is False
    finally:
        storage.close()


def test_interrupted_stopped_and_finished_narrations_replay(tmp_path):
    storage = open_test_storage(tmp_path)
    try:
        plan = create_plan(storage, "State.")
        with pytest.raises(NarrationNotResumable):
            storage.resume(plan.id)
        storage.set_status(plan.id, NarrationStatus.STOPPED)
        assert storage.resume(plan.id).id == plan.id
        storage.set_status(
            plan.id,
            NarrationStatus.FINISHED,
            playhead_sec=3.0,
            total_duration_sec=3.0,
        )
        assert storage.resume(plan.id).playhead_sec == 0.0
    finally:
        storage.close()


def test_history_is_reverse_chronological_and_reports_gaps(tmp_path):
    clock = AdvancingClock(datetime(2026, 8, 26, tzinfo=UTC))
    storage = open_test_storage(tmp_path, clock=clock)
    try:
        older = create_plan(storage, "Older.")
        clock.advance(seconds=1)
        newer = create_plan(storage, "Newer.")
        storage.record_gap(newer.id, newer.segments[0], "generation_failed")

        summaries = storage.history()
        assert [item.id for item in summaries] == [newer.id, older.id]
        assert summaries[0].has_gaps is True
    finally:
        storage.close()


def test_budget_evicts_oldest_audio_and_stops_at_the_limit(tmp_path):
    clock = AdvancingClock(datetime(2026, 8, 26, tzinfo=UTC))
    storage = open_test_storage(tmp_path, clock=clock)
    try:
        older = create_plan(storage, "Older audio.")
        publish_one(storage, older)
        clock.advance(seconds=1)
        newer = create_plan(storage, "Newer audio.")
        publish_one(storage, newer)
        before = storage.retention()

        after = storage.update_retention(
            segment_budget_bytes=before.audio_bytes - 1,
            keep_audio_days=None,
        )

        assert storage.history_detail(older.id).audio_present is False
        assert storage.history_detail(newer.id).audio_present is True
        assert after.state.audio_bytes <= after.state.segment_budget_bytes
        # The sweep counts what it deleted rather than leaving a caller to
        # subtract two disk readings, which the Janitor's own sweeps and a
        # Narration still publishing would both make wrong.
        assert after.evicted_bytes == before.audio_bytes - after.state.audio_bytes
        assert after.evicted_bytes > 0
    finally:
        storage.close()


def test_publishing_keeps_its_audio_and_the_next_sweep_enforces_budget(tmp_path):
    """Synthesis never deletes what it just wrote; the sweep does.

    The store is allowed over budget between sweeps — the alternative is a
    full walk of the Segment tree on every published Block, which both
    slows the first-audio path and can evict the Narration now playing.
    """
    storage = open_test_storage(tmp_path)
    try:
        plan = create_plan(storage, "Too large.")
        storage.update_retention(segment_budget_bytes=1, keep_audio_days=None)

        audio = publish_one(storage, plan)

        assert len(audio.pcm) == 240
        assert storage.retention().audio_bytes > 1
        assert storage.history_detail(plan.id).audio_present is True

        storage.run_retention()

        assert storage.retention().audio_bytes <= 1
        assert storage.history_detail(plan.id).audio_present is False
    finally:
        storage.close()


def test_shared_audio_uses_the_newest_referencing_play_clock(tmp_path):
    clock = AdvancingClock(datetime(2026, 8, 26, tzinfo=UTC))
    storage = open_test_storage(tmp_path, clock=clock)
    try:
        old_shared = create_plan(storage, "Shared audio.")
        publish_one(storage, old_shared)
        clock.advance(seconds=1)
        unique = create_plan(storage, "Unique audio.")
        publish_one(storage, unique)
        clock.advance(seconds=1)
        new_shared = create_plan(storage, "Shared audio.")
        before = storage.retention()

        storage.update_retention(
            segment_budget_bytes=before.audio_bytes - 1,
            keep_audio_days=None,
        )

        assert storage.history_detail(unique.id).audio_present is False
        assert storage.history_detail(old_shared.id).audio_present is True
        assert storage.history_detail(new_shared.id).audio_present is True
    finally:
        storage.close()


def test_replaying_within_the_age_window_protects_audio_from_eviction(tmp_path):
    clock = AdvancingClock(datetime(2026, 8, 1, tzinfo=UTC))
    storage = open_test_storage(tmp_path, clock=clock)
    try:
        plan = create_plan(storage, "Replay protects this.")
        publish_one(storage, plan)
        clock.advance(seconds=10 * 24 * 60 * 60)
        storage.checkpoint(plan.id, 0.5)
        clock.advance(seconds=6 * 24 * 60 * 60)

        storage.update_retention(
            segment_budget_bytes=storage.retention().segment_budget_bytes,
            keep_audio_days=7,
        )

        assert storage.history_detail(plan.id).audio_present is True
    finally:
        storage.close()


def test_audio_older_than_the_age_window_is_evicted_on_the_next_sweep(tmp_path):
    clock = AdvancingClock(datetime(2026, 8, 1, tzinfo=UTC))
    storage = open_test_storage(tmp_path, clock=clock)
    try:
        plan = create_plan(storage, "This ages out.")
        publish_one(storage, plan)
        storage.update_retention(
            segment_budget_bytes=storage.retention().segment_budget_bytes,
            keep_audio_days=7,
        )
        clock.advance(seconds=8 * 24 * 60 * 60)

        storage.run_retention()

        assert storage.history_detail(plan.id).audio_present is False
    finally:
        storage.close()


def test_never_keeps_audio_no_matter_how_old_it_is(tmp_path):
    clock = AdvancingClock(datetime(2026, 8, 1, tzinfo=UTC))
    storage = open_test_storage(tmp_path, clock=clock)
    try:
        plan = create_plan(storage, "Kept forever.")
        publish_one(storage, plan)
        storage.update_retention(
            segment_budget_bytes=storage.retention().segment_budget_bytes,
            keep_audio_days=None,
        )
        clock.advance(seconds=5000 * 24 * 60 * 60)

        storage.run_retention()

        assert storage.retention().keep_audio_days is None
        assert storage.history_detail(plan.id).audio_present is True
    finally:
        storage.close()


def test_an_absurd_age_window_evicts_nothing_instead_of_raising(tmp_path):
    """A policy row past `datetime` range must not brick the sweep.

    The wire rejects these, so only a hand-edited database gets here — but
    this sweep runs on the Janitor thread and at every startup, so raising
    would take the Engine down rather than skip one eviction rule.
    """
    clock = AdvancingClock(datetime(2026, 8, 1, tzinfo=UTC))
    storage = open_test_storage(tmp_path, clock=clock)
    try:
        plan = create_plan(storage, "Survives an absurd policy.")
        publish_one(storage, plan)
        storage.update_retention(
            segment_budget_bytes=DEFAULT_SEGMENT_BUDGET_BYTES,
            keep_audio_days=10**9,
        )

        storage.run_retention()

        assert storage.history_detail(plan.id).audio_present is True
    finally:
        storage.close()


def test_delete_keeps_shared_segments_until_the_last_reference_is_gone(tmp_path):
    storage = open_test_storage(tmp_path)
    try:
        first = create_plan(storage, "Shared forever.")
        second = create_plan(storage, "Shared forever.")
        publish_one(storage, first)

        first_result = storage.delete(first.id)

        assert first_result is not None
        assert first_result.audio_bytes_freed == 0
        assert storage.history_detail(first.id) is None
        assert storage.history_detail(second.id).audio_present is True

        second_result = storage.delete(second.id)

        assert second_result is not None
        assert second_result.audio_bytes_freed > 0
        assert storage.history_detail(second.id) is None
        assert storage.retention().audio_bytes == 0
        assert storage.delete(second.id) is None
    finally:
        storage.close()


def test_retention_sweeps_orphaned_segment_artifacts(tmp_path):
    """Audio no Narration references — a crashed launch's leftovers — is
    collected by the sweep, not by constructing storage: opening a store
    should not delete files."""
    orphan_store = SegmentStore(
        tmp_path / "segments", encoder=encode_npz, decoder=decode_npz
    )
    orphan_store.write("a" * 64, np.ones(240, dtype=np.float32), 24_000)
    assert orphan_store.disk_usage() > 0

    storage = open_test_storage(tmp_path)
    try:
        assert storage.retention().audio_bytes > 0

        storage.run_retention()

        assert storage.retention().audio_bytes == 0
    finally:
        storage.close()


def test_generation_identity_survives_reopen_and_ignores_position_and_speed(tmp_path):
    from dataclasses import replace

    entry = entry_for(SETTINGS)
    storage = open_test_storage(tmp_path)
    first = storage.create(
        ChunkedSource("One.", (Block("One.", 0, 4, Boundary.PARAGRAPH),)),
        SETTINGS,
        entry,
    )
    second = storage.create(
        ChunkedSource(
            "Two. One.",
            (
                Block("Two.", 0, 4, Boundary.SENTENCE),
                Block(" One.", 4, 9, Boundary.PARAGRAPH),
            ),
        ),
        replace(SETTINGS, speed=3.0),
        entry,
    )
    expected = first.segments[0].generation
    assert expected == second.segments[1].generation
    assert first.segments[0].key == second.segments[1].key == expected.key
    storage.set_status(second.id, NarrationStatus.STOPPED)
    storage.close()

    reopened = open_test_storage(tmp_path)
    try:
        resumed = reopened.resume(second.id)
        assert resumed.settings.speed == 3.0
        assert resumed.segments[1].generation == expected
        assert resumed.segments[1].key == expected.key
    finally:
        reopened.close()


def test_legacy_audio_is_not_advertised_as_available_for_replay_or_export(tmp_path):
    storage = open_test_storage(tmp_path)
    try:
        plan = create_plan(storage, "Saved audio.")
        publish_segment(
            storage,
            plan,
            plan.segments[0],
            np.ones(240, dtype=np.float32),
            24_000,
        )
        strip_generation_records(tmp_path / "readily.db")
        assert not storage.history_detail(plan.id).audio_present
        assert not storage.history_detail(plan.id).segments[0].audio_present
        assert not storage.history()[0].audio_present
    finally:
        storage.close()
