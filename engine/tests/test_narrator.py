"""Engine shutdown order: generation first, the audio device only after."""

from readily_engine.narration.narrator import EngineNarrator


class RecordingPlayback:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class Worker:
    def __init__(self, stopped: bool, order: list[str]) -> None:
        self._stopped = stopped
        self._order = order
        self.close_calls = 0

    def close(self) -> bool:
        self.close_calls += 1
        self._order.append("worker")
        return self._stopped


class RecordingExporter:
    def __init__(self, order: list[str]) -> None:
        self._order = order

    def close(self) -> None:
        self._order.append("exporter")


class Narrator(EngineNarrator):
    """The real narrator's shutdown over stand-in collaborators — building
    the production one would start a worker thread and open a device."""

    def __init__(
        self, worker: Worker, playback: RecordingPlayback, order: list[str]
    ) -> None:
        self._worker = worker
        self._playback = playback
        self._exporter = RecordingExporter(order)


def narrator_for(stopped: bool) -> tuple[Narrator, RecordingPlayback, list[str]]:
    order: list[str] = []
    playback = RecordingPlayback()
    return Narrator(Worker(stopped, order), playback, order), playback, order


def test_the_device_closes_once_generation_has_stopped():
    narrator, playback, _order = narrator_for(stopped=True)
    stuck: list[str] = []

    narrator.close(on_stuck=lambda: stuck.append("terminated"))

    assert playback.closed
    assert stuck == []


def test_generation_stops_before_export_so_a_blocked_export_is_released():
    narrator, _playback, order = narrator_for(stopped=True)

    narrator.close(on_stuck=lambda: None)

    assert order == ["worker", "exporter"]


def test_a_worker_stuck_in_a_native_call_ends_the_process_untorn_down():
    # Closing PortAudio, or returning into interpreter finalization, while
    # MLX or ONNX is still running on the generation thread is the segfault
    # the worker exists to avoid: leave both alone and end the process.
    narrator, playback, _order = narrator_for(stopped=False)
    stuck: list[str] = []

    narrator.close(on_stuck=lambda: stuck.append("terminated"))

    assert not playback.closed
    assert stuck == ["terminated"]
