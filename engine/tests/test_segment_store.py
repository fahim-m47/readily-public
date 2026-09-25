"""Identical speech is one content-addressed Segment; a crash never half-writes it."""

import sys
from pathlib import Path

import numpy as np
import pytest
from storage_fakes import decode_npz, encode_npz

from readily_engine.audio.encoding import MIN_FLAC_FRAMES
from readily_engine.storage.segments import (
    SegmentStorageError,
    SegmentStore,
)
from readily_engine.timings import Timing

HASHED = "ab" + "0" * 62


def test_path_is_hash_sharded(tmp_path):
    store = SegmentStore(tmp_path, encoder=encode_npz, decoder=decode_npz)
    key = HASHED
    assert store.path_for(key) == tmp_path / "ab" / f"{key}.flac"


def test_path_for_rejects_non_sha256_keys(tmp_path):
    store = SegmentStore(tmp_path, encoder=encode_npz, decoder=decode_npz)
    with pytest.raises(ValueError, match="64 lowercase"):
        store.path_for("AB" + "0" * 62)
    with pytest.raises(ValueError, match="64 lowercase"):
        store.path_for("ab")


def test_equal_segments_publish_one_final_file(tmp_path):
    store = SegmentStore(tmp_path, encoder=encode_npz, decoder=decode_npz)
    key = HASHED
    first = store.write(key, np.array([0.0, 0.5], dtype=np.float32), 24_000)
    second = store.write(key, np.array([1.0], dtype=np.float32), 24_000)

    assert first.sample_rate == 24_000
    np.testing.assert_array_equal(second.pcm, first.pcm)
    assert list((tmp_path / "ab").glob("*.flac")) == [store.path_for(key)]


def _pad_like_afconvert(pcm, sample_rate, path):
    samples = np.asarray(pcm, dtype=np.float32)
    if len(samples) < MIN_FLAC_FRAMES:
        padded = np.zeros(MIN_FLAC_FRAMES, dtype=np.float32)
        padded[: len(samples)] = samples
        samples = padded
    encode_npz(samples, sample_rate, path)


def test_read_returns_the_original_length_when_the_encoder_padded(tmp_path):
    store = SegmentStore(tmp_path, encoder=_pad_like_afconvert, decoder=decode_npz)
    source = np.arange(100, dtype=np.float32) / 100
    store.write(HASHED, source, 24_000)

    stored = store.read(HASHED)
    assert stored is not None
    np.testing.assert_array_equal(stored.pcm, source)


def test_failed_encode_leaves_no_final_or_temporary_file(tmp_path):
    def fail(_pcm, _sample_rate, _path):
        raise RuntimeError("encode failed")

    store = SegmentStore(tmp_path, encoder=fail, decoder=decode_npz)
    key = HASHED
    with pytest.raises(SegmentStorageError):
        store.write(key, np.ones(10, dtype=np.float32), 24_000)
    assert not store.path_for(key).exists()
    assert list(tmp_path.rglob("*.tmp.*")) == []
    assert list(tmp_path.rglob(".*.tmp.*")) == []


def test_corrupt_flac_reads_as_a_cache_miss(tmp_path):
    def fail(_path):
        raise ValueError("decode failed")

    store = SegmentStore(tmp_path, encoder=encode_npz, decoder=fail)
    key = HASHED
    store.path_for(key).parent.mkdir(parents=True)
    store.path_for(key).write_bytes(b"not flac")
    assert store.read(key) is None


def test_missing_segment_reads_as_a_cache_miss(tmp_path):
    store = SegmentStore(tmp_path, encoder=encode_npz, decoder=decode_npz)
    assert store.read(HASHED) is None


def test_opening_the_store_sweeps_only_its_own_temporary_files(tmp_path):
    shard = tmp_path / "ab"
    shard.mkdir()
    stale_wav = shard / ".dead.tmp.wav"
    stale_flac = shard / ".dead.tmp.flac"
    keeper = shard / f"{HASHED}.flac"
    stale_wav.write_bytes(b"x")
    stale_flac.write_bytes(b"x")
    keeper.write_bytes(b"keep")

    SegmentStore(tmp_path, encoder=encode_npz, decoder=decode_npz)

    assert not stale_wav.exists()
    assert not stale_flac.exists()
    assert keeper.read_bytes() == b"keep"


