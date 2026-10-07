"""Fakes for the Architectures and the two Backends they run on, so every
Architecture is exercised on ubuntu with no weights, no download and no MLX
import; the real MLX path belongs to the macOS smoke lane.

`FakeOnnxRuntime` and `FakeMlx` stand in for the upstream modules themselves
(installed into `sys.modules`), so an Architecture's own `load` runs
unchanged; `FakeModel` and `synthesizer` drive the MLX lane directly."""

import sys
import wave
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import onnxruntime
from generation_fakes import record

from readily_engine.audio.encoding import write_float_wav
from readily_engine.catalog import PinnedArtifact
from readily_engine.loading import qwen3
from readily_engine.loading.mlx_lane import (
    STREAMING_INTERVAL_SECONDS,
    Generate,
    MlxSynthesizer,
)

SAMPLE_RATE = 24_000


def tone(frequency: float, seconds: float, amplitude: float = 0.3) -> np.ndarray:
    t = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
    return (amplitude * np.sin(2 * np.pi * frequency * t)).astype(np.float32)


def clean_chunk(seconds: float = 2.0) -> np.ndarray:
    return tone(400, seconds)


def degenerate_chunk(seconds: float = 2.0) -> np.ndarray:
    rng = np.random.default_rng(16)
    return (0.3 * rng.standard_normal(int(SAMPLE_RATE * seconds))).astype(np.float32)


def crackle(seconds: float = 0.05) -> np.ndarray:
    # The ear-calibrated crackle signature: a tonal 6-12kHz event with no
    # voiced body, isolated inside a pause.
    return tone(9_000, seconds, amplitude=0.1)


class FakeModel:
    """Replays scripted generations chunk by chunk, recording every call, and
    stops at `max_tokens` decoded at `tokens_per_second`; without a rate it
    plays every chunk, like an upstream with no ceiling."""

    def __init__(
        self,
        generations: list[list[np.ndarray]],
        *,
        has_encoder: bool = True,
        sample_rate: int = SAMPLE_RATE,
        tokens_per_second: float | None = None,
    ) -> None:
        self.generations = generations
        self.sample_rate = sample_rate
        self.tokens_per_second = tokens_per_second
        self.calls: list[dict[str, object]] = []
        self.options: list[tuple[bool, int]] = []
        self.speech_tokenizer = SimpleNamespace(has_encoder=has_encoder)
        self.tokenizer = None

    # What `load_without_hook` asks of the model it builds.
    def sanitize(self, weights):
        return weights

    def load_weights(self, weights, *, strict):
        assert strict

    def eval(self):
        pass

    def generate(
        self,
        text,
        *,
        voice=None,
        ref_audio=None,
        ref_text=None,
        speed=1.0,
        stream=False,
        streaming_interval=2.0,
        verbose=False,
        max_tokens=4096,
        **parameters,
    ):
        self.options.append((stream, max_tokens))
        call = {"text": text, "voice": voice, "speed": speed}
        if ref_audio is not None or ref_text is not None:
            call |= {"ref_audio": ref_audio, "ref_text": ref_text}
        results = self._replay(call)
        remaining = None
        if self.tokens_per_second is not None:
            remaining = int(max_tokens * self.sample_rate / self.tokens_per_second)
        pieces = []
        for result in results:
            pcm = result.audio[:remaining]
            if remaining is not None:
                remaining -= len(pcm)
            if stream:
                yield SimpleNamespace(audio=pcm, sample_rate=self.sample_rate)
            else:
                pieces.append(pcm)
            if remaining == 0:
                break
        if not stream and pieces:
            yield SimpleNamespace(
                audio=np.concatenate(pieces), sample_rate=self.sample_rate
            )

    def _replay(self, call: dict[str, object]):
        self.calls.append(call)
        chunks = self.generations[min(len(self.calls), len(self.generations)) - 1]
        for chunk in chunks:
            yield SimpleNamespace(audio=chunk, sample_rate=self.sample_rate)


def synthesizer(
    model_dir,
    model,
    generate=qwen3.generate,
    runaway_seconds=qwen3.runaway_seconds,
    **options,
) -> MlxSynthesizer:
    """A lane over `model`, on the qwen3 Architecture's call unless told otherwise."""
    return MlxSynthesizer(
        model_dir,
        seed_rng=lambda _seed: None,
        **options,
        generate=generate,
        runaway_seconds=runaway_seconds,
        model_factory=lambda _path: model,
    )


def mlx_model_dir(tmp_path):
    """A promoted directory with the one file the lane checks before loading;
    a test module registers it as a fixture (`pytest.fixture(mlx_model_dir)`)."""
    (tmp_path / "config.json").write_text("{}")
    return tmp_path


