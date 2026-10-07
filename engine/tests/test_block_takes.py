import threading

from conftest import make_entry, wait_until
from generation_fakes import entry_for
from storage_fakes import (
    SETTINGS,
    create_plan,
    open_test_storage,
    strip_generation_records,
)
from worker_fakes import (
    RecordingPlayback,
    RecordingStorage,
    RecordingSynthesizer,
    voice_model,
)

from readily_engine.chunking import Block, Boundary, ChunkedSource
from readily_engine.narration.worker import GenerationWorker, NarrationRequest


def worker_over(storage, entry, *, installed_as=None):
    return GenerationWorker(
        {installed_as or entry.id: voice_model(RecordingSynthesizer(), entry)},
        RecordingPlayback(),
        storage,
        default_model=entry.id,
        default_voice=entry.default_voice,
    )


def test_reroll_preserves_original_and_ab_replays_without_synthesis(tmp_path):
    storage = open_test_storage(tmp_path)
    synth = RecordingSynthesizer()
    entry = make_entry()
    worker = GenerationWorker(
        {entry.id: voice_model(synth, entry)},
        RecordingPlayback(),
        storage,
        default_model=entry.id,
        default_voice=entry.default_voice,
    )
    try:
        narration = worker.start(
            NarrationRequest(entry.id, "One Block.", entry.default_voice)
        )
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        original = storage.plan(narration).segments[0].generation
        assert worker.select_take(narration, 0, "reroll")
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        reroll = storage.plan(narration).segments[0].generation
        assert original.key != reroll.key
        assert original.text == reroll.text
        assert len(synth.inputs) == 2
        storage.run_retention()
        assert worker.select_take(narration, 0, "A")
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert storage.plan(narration).segments[0].generation == original
        assert worker.select_take(narration, 0, "B")
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert storage.plan(narration).segments[0].generation == reroll
        assert len(synth.inputs) == 2
        assert not worker.select_take(narration, 999, "reroll")
    finally:
        worker.close()
        storage.close()
    storage = open_test_storage(tmp_path)
    try:
        assert storage.block_takes(narration, 0) == {"A": original, "B": reroll}
        assert storage.narration_takes(narration) == {0: {"A": original, "B": reroll}}
    finally:
        storage.close()


def test_narrating_reads_the_take_map_once_per_narration(tmp_path):
    class CountingStorage(RecordingStorage):
        def __init__(self, wrapped):
            super().__init__(wrapped)
            self.take_reads = []

        def block_takes(self, narration_id, ordinal):
            self.take_reads.append("block")
            return self.wrapped.block_takes(narration_id, ordinal)

        def narration_takes(self, narration_id):
            self.take_reads.append("narration")
            return self.wrapped.narration_takes(narration_id)

    storage = CountingStorage(open_test_storage(tmp_path))
    entry = make_entry()
    worker = worker_over(storage, entry)
    try:
        worker.start(
            NarrationRequest(
                entry.id, "First Block.\n\nSecond Block.", entry.default_voice
            )
        )
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert storage.take_reads == ["narration"]
    finally:
        worker.close()
        storage.close()


def test_select_take_rejects_a_narration_the_engine_cannot_resume(tmp_path):
    storage = open_test_storage(tmp_path)
    worker = worker_over(storage, make_entry())
    try:
        assert not worker.select_take("missing", 0, "reroll")
        source = "First. Second."
        plan = storage.create(
            ChunkedSource(
                source=source,
                blocks=(
                    Block("First.", 0, 6, Boundary.SENTENCE),
                    Block("Second.", 7, 14, Boundary.PARAGRAPH),
                ),
            ),
            SETTINGS,
            entry_for(SETTINGS),
        )
        assert not worker.select_take(plan.id, 0, "B")
        assert not worker.select_take(plan.id, 1, "reroll")
        strip_generation_records(tmp_path / "readily.db")
        assert not worker.select_take(plan.id, 0, "reroll")
    finally:
        worker.close()
        storage.close()


def test_select_take_rejects_a_model_the_engine_has_not_installed(tmp_path):
    storage = open_test_storage(tmp_path)
    worker = worker_over(storage, make_entry(), installed_as="acme:elsewhere")
    try:
        plan = create_plan(storage, "First.")
        assert not worker.select_take(plan.id, 0, "reroll")
    finally:
        worker.close()
        storage.close()


