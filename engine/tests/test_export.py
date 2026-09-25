"""Export writes what was heard, once, to somewhere the user chose."""

import sys
import threading
import wave
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from conftest import ENTRY, wait_until
from generation_fakes import entry_for
from storage_fakes import (
    SETTINGS,
    decode_npz,
    encode_npz,
    open_test_storage,
    publish_segment,
    strip_generation_records,
)
from test_assembly import RawFeed
from worker_fakes import (
    RecordingPlayback,
    RecordingSynthesizer,
    voice_model,
    worker_for,
)

from readily_engine.audio.encoding import AudioEncodingError
from readily_engine.catalog import PausePolicy, Tunables
from readily_engine.chunking import Block, Boundary, ChunkedSource
from readily_engine.narration.export import (
    ExportDestinationError,
    Exporter,
    ExportInProgress,
    check_destination,
)
from readily_engine.narration.measure import trimmed_audio
from readily_engine.narration.timeline import timeline_starts
from readily_engine.narration.worker import GenerationWorker
from readily_engine.storage.history import HistoryStore, NarrationStatus
from readily_engine.storage.segments import SegmentStore
from readily_engine.storage.storage import NarrationPlan, NarrationStorage

KOKORO = Tunables.model_validate(ENTRY["tunables"])
RATE = 24_000

# Three Blocks with three different seams, so an Export that quietly
# concatenated instead of assembling would diverge on every one of them.
BOUNDARIES = (Boundary.SENTENCE, Boundary.MID_SENTENCE, Boundary.PARAGRAPH)


def tone(seed: int, frames: int = 2_400) -> np.ndarray:
    """Audible PCM — assembly trims silence, so a Block of zeros is a Block
    of nothing and would hide every seam this suite is about.

    Bounded well inside full scale: the equal-power crossfade sums two
    Segments, so a source near ±1.0 would clip in the encoder and the WAV
    round-trip below would be measuring the clip, not the assembly.
    """
    generator = np.random.default_rng(seed)
    return np.clip(generator.standard_normal(frames) * 0.2, -0.6, 0.6).astype(
        np.float32
    )


def make_plan(storage: NarrationStorage) -> NarrationPlan:
    words = ["First block here.", "second block here", "Third block here."]
    blocks: list[Block] = []
    cursor = 0
    for text, boundary in zip(words, BOUNDARIES, strict=True):
        blocks.append(Block(text, cursor, cursor + len(text), boundary))
        cursor += len(text) + 1
    source = " ".join(words)
    return storage.create(
        ChunkedSource(source=source, blocks=tuple(blocks)),
        SETTINGS,
        entry_for(SETTINGS),
    )


def publish_all(storage: NarrationStorage, plan: NarrationPlan) -> list[np.ndarray]:
    audio = [tone(index) for index in range(len(plan.segments))]
    for segment, pcm in zip(plan.segments, audio, strict=True):
        publish_segment(storage, plan, segment, pcm, RATE)
    return audio


def assembled(audio: list[np.ndarray], boundaries=BOUNDARIES) -> np.ndarray:
    """What playback would have fed for these Blocks, in one array."""
    assembler = RawFeed(KOKORO)
    pieces = [
        assembler.add(pcm, RATE, boundary)
        for pcm, boundary in zip(audio, boundaries, strict=True)
    ]
    pieces.append(assembler.flush())
    return np.concatenate(pieces)


class CapturingEncoder:
    """Stands in for afconvert: records the PCM Export handed it and writes
    a marker file, so assembly can be asserted on without a codec."""

    def __init__(self) -> None:
        self.calls = 0
        self.pcm: np.ndarray | None = None
        self.sample_rate = 0

    def __call__(self, pcm, sample_rate: int, path: Path) -> None:
        self.calls += 1
        self.pcm = np.asarray(pcm, dtype=np.float32).copy()
        self.sample_rate = sample_rate
        path.write_bytes(b"encoded")


class RefusingEncoder:
    def __call__(self, pcm, sample_rate: int, path: Path) -> None:
        del pcm, sample_rate
        path.write_bytes(b"half")
        raise AudioEncodingError("the encoder gave up")