def write_wav(path: Path, pcm: np.ndarray, *, rate=SAMPLE_RATE, channels=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    if channels == 1:
        write_float_wav(path, pcm, rate)
        return
    # `write_float_wav` is mono by design; a stereo clip is assembled by hand.
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        frames = np.repeat((np.clip(pcm, -1, 1) * 32767).astype(np.int16), channels)
        out.writeframes(frames.tobytes())


def assert_parameters_reach_the_upstream_call(
    generate: Generate, parameters: Mapping[str, float | int]
) -> None:
    """Every declared knob in `parameters` an Architecture's `generate` receives
    lands on the upstream call as a keyword, with the record's decode mode and
    the lane's streaming interval; `speed` never does (mlx-audio has no such
    knob)."""
    calls = []

    class Model:
        def generate(self, text, **kwargs):
            calls.append((text, kwargs))
            return iter(())

    for decode_mode in ("streaming", "non-streaming"):
        generation = record(parameters=parameters, decode_mode=decode_mode)
        list(generate(Model(), generation))
        text, kwargs = calls[-1]
        assert text == generation.text
        assert kwargs.items() >= parameters.items()
        assert kwargs["stream"] is (decode_mode == "streaming")
        assert kwargs["streaming_interval"] == STREAMING_INTERVAL_SECONDS
        assert "speed" not in kwargs


type Feed = dict[str, np.ndarray]


class FakeOnnxSession:
    """One graph's `InferenceSession`: records every feed and answers it
    with `answer(feed)`."""

    def __init__(self, answer: Callable[[Feed], list[np.ndarray]]) -> None:
        self.answer = answer
        self.calls: list[Feed] = []

    @property
    def inputs(self) -> Feed:
        """The last feed, for a graph run once per draw."""
        return self.calls[-1]

    def run(self, outputs, inputs: Feed) -> list[np.ndarray]:
        assert outputs is None
        self.calls.append(inputs)
        return self.answer(inputs)


class FakeOnnxRuntime:
    """The `onnxruntime` module, installed with `install(monkeypatch)`, for
    graphs under `promoted`: one named in `graphs` (by file stem) answers
    with that function, and any other with the audio `speak` last set, so a
    one-graph export and a vocoder are driven the same way. A graph outside
    `promoted` — the bundled pronunciation fallback — runs for real."""

    def __init__(
        self,
        promoted: Path,
        graphs: Mapping[str, Callable[[Feed], list[np.ndarray]]] = {},
    ) -> None:
        self.promoted = promoted
        self.graphs = graphs
        self.sessions: dict[str, FakeOnnxSession] = {}
        self.paths: list[Path] = []
        self.speak([tone(400, 0.5)])

    def speak(self, chunks: Sequence[np.ndarray]) -> None:
        self._pcm = np.concatenate(chunks)

    def install(self, monkeypatch) -> None:
        monkeypatch.setitem(sys.modules, "onnxruntime", self)

    def disable_telemetry_events(self) -> None:
        pass

    def InferenceSession(self, path: str, providers: list[str]):
        if not Path(path).is_relative_to(self.promoted):
            return onnxruntime.InferenceSession(path, providers=providers)
        self.paths.append(Path(path))
        stem = Path(path).stem
        session = FakeOnnxSession(
            self.graphs.get(stem, lambda _feed: [self._pcm.reshape(1, -1)])
        )
        self.sessions[stem] = session
        return session


class FakeMlx:
    """The `mlx`, `mlx_audio` and `transformers` modules, installed with
    `install(monkeypatch)`: `load`, and the steps `load_without_hook` takes
    in its place, hand back one `FakeModel`, which replays what `speak` last
    set; `mlx.core.array` is numpy's, and `AutoTokenizer` records the
    directory it is loaded from."""

    def __init__(self, tokens_per_second: float | None = None) -> None:
        self.model = FakeModel([[clean_chunk()]], tokens_per_second=tokens_per_second)
        self.loaded: list[Path] = []
        self.tokenizers: list[Path] = []

    def speak(self, chunks: Sequence[np.ndarray]) -> None:
        self.model.generations = [list(chunks)]

    def install(self, monkeypatch) -> None:
        core = SimpleNamespace(
            array=np.asarray, random=SimpleNamespace(seed=lambda _seed: None)
        )
        utils = SimpleNamespace(load=self._load, MODEL_REMAPPING={})
        tts = SimpleNamespace(utils=utils)
        model_class = SimpleNamespace(
            Model=self._build, ModelConfig=SimpleNamespace(from_dict=lambda c: c)
        )
        audio_utils = SimpleNamespace(
            load_config=lambda _path: {"model_type": "fake"},
            get_model_class=lambda *_args: (model_class, "fake"),
            load_weights=lambda _path: {},
            apply_quantization=lambda *_args: None,
        )
        auto_tokenizer = SimpleNamespace(from_pretrained=self._tokenizer)
        for name, module in {
            "mlx": SimpleNamespace(core=core),
            "mlx.core": core,
            "mlx_audio": SimpleNamespace(tts=tts, utils=audio_utils),
            "mlx_audio.tts": tts,
            "mlx_audio.tts.utils": utils,
            "mlx_audio.utils": audio_utils,
            "transformers": SimpleNamespace(AutoTokenizer=auto_tokenizer),
        }.items():
            monkeypatch.setitem(sys.modules, name, module)

    def _load(self, model_dir: Path, *, lazy: bool) -> FakeModel:
        assert lazy
        self.loaded.append(model_dir)
        return self.model

    def _build(self, config: dict[str, str]) -> FakeModel:
        self.loaded.append(Path(config["model_path"]))
        return self.model

    def _tokenizer(self, directory: Path) -> str:
        self.tokenizers.append(directory)
        return f"tokenizer from {directory}"


class Store:
    """The store's answer on promoted directories, over `root`: an entry is
    installed exactly when its directory exists."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def promoted_dir(self, entry: PinnedArtifact) -> Path:
        return self.root / entry.name / entry.tag

    def installed(self, entry: PinnedArtifact) -> bool:
        return self.promoted_dir(entry).is_dir()