@pytest.mark.skipif(sys.platform != "darwin", reason="afconvert is macOS-only")
def test_real_flac_round_trip(tmp_path):
    store = SegmentStore(tmp_path)
    source = np.sin(2 * np.pi * 440 * np.arange(2_400) / 24_000).astype(np.float32)
    key = HASHED
    decoded = store.write(key, source, 24_000)

    assert store.path_for(key).read_bytes().startswith(b"fLaC")
    assert decoded.sample_rate == 24_000
    assert len(decoded.pcm) == len(source)
    np.testing.assert_allclose(decoded.pcm, source, atol=2 / 2**23)
    reread = store.read(key)
    assert reread is not None
    assert len(reread.pcm) == len(source)
    np.testing.assert_allclose(reread.pcm, source, atol=2 / 2**23)


def test_usage_counts_a_file_that_vanishes_mid_measurement_as_gone(
    tmp_path, monkeypatch
):
    """Measuring the store is not serialized against evicting from it.

    `NarrationStorage.retention` walks the tree outside its lock, so that
    rendering Settings cannot block synthesis. A sweep or a delete can
    therefore unlink a file after the measurement has already seen it, and
    the user must not get an error out of that.

    The eviction is staged on the first `stat` of the FLAC so it lands in
    exactly that window rather than depending on thread timing.
    """
    store = SegmentStore(tmp_path, encoder=encode_npz, decoder=decode_npz)
    store.write(HASHED, np.ones(240, dtype=np.float32), 24_000)
    flac = store.path_for(HASHED)
    frames = flac.with_name(f"{HASHED}.frames")
    real_stat = Path.stat
    expected = real_stat(flac).st_size + real_stat(frames).st_size
    seen = 0

    def evict_after_the_first_look(self, *args, **kwargs):
        nonlocal seen
        result = real_stat(self, *args, **kwargs)
        if self == flac:
            seen += 1
            if seen == 1:
                flac.unlink()
        return result

    monkeypatch.setattr(Path, "stat", evict_after_the_first_look)

    assert store.disk_usage_for(HASHED) == expected


def test_cached_timings_belong_to_the_first_accepted_waveform(tmp_path):
    store = SegmentStore(tmp_path, encoder=encode_npz, decoder=decode_npz)
    timings = (
        Timing(
            start_char=4, end_char=8, start_sec=0.4, end_sec=0.8, provenance="spoken"
        ),
    )
    store.write(HASHED, np.ones(1000, dtype=np.float32), 1000, timings=timings)
    reopened = SegmentStore(tmp_path, encoder=encode_npz, decoder=decode_npz)
    second = reopened.write(HASHED, np.zeros(2000, dtype=np.float32), 1000)
    assert second.timings == timings
    assert len(second.pcm) == 1000
    assert reopened.read(HASHED).timings == timings


def test_replacing_waveform_without_its_metadata_is_a_cache_miss(tmp_path):
    store = SegmentStore(tmp_path, encoder=encode_npz, decoder=decode_npz)
    store.write(HASHED, np.ones(1000, dtype=np.float32), 1000)
    encode_npz(np.zeros(1000, dtype=np.float32), 1000, store.path_for(HASHED))
    assert store.read(HASHED) is None
    assert store.has(HASHED)
    assert not store.has_verified(HASHED)


@pytest.mark.parametrize("sidecar", ["1000\n", '{"frame_count":1000,"timings":[]}'])
def test_digestless_sidecars_are_replaced_on_the_next_write(tmp_path, sidecar):
    store = SegmentStore(tmp_path, encoder=encode_npz, decoder=decode_npz)
    store.write(HASHED, np.ones(1000, dtype=np.float32), 1000)
    store.path_for(HASHED).with_suffix(".frames").write_text(sidecar)
    assert store.has(HASHED)
    assert not store.has_verified(HASHED)
    assert store.read(HASHED) is None

    store.write(HASHED, np.zeros(500, dtype=np.float32), 1000)

    assert store.has_verified(HASHED)
    np.testing.assert_array_equal(store.read(HASHED).pcm, np.zeros(500))