class RecordingFiller:
    """The generation worker as Export sees it: one Block at a time."""

    def __init__(self, storage: NarrationStorage) -> None:
        self._storage = storage
        self.filled: list[int] = []

    def cached(self, plan, segment):
        return trimmed_audio(self._storage, plan, segment)

    def fill(self, plan, segment):
        self.filled.append(segment.ordinal)
        return publish_segment(
            self._storage, plan, segment, tone(segment.ordinal), RATE
        )


class RefusingFiller:
    def cached(self, plan, segment):
        del plan, segment
        return None

    def fill(self, plan, segment):
        del plan, segment
        return None


def exporter_for(tmp_path, storage, filler, encoder) -> Exporter:
    return Exporter(
        storage,
        filler,
        tmp_path / "data",
        encoders={"m4a": encoder, "wav": encoder},
    )


def finished(exporter: Exporter) -> None:
    wait_until(lambda: exporter.snapshot()["phase"] in {"finished", "failed"})


@pytest.fixture
def workspace(tmp_path):
    """A storage tree, a destination directory outside it, and a plan."""
    data = tmp_path / "data"
    data.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    storage = open_test_storage(data)
    try:
        yield storage, make_plan(storage), out
    finally:
        storage.close()


def test_an_export_is_the_narration_assembled_exactly_as_it_was_heard(
    tmp_path, workspace
):
    # ADR 0004 §4: decode, concat, encode once. "Concat" is the Assembler's
    # concat — trimmed Segments, authored pauses, one crossfade at the
    # mid-sentence seam. A plain join would be a different recording.
    storage, plan, out = workspace
    audio = publish_all(storage, plan)
    encoder = CapturingEncoder()
    exporter = exporter_for(tmp_path, storage, RecordingFiller(storage), encoder)

    exporter.start(plan.id, out / "read.m4a", "m4a")
    finished(exporter)

    assert exporter.snapshot()["phase"] == "finished"
    assert encoder.sample_rate == RATE
    np.testing.assert_array_equal(encoder.pcm, assembled(audio))


def test_cached_unplayed_blocks_have_the_same_sample_starts_in_playback_and_export(
    tmp_path, workspace
):
    storage, plan, out = workspace
    audio = [np.pad(tone(index), (2400, 2400)) for index in range(3)]
    for segment, pcm in zip(plan.segments, audio, strict=True):
        publish_segment(storage, plan, segment, pcm, RATE)
    starts = timeline_starts(storage.plan(plan.id))
    # 180ms first Block + 80ms pause; 140ms second Block minus 15ms blend.
    assert [round(start * RATE) for start in starts] == [0, 6240, 9240]
    storage.set_status(plan.id, NarrationStatus.STOPPED)
    playback = RecordingPlayback()
    synth = RecordingSynthesizer()
    worker = GenerationWorker(
        {"acme:1m": voice_model(synth, entry_for(SETTINGS))},
        playback,
        storage,
        default_model="acme:1m",
        default_voice="narrator",
    )
    try:
        worker.resume(plan.id)
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        encoder = CapturingEncoder()
        exporter = exporter_for(tmp_path, storage, worker, encoder)
        exporter.start(plan.id, out / "timeline.wav", "wav")
        finished(exporter)
        assert exporter.snapshot()["phase"] == "finished"
        np.testing.assert_array_equal(
            encoder.pcm, np.concatenate([pcm for pcm, _ in playback.played])
        )
        np.testing.assert_array_equal(encoder.pcm, assembled(audio))
        assert synth.inputs == []
    finally:
        worker.close()


def test_resume_at_a_short_crossfade_block_keeps_later_block_starts(tmp_path):
    storage = open_test_storage(tmp_path)
    plan = storage.create(
        ChunkedSource(
            "One Two Three",
            (
                Block("One", 0, 3, Boundary.MID_SENTENCE),
                Block("Two", 4, 7, Boundary.MID_SENTENCE),
                Block("Three", 8, 13, Boundary.PARAGRAPH),
            ),
        ),
        SETTINGS,
        entry_for(SETTINGS),
    )
    for segment, frames in zip(plan.segments, [2400, 480, 2400], strict=True):
        publish_segment(storage, plan, segment, tone(segment.ordinal, frames), RATE)
    # First Block lends 15ms to a 20ms second Block. Its remaining 5ms
    # cannot lend another 15ms, so the third starts at 105ms.
    assert timeline_starts(storage.plan(plan.id)) == (0.0, 0.085, 0.105)
    storage.set_status(plan.id, NarrationStatus.STOPPED, playhead_sec=0.085)
    synth = RecordingSynthesizer()
    playback = RecordingPlayback()
    worker = GenerationWorker(
        {"acme:1m": voice_model(synth, entry_for(SETTINGS))},
        playback,
        storage,
        default_model="acme:1m",
        default_voice="narrator",
    )
    try:
        worker.resume(plan.id)
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert len(playback.played[0][0]) == 480
        assert worker.snapshot()["totalSec"] == 0.205
        assert synth.inputs == []
    finally:
        worker.close()