def test_select_take_measures_the_take_before_holding_the_engine(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class ParkedStorage(RecordingStorage):
        armed = False

        def raw_audio(self, segment):
            if self.armed:
                entered.set()
                release.wait(2)
            return self.wrapped.raw_audio(segment)

    storage = ParkedStorage(open_test_storage(tmp_path))
    entry = make_entry()
    worker = worker_over(storage, entry)
    try:
        narration = worker.start(
            NarrationRequest(entry.id, "One Block.", entry.default_voice)
        )
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        assert worker.select_take(narration, 0, "reroll")
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
        storage.armed = True
        accepted = []
        selecting = threading.Thread(
            target=lambda: accepted.append(worker.select_take(narration, 0, "A"))
        )
        selecting.start()
        assert entered.wait(1)
        speeding = threading.Thread(target=lambda: worker.set_speed(1.5))
        speeding.start()
        speeding.join(1)
        engine_held = speeding.is_alive()
        release.set()
        selecting.join(2)
        assert not engine_held
        assert accepted == [True]
        wait_until(lambda: worker.snapshot()["phase"] == "finished")
    finally:
        release.set()
        worker.close()
        storage.close()


def test_take_route_rejects_untrusted_actions_before_calling_narrator():
    from conftest import AUTH, TOKEN
    from fastapi.testclient import TestClient
    from test_server_wire import InstalledStore, RecordingHistory

    from readily_engine.server.app import create_app

    class Narrator:
        def __init__(self):
            self.calls = []

        def select_take(self, narration_id, ordinal, action):
            self.calls.append((narration_id, ordinal, action))
            return ordinal == 0

    narrator = Narrator()
    history = RecordingHistory()
    client = TestClient(
        create_app(
            token=TOKEN, narrator=narrator, history=history, store=InstalledStore()
        )
    )
    narration_id = history.item.id
    path = f"/v1/history/{narration_id}/take"
    assert client.post(path, json={"ordinal": 0, "action": "reroll"}).status_code == 401
    for body in (
        {"ordinal": -1, "action": "A"},
        {"ordinal": 0, "action": "hash"},
        {"ordinal": 0, "action": "A", "seed": 1},
    ):
        assert client.post(path, headers=AUTH, json=body).status_code == 422
    assert narrator.calls == []
    assert (
        client.post(
            path, headers=AUTH, json={"ordinal": 0, "action": "reroll"}
        ).status_code
        == 202
    )
    assert narrator.calls == [(narration_id, 0, "reroll")]
    assert (
        client.post(path, headers=AUTH, json={"ordinal": 1, "action": "A"}).status_code
        == 422
    )


def test_take_route_refuses_a_model_this_machine_cannot_run():
    from conftest import AUTH, TOKEN
    from fastapi.testclient import TestClient
    from test_server_wire import InstalledStore, expressive_history

    from readily_engine.server.app import create_app

    class Narrator:
        def __init__(self):
            self.calls = []

        def select_take(self, narration_id, ordinal, action):
            self.calls.append((narration_id, ordinal, action))
            return True

    narrator = Narrator()
    history = expressive_history()
    client = TestClient(
        create_app(
            token=TOKEN,
            narrator=narrator,
            history=history,
            store=InstalledStore(),
            backends=frozenset({"onnxruntime"}),
        )
    )
    refused = client.post(
        f"/v1/history/{history.item.id}/take",
        headers=AUTH,
        json={"ordinal": 0, "action": "reroll"},
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "model_unsupported"
    assert narrator.calls == []


def test_frozen_export_plan_cannot_read_or_overwrite_another_takes_range(tmp_path):
    from dataclasses import replace

    import numpy as np

    from readily_engine.chunking import chunk
    from readily_engine.narration.measure import measure, trimmed_audio
    from readily_engine.storage.history import SynthesisSettings

    entry = make_entry()
    storage = open_test_storage(tmp_path)
    try:
        plan_a = storage.create(
            chunk("Hello.", entry.tunables),
            SynthesisSettings(
                entry.id,
                entry.version,
                entry.default_voice,
                1,
                entry.tunables.pause_policy,
            ),
            entry,
        )
        a = plan_a.segments[0]
        raw_a = storage.store_audio(a.key, np.ones(2400, dtype=np.float32), 24000)
        measure(storage, plan_a, a, raw_a)
        plan_b = storage.select_take(plan_a.id, 0, replace(a.generation, seed=123))
        b = plan_b.segments[0]
        raw_b = storage.store_audio(b.key, np.ones(4800, dtype=np.float32), 24000)
        assert len(trimmed_audio(storage, plan_a, a).pcm) == 2400
        assert storage.plan(plan_a.id).segments[0].frame_count is None
        measure(storage, plan_b, b, raw_b)
        assert len(trimmed_audio(storage, plan_a, a).pcm) == 2400
        assert storage.plan(plan_a.id).segments[0].frame_count == 4800
    finally:
        storage.close()
