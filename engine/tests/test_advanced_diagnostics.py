from readily_engine.narration.readiness import Readiness


def test_diagnostics_measure_generation_and_ready_listening_time():
    now = [0.0]
    ready = Readiness(lambda: now[0])
    assert ready.progress(0, 2)["audioSecondsPerSecond"] is None
    with ready.generating():
        now[0] = 4.0
    ready.add(start=0, pause=0, seconds=6, generated=True)
    ready.advance(6)
    report = ready.progress(2, 2)
    assert report["audioSecondsPerSecond"] == 1.5
    assert report["readySecondsAhead"] == 2.0


def test_worker_reports_live_preparation_and_playing_record(tmp_path):
    import threading

    import numpy as np
    from conftest import make_entry, wait_until
    from storage_fakes import open_test_storage
    from worker_fakes import RecordingPlayback, voice_model

    from readily_engine.generation import GeneratedAudio
    from readily_engine.narration.worker import GenerationWorker, NarrationRequest

    release = threading.Event()
    entered = threading.Event()

    class Synth:
        calls = 0

        def generate(self, record):
            self.calls += 1
            entered.set()
            release.wait(2)
            if self.calls == 1:
                raise RuntimeError("retry me")
            return GeneratedAudio(np.ones(2400, dtype=np.float32), 24000, cutoffs=1)

    class Playback(RecordingPlayback):
        ring_starvations = 3
        underflows = 2

        def drain(self, generation):
            while generation == self.generation and not finish.is_set():
                finish.wait(0.01)
            return 0

        def position(self, generation):
            return 0.05

    finish = threading.Event()
    storage = open_test_storage(tmp_path)
    entry = make_entry()
    worker = GenerationWorker(
        {entry.id: voice_model(Synth(), entry)},
        Playback(),
        storage,
        default_model=entry.id,
        default_voice=entry.default_voice,
    )
    try:
        narration = worker.start(
            NarrationRequest(entry.id, "Hello.", entry.default_voice)
        )
        assert entered.wait(1)
        assert worker.snapshot()["diagnostics"]["preparingBlock"] == 0
        assert worker.snapshot()["diagnostics"]["audioSecondsPerSecond"] is None
        release.set()
        wait_until(lambda: worker.snapshot()["phase"] == "playing")
        report = worker.snapshot()["diagnostics"]
        record = storage.plan(narration).segments[0].generation
        assert report["playingBlock"]["recordHash"] == record.key
        assert report["playingBlock"]["seed"] == record.rng_seed
        assert report["playingBlock"]["wordTiming"] == "estimated"
        assert report["playingBlock"]["cacheHit"] is False
        assert report["retries"] == 1
        assert report["cutoffs"] == 1
        assert report["ringStarvations"] == 3
        assert report["deviceUnderflows"] == 2
    finally:
        release.set()
        finish.set()
        worker.close()
        storage.close()


def test_export_fill_does_not_change_playback_diagnostics(tmp_path):
    import numpy as np
    from generation_fakes import entry_for
    from storage_fakes import open_test_storage
    from test_export import make_plan
    from worker_fakes import RecordingPlayback, voice_model

    from readily_engine.generation import GeneratedAudio
    from readily_engine.narration.worker import GenerationWorker

    class Synth:
        calls = 0

        def generate(self, record):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("retry Export")
            return GeneratedAudio(np.ones(2400, dtype=np.float32), 24000, cutoffs=1)

    storage = open_test_storage(tmp_path)
    plan = make_plan(storage)
    entry = entry_for(plan.settings)
    worker = GenerationWorker(
        {entry.id: voice_model(Synth(), entry)},
        RecordingPlayback(),
        storage,
        default_model=entry.id,
        default_voice=plan.settings.voice_id,
    )
    try:
        before = worker.snapshot()["diagnostics"]
        assert worker.fill(plan, plan.segments[0]) is not None
        assert worker.snapshot()["diagnostics"] == before
    finally:
        worker.close()
        storage.close()