def test_export_uses_the_saved_policy_including_trim_margin(tmp_path, workspace):
    storage, _, out = workspace
    policy = PausePolicy(pause_sentence_ms=300, pause_paragraph_break_ms=800)
    source = ChunkedSource(
        "One. Two.",
        (
            Block("One.", 0, 4, Boundary.SENTENCE),
            Block("Two.", 5, 9, Boundary.PARAGRAPH),
        ),
    )
    plan = storage.create(
        source,
        replace(SETTINGS, pause_policy=policy),
        entry_for(replace(SETTINGS, pause_policy=policy)),
    )
    pcm = np.concatenate(
        [
            np.zeros(2400, np.float32),
            np.full(2400, 0.5, np.float32),
            np.zeros(2400, np.float32),
        ]
    )
    for segment in plan.segments:
        publish_segment(storage, plan, segment, pcm, RATE)
    encoder = CapturingEncoder()
    exporter = exporter_for(tmp_path, storage, RecordingFiller(storage), encoder)
    exporter.start(plan.id, out / "saved.wav", "wav")
    finished(exporter)
    assert exporter.snapshot()["phase"] == "finished"
    margin = round(policy.trim_margin_ms / 1000 * RATE)
    pause = round(policy.pause_sentence_ms / 1000 * RATE)
    assert len(encoder.pcm) == 2 * (2400 + 2 * margin) + pause


def test_audio_already_in_the_cache_is_never_re_synthesized(tmp_path, workspace):
    # "Export never re-degrades audio that is already present": the lossless
    # Segment cache is the source of truth, so a cached Export must not
    # touch the synthesizer at all.
    storage, plan, out = workspace
    publish_all(storage, plan)
    filler = RecordingFiller(storage)
    exporter = exporter_for(tmp_path, storage, filler, CapturingEncoder())

    exporter.start(plan.id, out / "read.m4a", "m4a")
    finished(exporter)

    assert filler.filled == []