def test_a_verified_segment_is_digested_once_until_its_file_changes(
    tmp_path, monkeypatch
):
    import readily_engine.storage.segments as segments

    digests = 0
    digest = segments._digest

    def counting_digest(path):
        nonlocal digests
        digests += 1
        return digest(path)

    monkeypatch.setattr(segments, "_digest", counting_digest)
    store = SegmentStore(tmp_path, encoder=encode_npz, decoder=decode_npz)
    store.write(HASHED, np.ones(1000, dtype=np.float32), 1000)
    # `write` reads back what it published, so the digest is already known.
    digests = 0

    assert store.has_verified(HASHED)
    assert store.read(HASHED) is not None
    assert store.has_verified(HASHED)
    assert digests == 0

    encode_npz(np.zeros(1000, dtype=np.float32), 1000, store.path_for(HASHED))
    assert not store.has_verified(HASHED)
    assert digests == 1


def test_integrity_check_does_not_decode_audio(tmp_path):
    store = SegmentStore(tmp_path, encoder=encode_npz, decoder=decode_npz)
    store.write(HASHED, np.ones(1000, dtype=np.float32), 1000)

    def unexpected_decode(_path):
        raise AssertionError("readiness must not decode audio")

    reopened = SegmentStore(tmp_path, encoder=encode_npz, decoder=unexpected_decode)
    assert reopened.has_verified(HASHED)


def test_read_cannot_pair_old_decoded_audio_with_new_timings(tmp_path):
    replace_during_decode = False
    replacement = "cd" + "0" * 62

    def decode_and_replace(path):
        stored = decode_npz(path)
        if replace_during_decode and path == store.path_for(HASHED):
            path.write_bytes(store.path_for(replacement).read_bytes())
            path.with_suffix(".frames").write_bytes(
                store.path_for(replacement).with_suffix(".frames").read_bytes()
            )
        return stored

    store = SegmentStore(tmp_path, encoder=encode_npz, decoder=decode_and_replace)
    store.write(HASHED, np.ones(1000, dtype=np.float32), 1000)
    store.write(
        replacement,
        np.zeros(1000, dtype=np.float32),
        1000,
        timings=(
            Timing(
                start_char=0,
                end_char=3,
                start_sec=0.1,
                end_sec=0.5,
                provenance="spoken",
            ),
        ),
    )
    replace_during_decode = True
    stored = store.read(HASHED)
    assert stored is not None
    assert stored.timings == ()
    assert np.all(stored.pcm == 1)


def test_a_segment_stored_without_words_gains_them_on_the_next_write(tmp_path):
    store = SegmentStore(tmp_path, encoder=encode_npz, decoder=decode_npz)
    store.write(HASHED, np.ones(1000, dtype=np.float32), 1000)
    timings = (
        Timing(
            start_char=0, end_char=3, start_sec=0.1, end_sec=0.5, provenance="matched"
        ),
    )
    adopted = store.write(
        HASHED, np.zeros(1000, dtype=np.float32), 1000, timings=timings
    )
    assert adopted.timings == timings
    assert np.all(adopted.pcm == 1)
    assert store.read(HASHED).timings == timings
    assert store.timings(HASHED, 1000) == timings
    assert not [path for path in tmp_path.rglob(".*.tmp.*")]


def test_timings_outlasting_the_stored_frames_are_dropped(tmp_path):
    store = SegmentStore(tmp_path, encoder=encode_npz, decoder=decode_npz)
    kept = Timing(
        start_char=0, end_char=3, start_sec=0.1, end_sec=0.5, provenance="spoken"
    )
    late = Timing(
        start_char=4, end_char=8, start_sec=0.6, end_sec=1.5, provenance="spoken"
    )
    store.write(HASHED, np.ones(1000, dtype=np.float32), 1000, timings=(kept, late))
    assert store.timings(HASHED, 1000) == (kept,)
