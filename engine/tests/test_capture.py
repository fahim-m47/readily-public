"""Capture: narrating the passage through a Backend and dressing the take
for audition. Runs offline on synthetic PCM, like `test_curation`.
"""

import json
from pathlib import Path

import numpy as np
import pytest
from conftest import FILES, make_entry
from curation_fakes import SAMPLE_RATE, ClippedSynthesizer, FakeSynthesizer

from readily_engine.curation import (
    CAPTURE_PASSAGE,
    CurationError,
    capture_voice,
    dress_for_audition,
    preview_relpath,
)
from readily_engine.curation.capture import (
    FADE_IN_SECONDS,
    FADE_OUT_SECONDS,
    HEAD_SILENCE_SECONDS,
    TAIL_SILENCE_SECONDS,
)
from readily_engine.generation import GeneratedAudio


class TestCapture:
    def test_a_capture_is_a_run_the_analyzer_can_read(self, tmp_path: Path) -> None:
        from readily_engine.audio.qualification import qualify_run

        entry = make_entry(files=FILES)
        run_dir = tmp_path / "run"
        capture_voice(entry, "narrator", FakeSynthesizer(), run_dir)

        report = qualify_run(run_dir, frozenset("ABDF"))
        assert report.issues == ()
        assert report.voice_model == "acme:1m"

    def test_a_capture_narrates_the_standard_passage_through_the_backend(
        self, tmp_path: Path
    ) -> None:
        synthesizer = FakeSynthesizer()
        capture_voice(
            make_entry(files=FILES), "narrator", synthesizer, tmp_path / "run"
        )

        spoken = "".join(text for text, _voice in synthesizer.spoken)
        assert "".join(spoken.split()) == "".join(CAPTURE_PASSAGE.split())
        assert {voice for _text, voice in synthesizer.spoken} == {"narrator"}

    def test_the_captured_segments_reassemble_into_the_returned_stream(
        self, tmp_path: Path
    ) -> None:
        from readily_engine.audio.artifacts import read_wav

        run_dir = tmp_path / "run"
        stream, rate = capture_voice(
            make_entry(files=FILES), "narrator", FakeSynthesizer(), run_dir
        )

        manifest = json.loads((run_dir / "manifest.json").read_text())
        pieces = [
            read_wav(run_dir / f"seg{segment['i']:02d}-processed.wav")[1]
            for segment in manifest["segments"]
        ]
        assert rate == SAMPLE_RATE
        assert np.array_equal(np.concatenate(pieces), stream)

    def test_a_block_too_short_to_blend_is_captured_as_speech_not_pause(
        self, tmp_path: Path
    ) -> None:
        """A Segment's `pause_len` has to be silence, whatever the Backend did.

        When a MID_SENTENCE Block is followed by one shorter than the 15ms
        crossfade window, the `Assembler` cannot blend: it plays the held
        tail out plain, so the next piece opens with the *previous* Block's
        speech rather than an authored pause. Counting that as pause ends
        the Block's played region short of its own trim, where check B reads
        a mid-word sample as the Segment's edge — a spurious refusal of a
        good model — and check A's statistics lose the tail.
        """
        from readily_engine.audio.artifacts import read_wav

        entry = make_entry(
            files=FILES,
            tunables={
                "chunk_budget_chars": 40,
                "first_block_chars": 20,
                "pause_sentence_ms": 200,
                "pause_paragraph_break_ms": 500,
            },
        )
        passage = "Readily reads what you paste, out loud, on your own Mac, quietly."
        run_dir = tmp_path / "run"
        synthesizer = ClippedSynthesizer(chars=12)

        stream, _rate = capture_voice(
            entry, "narrator", synthesizer, run_dir, passage=passage
        )

        assert [
            text for text, _voice in synthesizer.spoken if len(text.strip()) < 12
        ], "the Backend never took the sub-crossfade path this test is about"
        segments = json.loads((run_dir / "manifest.json").read_text())["segments"]
        pieces = []
        for segment in segments:
            _rate, processed = read_wav(
                run_dir / f"seg{segment['i']:02d}-processed.wav"
            )
            pieces.append(processed)
            pause = processed[segment["audio_len"] :]
            assert len(pause) == segment["pause_len"]
            assert not np.any(pause), f"Segment {segment['i']} counts speech as pause"
        # Moving the cut may not move a sample: the Segments are still the
        # stream, split somewhere else.
        assert np.array_equal(np.concatenate(pieces), stream)

    def test_a_capture_refuses_a_backend_that_emits_non_finite_samples(
        self, tmp_path: Path
    ) -> None:
        """A NaN turns every qualification comparison it reaches into False —
        the passing side of every check — so an interior NaN would qualify
        with a clean verdict and ship an unplayable clip."""

        class NanSynthesizer(FakeSynthesizer):
            def generate(self, record):
                result = super().generate(record)
                pcm, rate = result.pcm, result.sample_rate
                poisoned = pcm.copy()
                poisoned[len(poisoned) // 2] = np.nan
                return GeneratedAudio(poisoned, rate)

        with pytest.raises(CurationError, match="non-finite"):
            capture_voice(
                make_entry(files=FILES), "narrator", NanSynthesizer(), tmp_path / "run"
            )

    def test_an_audition_clip_carries_its_own_silence_and_fades(self) -> None:
        """A Voice Preview is played from silence by a button press, so the
        clip is dressed with head and tail room and its own fades. Only the
        clip: the capture the analyzer reads keeps the Assembler's 3ms edges,
        which are what ADR 0002 and check B measure."""
        stream = np.full(SAMPLE_RATE, 0.5, dtype=np.float32)

        dressed = dress_for_audition(stream, SAMPLE_RATE)

        head = round(SAMPLE_RATE * HEAD_SILENCE_SECONDS)
        tail = round(SAMPLE_RATE * TAIL_SILENCE_SECONDS)
        fade_in = round(SAMPLE_RATE * FADE_IN_SECONDS)
        fade_out = round(SAMPLE_RATE * FADE_OUT_SECONDS)
        assert len(dressed) == head + len(stream) + tail
        assert not np.any(dressed[:head]), "the clip does not open in silence"
        assert not np.any(dressed[-tail:]), "the clip does not close in silence"
        # The caller keeps the capture it passed in: the analyzer reads the
        # same array afterwards, and dressing it in place would move what
        # check B measures.
        assert np.all(stream == 0.5), "dressing wrote back over the capture"

        body = dressed[head:-tail]
        assert np.array_equal(body[fade_in:-fade_out], stream[fade_in:-fade_out])
        rise, fall = body[:fade_in], body[-fade_out:]
        assert rise[0] == 0.0 and fall[-1] == 0.0
        assert np.all(np.diff(rise) > 0), "the fade in is not monotonic"
        assert np.all(np.diff(fall) < 0), "the fade out is not monotonic"
        # Raised cosine, not a line. A line is monotonic too, and leaves a
        # corner where it meets silence and where it meets full gain; the
        # cosine flattens at both. A quarter of the way in it has covered an
        # eighth of the distance rather than a quarter, and its curvature
        # changes sign at the halfway point.
        assert rise[fade_in // 4] / 0.5 == pytest.approx(0.1464, abs=1e-3)
        shoulder = np.diff(rise, 2)
        assert shoulder[0] > 0 > shoulder[-1], "the fade in has no shoulder"

    def test_a_short_capture_is_dressed_with_the_fades_it_has_room_for(self) -> None:
        """The fades are shorter than any real capture, but a Backend that
        returns almost nothing must still produce a playable clip rather than
        an exception or a body faded past its own middle."""
        stream = np.full(300, 0.5, dtype=np.float32)

        dressed = dress_for_audition(stream, SAMPLE_RATE)

        head = round(SAMPLE_RATE * HEAD_SILENCE_SECONDS)
        tail = round(SAMPLE_RATE * TAIL_SILENCE_SECONDS)
        body = dressed[head:-tail]
        assert len(body) == len(stream)
        assert body[0] == 0.0 and body[-1] == 0.0
        assert body.max() < 0.5, "a body this short was not faded at all"
        assert np.all(body >= 0.0), "the fades overran each other"

    def test_a_preview_path_names_the_voice_under_its_entry(self) -> None:
        assert preview_relpath(make_entry(files=FILES), "narrator") == (
            "acme/1m/narrator.m4a"
        )


def test_the_capture_passage_exercises_both_pause_classes() -> None:
    from readily_engine.chunking import Boundary, chunk

    blocks = chunk(CAPTURE_PASSAGE, make_entry(files=FILES).tunables).blocks

    assert len(blocks) >= 3
    assert {block.boundary for block in blocks} >= {
        Boundary.SENTENCE,
        Boundary.PARAGRAPH,
    }