def test_export_recovers_shared_audio_on_its_worker_recording_only_lengths(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    decode_threads = []

    def decode(path):
        decode_threads.append(threading.current_thread())
        return decode_npz(path)

    storage = NarrationStorage(
        HistoryStore.open(data / "readily.db"),
        SegmentStore(data / "segments", encoder=encode_npz, decoder=decode),
    )
    exporter = exporter_for(
        tmp_path, storage, RecordingFiller(storage), CapturingEncoder()
    )
    try:
        original = make_plan(storage)
        publish_all(storage, original)
        shared = make_plan(storage)
        before = storage.history_detail(shared.id)
        decode_threads.clear()
        exporter.start(shared.id, tmp_path / "shared.wav", "wav")
        finished(exporter)
        assert exporter.snapshot()["phase"] == "finished"
        assert len(decode_threads) == len(shared.segments)
        assert all(
            thread is not threading.current_thread() for thread in decode_threads
        )
        after = storage.history_detail(shared.id)
        assert all(part.duration_sec is not None for part in after.segments)
        unmeasured = tuple(replace(part, duration_sec=None) for part in after.segments)
        assert replace(after, segments=unmeasured) == before
    finally:
        exporter.close()
        storage.close()


def test_an_evicted_narration_is_re_synthesized_into_a_complete_file(
    tmp_path, workspace
):
    storage, plan, out = workspace
    audio = publish_all(storage, plan)
    # Exactly what a retention sweep leaves behind: the History rows stay,
    # the audio is gone.
    storage.update_retention(segment_budget_bytes=1, keep_audio_days=None)
    assert all(storage.raw_audio(segment) is None for segment in plan.segments)
    filler = RecordingFiller(storage)
    encoder = CapturingEncoder()
    exporter = exporter_for(tmp_path, storage, filler, encoder)

    exporter.start(plan.id, out / "read.m4a", "m4a")
    finished(exporter)

    assert exporter.snapshot()["phase"] == "finished"
    assert filler.filled == [0, 1, 2]
    np.testing.assert_array_equal(encoder.pcm, assembled(audio))


def test_export_regenerates_only_the_replaced_measured_segment(tmp_path, workspace):
    storage, plan, out = workspace
    publish_all(storage, plan)
    key = plan.segments[1].key
    flac = tmp_path / "data" / "segments" / key[:2] / f"{key}.flac"
    encode_npz(np.zeros(100, dtype=np.float32), RATE, flac)
    synth = RecordingSynthesizer()
    worker = worker_for(synth, RecordingPlayback(), storage)
    encoder = CapturingEncoder()
    exporter = exporter_for(tmp_path, storage, worker, encoder)
    try:
        exporter.start(plan.id, out / "read.wav", "wav")
        finished(exporter)

        assert exporter.snapshot()["phase"] == "finished"
        assert synth.inputs == ["second block here"]
        assert encoder.calls == 1
        assert storage.history_detail(plan.id).audio_present
    finally:
        exporter.close()
        worker.close()


@pytest.mark.parametrize("version, inputs", [(3, ["Saved Qwen."]), (4, [])])
def test_export_refills_qwen_audio_from_before_reference_pinning(
    tmp_path, version, inputs
):
    storage = open_test_storage(tmp_path / "data")
    source = "Saved Qwen."
    plan = storage.create(
        ChunkedSource(source, (Block(source, 0, len(source), Boundary.PARAGRAPH),)),
        replace(SETTINGS, model_id="qwen3-tts:0.6b", catalog_version=version),
        entry_for(
            replace(SETTINGS, model_id="qwen3-tts:0.6b", catalog_version=version)
        ),
    )
    publish_all(storage, plan)
    if version < 4:
        strip_generation_records(tmp_path / "data" / "readily.db")
    synth = RecordingSynthesizer()
    worker = GenerationWorker(
        {
            "qwen3-tts:0.6b": voice_model(
                synth,
                entry_for(
                    replace(SETTINGS, model_id="qwen3-tts:0.6b", catalog_version=4)
                ),
            )
        },
        RecordingPlayback(),
        storage,
        default_model="qwen3-tts:0.6b",
        default_voice="Chelsie",
    )
    exporter = exporter_for(tmp_path, storage, worker, CapturingEncoder())
    try:
        exporter.start(plan.id, tmp_path / "read.m4a", "m4a")
        finished(exporter)
        assert exporter.snapshot()["phase"] == "finished"
        assert synth.inputs == inputs
        exporter.start(plan.id, tmp_path / "repeated.m4a", "m4a")
        finished(exporter)
        assert exporter.snapshot()["phase"] == "finished"
        assert synth.inputs == inputs
    finally:
        exporter.close()
        worker.close()
        storage.close()


def test_a_block_already_recorded_as_a_gap_is_replayed_as_a_gap(tmp_path, workspace):
    # A gap is a Block synthesis gave up on twice. Re-attempting it during
    # Export would make the file diverge from what the listener heard, and
    # would let one broken Block stall the whole Export.
    storage, plan, out = workspace
    for segment in plan.segments:
        if segment.ordinal == 1:
            storage.record_gap(plan.id, segment, "generation_failed")
        else:
            publish_segment(storage, plan, segment, tone(segment.ordinal), RATE)
    filler = RecordingFiller(storage)
    encoder = CapturingEncoder()
    exporter = exporter_for(tmp_path, storage, filler, encoder)

    exporter.start(plan.id, out / "read.m4a", "m4a")
    finished(exporter)

    assert filler.filled == []
    expected = RawFeed(KOKORO)
    pieces = [expected.add(tone(0), RATE, BOUNDARIES[0])]
    pieces.append(expected.gap(BOUNDARIES[1]))
    pieces.append(expected.add(tone(2), RATE, BOUNDARIES[2]))
    pieces.append(expected.flush())
    np.testing.assert_array_equal(encoder.pcm, np.concatenate(pieces))


def test_a_block_that_cannot_be_synthesized_fails_the_export(tmp_path, workspace):
    # Better a failure the user can retry than a file that silently drops
    # part of the read they asked to keep.
    storage, plan, out = workspace
    exporter = exporter_for(tmp_path, storage, RefusingFiller(), CapturingEncoder())

    exporter.start(plan.id, out / "read.m4a", "m4a")
    finished(exporter)

    snapshot = exporter.snapshot()
    assert snapshot["phase"] == "failed"
    assert snapshot["error"] is not None
    assert snapshot["error"]["code"] == "export_failed"
    assert not (out / "read.m4a").exists()


def test_a_failed_encode_leaves_nothing_at_the_destination(tmp_path, workspace):
    # The rename is the publication step: a crash or a refused encode must
    # leave a dot-file to delete, never a truncated recording under the name
    # the user chose and might send to someone.
    storage, plan, out = workspace
    publish_all(storage, plan)
    exporter = exporter_for(
        tmp_path, storage, RecordingFiller(storage), RefusingEncoder()
    )

    exporter.start(plan.id, out / "read.m4a", "m4a")
    finished(exporter)

    assert exporter.snapshot()["phase"] == "failed"
    assert not (out / "read.m4a").exists()
    assert list(out.iterdir()) == []


class PathQuotingEncoder:
    """An encoder that fails the way `os.replace` and `afconvert` do: with
    the destination path in the message."""

    def __call__(self, pcm, sample_rate, path):
        raise PermissionError(13, "Permission denied", str(path))


def test_a_failed_export_logs_no_path_the_reader_chose(tmp_path, workspace, caplog):
    # The destination came from the save panel, and a filename is often a
    # book's title; the log file a reader sends records the failure's kind.
    storage, plan, out = workspace
    publish_all(storage, plan)
    exporter = exporter_for(
        tmp_path, storage, RecordingFiller(storage), PathQuotingEncoder()
    )

    exporter.start(plan.id, out / "read.m4a", "m4a")
    finished(exporter)

    assert exporter.snapshot()["phase"] == "failed"
    assert "PermissionError" in caplog.text
    assert str(out) not in caplog.text


def test_progress_counts_every_block_and_ends_on_the_encode(tmp_path, workspace):
    storage, plan, out = workspace
    publish_all(storage, plan)
    exporter = exporter_for(
        tmp_path, storage, RecordingFiller(storage), CapturingEncoder()
    )

    exporter.start(plan.id, out / "read.m4a", "m4a")
    accepted = exporter.snapshot()
    finished(exporter)

    assert accepted["phase"] == "preparing"
    assert accepted["narrationId"] == plan.id
    assert accepted["format"] == "m4a"
    assert accepted["totalBlocks"] == 3
    final = exporter.snapshot()
    assert final["phase"] == "finished"
    assert final["completedBlocks"] == 3
    assert final["error"] is None


def test_a_second_export_is_refused_while_one_is_running(tmp_path, workspace):
    storage, plan, out = workspace
    publish_all(storage, plan)
    release = threading.Event()

    class BlockingEncoder(CapturingEncoder):
        def __call__(self, pcm, sample_rate, path):
            release.wait(timeout=2)
            super().__call__(pcm, sample_rate, path)

    exporter = exporter_for(
        tmp_path, storage, RecordingFiller(storage), BlockingEncoder()
    )
    exporter.start(plan.id, out / "first.m4a", "m4a")
    wait_until(lambda: exporter.snapshot()["phase"] == "encoding")
    try:
        with pytest.raises(ExportInProgress):
            exporter.start(plan.id, out / "second.m4a", "m4a")
    finally:
        release.set()
    finished(exporter)

    assert (out / "first.m4a").exists()
    assert not (out / "second.m4a").exists()


def test_an_unknown_narration_is_refused_before_anything_is_written(
    tmp_path, workspace
):
    storage, _plan, out = workspace
    exporter = exporter_for(
        tmp_path, storage, RecordingFiller(storage), CapturingEncoder()
    )

    with pytest.raises(KeyError):
        exporter.start("no-such-narration", out / "read.m4a", "m4a")

    assert exporter.snapshot()["phase"] == "idle"


def test_a_format_readily_does_not_export_is_refused(tmp_path, workspace):
    storage, plan, out = workspace
    exporter = exporter_for(
        tmp_path, storage, RecordingFiller(storage), CapturingEncoder()
    )

    with pytest.raises(ValueError):
        exporter.start(plan.id, out / "read.mp3", "mp3")


@pytest.mark.parametrize(
    "name",
    ["relative/read.m4a", "read.m4a"],
)
def test_a_relative_destination_is_refused(tmp_path, name):
    with pytest.raises(ExportDestinationError):
        check_destination(Path(name), tmp_path, "m4a")


def test_a_destination_whose_directory_does_not_exist_is_refused(tmp_path):
    with pytest.raises(ExportDestinationError):
        check_destination(tmp_path / "nowhere" / "read.m4a", tmp_path / "data", "m4a")


def test_a_destination_that_does_not_name_the_format_is_refused(tmp_path):
    # Nothing else ties the path to what will be written there. Without
    # this, a request could name any user-writable path at all and the
    # Engine would overwrite it with audio (threat model B4).
    data = tmp_path / "data"
    data.mkdir()

    with pytest.raises(ExportDestinationError):
        check_destination(tmp_path / ".zshrc", data, "m4a")
    with pytest.raises(ExportDestinationError):
        check_destination(tmp_path / "read.wav", data, "m4a")
    assert check_destination(tmp_path / "read.M4A", data, "m4a").name == "read.M4A"


def test_a_destination_inside_readilys_own_storage_is_refused(tmp_path):
    # Export writes *out* of Readily's storage. A destination inside the
    # Engine's data tree would let an HTTP body overwrite the History
    # database or a cached Segment (threat model B3).
    data = tmp_path / "data"
    (data / "segments" / "ab").mkdir(parents=True)

    with pytest.raises(ExportDestinationError):
        check_destination(data / "readily.wav", data, "wav")
    with pytest.raises(ExportDestinationError):
        check_destination(data / "segments" / "ab" / "read.m4a", data, "m4a")
    # ..-walking back into the tree resolves before the comparison.
    with pytest.raises(ExportDestinationError):
        check_destination(data / "segments" / ".." / "read.m4a", data, "m4a")


def test_a_destination_beside_the_data_directory_is_allowed(tmp_path):
    out = tmp_path / "data-elsewhere"
    out.mkdir()
    (tmp_path / "data").mkdir()

    checked = check_destination(out / "read.m4a", tmp_path / "data", "m4a")

    assert checked == out / "read.m4a"


@pytest.mark.skipif(sys.platform != "darwin", reason="afconvert is macOS-only")
def test_a_real_wav_export_is_bit_accurate_to_the_cached_audio(tmp_path, workspace):
    # WAV is the lossless option: what comes back has to be the assembled
    # PCM at 24-bit precision, not a re-encode of it.
    from readily_engine.narration.export import _ENCODERS

    storage, plan, out = workspace
    audio = publish_all(storage, plan)
    exporter = Exporter(
        storage, RecordingFiller(storage), tmp_path / "data", encoders=_ENCODERS
    )

    exporter.start(plan.id, out / "read.wav", "wav")
    finished(exporter)

    assert exporter.snapshot()["phase"] == "finished"
    with wave.open(str(out / "read.wav"), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 3
        assert handle.getframerate() == RATE
        raw = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.uint8)
    packed = raw.reshape(-1, 3).astype(np.int32)
    decoded = packed[:, 0] | (packed[:, 1] << 8) | (packed[:, 2] << 16)
    decoded = np.where(decoded >= 1 << 23, decoded - (1 << 24), decoded)
    np.testing.assert_allclose(
        decoded / 8_388_607, assembled(audio), atol=1 / 8_388_607
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="afconvert is macOS-only")
def test_a_real_m4a_export_is_an_aac_file_quicktime_will_open(tmp_path, workspace):
    from readily_engine.narration.export import _ENCODERS

    storage, plan, out = workspace
    publish_all(storage, plan)
    exporter = Exporter(
        storage, RecordingFiller(storage), tmp_path / "data", encoders=_ENCODERS
    )

    exporter.start(plan.id, out / "read.m4a", "m4a")
    finished(exporter)

    assert exporter.snapshot()["phase"] == "finished"
    header = (out / "read.m4a").read_bytes()
    # An ISO base media file: the `ftyp` box, branded M4A.
    assert header[4:8] == b"ftyp"
    assert b"M4A " in header[:32]


def test_a_stale_gap_row_never_silences_a_block_whose_audio_is_present(
    tmp_path, workspace
):
    # Segments are content-addressed and shared, so another Narration of the
    # same words can publish the audio a gap row here still claims was never
    # spoken. Playback reads the cache first and speaks it; an Export that
    # trusted the gap row would drop a whole Block from the file and still
    # report `finished`.
    storage, plan, out = workspace
    audio = publish_all(storage, plan)
    storage.record_gap(plan.id, plan.segments[1], "generation_failed")
    filler = RecordingFiller(storage)
    encoder = CapturingEncoder()
    exporter = exporter_for(tmp_path, storage, filler, encoder)

    exporter.start(plan.id, out / "read.m4a", "m4a")
    finished(exporter)

    assert exporter.snapshot()["phase"] == "finished"
    assert filler.filled == []
    np.testing.assert_array_equal(encoder.pcm, assembled(audio))


def test_a_narration_that_mixes_sample_rates_is_refused_rather_than_mispitched(
    tmp_path, workspace
):
    # Playback feeds each piece at its own rate; one file has only one. A
    # silently resampled Export would play every earlier Block at the wrong
    # pitch, which is worse than an Export the user can retry.
    storage, plan, out = workspace
    for segment in plan.segments:
        rate = RATE if segment.ordinal == 0 else 16_000
        publish_segment(storage, plan, segment, tone(segment.ordinal), rate)
    exporter = exporter_for(
        tmp_path, storage, RecordingFiller(storage), CapturingEncoder()
    )

    exporter.start(plan.id, out / "read.m4a", "m4a")
    finished(exporter)

    assert exporter.snapshot()["phase"] == "failed"
    assert not (out / "read.m4a").exists()


def test_a_narration_that_is_currently_playing_can_still_be_exported(
    tmp_path, workspace
):
    # `plan` exists precisely so Export reads a Narration in any status.
    # Refusing to export what the listener is hearing would be the most
    # obvious moment to press the button.
    storage, plan, out = workspace
    audio = publish_all(storage, plan)
    storage.set_status(plan.id, NarrationStatus.PLAYING, playhead_sec=1.0)
    encoder = CapturingEncoder()
    exporter = exporter_for(tmp_path, storage, RecordingFiller(storage), encoder)

    exporter.start(plan.id, out / "read.m4a", "m4a")
    finished(exporter)

    assert exporter.snapshot()["phase"] == "finished"
    # From the start of the Narration, not from the playhead: an Export is
    # the whole read, whatever the listener has reached.
    np.testing.assert_array_equal(encoder.pcm, assembled(audio))


def test_shutdown_refuses_new_exports_and_waits_out_the_one_in_flight(
    tmp_path, workspace
):
    storage, plan, out = workspace
    publish_all(storage, plan)
    release = threading.Event()

    class BlockingEncoder(CapturingEncoder):
        def __call__(self, pcm, sample_rate, path):
            release.wait(timeout=2)
            super().__call__(pcm, sample_rate, path)

    exporter = exporter_for(
        tmp_path, storage, RecordingFiller(storage), BlockingEncoder()
    )
    exporter.start(plan.id, out / "first.m4a", "m4a")
    wait_until(lambda: exporter.snapshot()["phase"] == "encoding")
    closing = threading.Thread(target=exporter.close)
    closing.start()
    try:
        # `RuntimeError`, not `ExportInProgress`: nothing is in progress
        # from the caller's point of view, the Engine is going away.
        with pytest.raises(RuntimeError):
            exporter.start(plan.id, out / "second.m4a", "m4a")
    finally:
        release.set()
    closing.join(timeout=2)

    assert not closing.is_alive()
    assert exporter.snapshot()["phase"] == "finished"
    assert (out / "first.m4a").exists()
    assert not (out / "second.m4a").exists()
