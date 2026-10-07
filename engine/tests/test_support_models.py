"""Support Models share the Catalog's verified installation boundary."""

import hashlib

from conftest import writing_fetch

from readily_engine.catalog import load_manifest
from readily_engine.catalog.manifest import SupportModel
from readily_engine.store import ModelStore

FILES = {
    "onnx/model.onnx": b"graph",
    "vocab.json": b"vocabulary",
    "preprocessor_config.json": b"preprocessor",
}


def support_model():
    return SupportModel(
        kind="forced-aligner",
        name="wav2vec2",
        tag="base-960h",
        version=1,
        display_name="Word alignment",
        license="Apache-2.0",
        ram_class_gb=1,
        copyright_notice=None,
        copyright_source=None,
        attribution=None,
        provenance="test",
        source={"hf_repo": "test/aligner", "revision": "a" * 40},
        files=[
            {
                "path": path,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
            }
            for path, data in FILES.items()
        ],
    )


def test_support_model_installs_with_its_licence_and_stays_out_of_the_voice_picker(
    tmp_path,
):
    support = support_model()
    catalog = load_manifest().model_copy(update={"support_models": [support]})
    store = ModelStore(tmp_path)
    store.install(support, writing_fetch(FILES))
    assert store.installed(support)
    assert (store.promoted_dir(support) / "LICENSE").is_file()
    assert support in catalog.artifacts
    assert catalog.find(support.id) is None


def test_voice_download_includes_shared_support_and_reuses_it(tmp_path):
    from conftest import make_entry

    from readily_engine.store import DownloadManager

    support = support_model()
    voice = make_entry({"voice.bin": b"voice"}, word_timing=support.id)
    store = ModelStore(tmp_path)
    calls = []

    def fetch(entry, directory):
        calls.append(entry.id)
        writing_fetch(FILES if entry.id == support.id else {"voice.bin": b"voice"})(
            entry, directory
        )

    manager = DownloadManager(store, fetch)
    manager.start(voice, support=(support,))
    manager.thread.join(timeout=5)
    assert manager.snapshot()["phase"] == "installed"
    assert (
        manager.snapshot()["bytesTotal"]
        == voice.download_bytes + support.download_bytes
    )
    assert manager.snapshot()["bytesDownloaded"] == manager.snapshot()["bytesTotal"]
    assert calls == [support.id, voice.id]
    assert manager.delete(voice)
    assert store.installed(support)
    manager.start(voice, support=(support,))
    manager.thread.join(timeout=5)
    assert calls == [support.id, voice.id, voice.id]


def test_corrupt_support_download_is_never_promoted(tmp_path):
    import pytest

    from readily_engine.store import VerificationError

    store = ModelStore(tmp_path)
    support = support_model()
    with pytest.raises(VerificationError):
        store.install(support, writing_fetch({**FILES, "onnx/model.onnx": b"wrong"}))
    assert not store.installed(support)


def test_support_recuration_preserves_the_manifest_and_requires_a_bump(tmp_path):
    import json

    import pytest

    from readily_engine.catalog import MANIFEST_PATH
    from readily_engine.curation.draft import CurationError, SupportDraft
    from readily_engine.curation.merge import publish_support
    from readily_engine.curation.pinning import pin

    draft = SupportDraft.from_entry(support_model())
    staging = tmp_path / "staged"

    def fetch(source, paths, directory):
        writing_fetch(FILES)(support_model(), directory)

    support = draft.entry(pin(draft, staging, fetch))
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(MANIFEST_PATH.read_bytes())
    document = json.loads(manifest.read_text())
    document["support_models"] = []
    manifest.write_text(json.dumps(document, indent=2) + "\n")
    publish_support(manifest, support)
    original = manifest.read_bytes()
    publish_support(manifest, support)
    assert manifest.read_bytes() == original
    moved = support.model_copy(
        update={"source": support.source.model_copy(update={"revision": "b" * 40})}
    )
    with pytest.raises(CurationError, match="bump"):
        publish_support(manifest, moved)


def test_native_timing_models_choose_no_support():
    from conftest import make_entry

    support = support_model()
    catalog = load_manifest().model_copy(update={"support_models": [support]})
    native = make_entry(
        {"voice.bin": b"voice"}, word_timing_languages=("en",), word_timing=support.id
    )
    aligned = make_entry({"voice.bin": b"voice"}, word_timing=support.id)
    for entry, expected in ((native, (native,)), (aligned, (support, aligned))):
        choice = entry.timing_choice(entry.default_voice)
        assert catalog.required_artifacts(entry, {choice}) == expected
