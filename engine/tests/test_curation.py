"""Curation: the scripted step that pins hashes and makes Voice Previews.

Everything here runs offline on synthetic PCM — a fake Synthesizer stands in
for the real one — because CI never downloads a model or runs inference
(engine/README.md § "Test layout"). What the fake cannot prove is proved by
running the script for real against the two committed entries and checking
that the manifest's bytes do not change.
"""

import hashlib
import json
from pathlib import Path

import pytest
from conftest import FILES, draft_for, fetch_writing, make_entry
from curation_fakes import FakeSynthesizer, encode_stub
from pydantic import ValidationError

from readily_engine.catalog import MANIFEST_PATH, CatalogEntry, Source
from readily_engine.curation import (
    CurationError,
    Draft,
    curate,
    pin,
    publish,
)
from readily_engine.curation.merge import _merged_manifest, _replace_manifest
from readily_engine.store import ModelStore, VerificationError


def curated(tmp_path: Path, synthesizer: FakeSynthesizer, **overrides: object):
    draft = draft_for(**overrides)
    return curate(
        draft,
        store=ModelStore(tmp_path / "data"),
        capture_root=tmp_path / "captures",
        fetch=fetch_writing(FILES),
        make_synthesizer=lambda entry, store: synthesizer,
        encode=encode_stub,
    )


def merge_entry(manifest_path: Path, entry: CatalogEntry) -> None:
    """Drive the same merge-and-replace pair `publish` composes, without
    needing staged clips — the seam the manifest-merge tests exercise."""
    _replace_manifest(manifest_path, _merged_manifest(manifest_path, entry))


def manifest_at(path: Path, *entries: CatalogEntry) -> Path:
    """Write a valid manifest holding `entries` and return its path."""
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "default_model": "acme",
                "models": [entry.model_dump(mode="json") for entry in entries],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


class TestDraft:
    @pytest.mark.parametrize("invalid", ["unsupported", "excluded"])
    def test_controls_are_validated_before_a_download(self, invalid):
        from readily_engine.catalog.manifest import load_manifest

        entry = load_manifest().resolve("supertonic")
        named = Draft.from_entry(entry).model_dump()
        if invalid == "unsupported":
            named["parameters"]["ignored"] = named["parameters"]["steps"]
        else:
            named["excluded_parameters"]["steps"] = "Not exposed."
        with pytest.raises(ValueError):
            Draft.model_validate(named)

    def test_reference_digest_is_computed_from_the_curated_clip(self, tmp_path):
        clip = tmp_path / "Chelsie.wav"
        clip.write_bytes(b"curated reference")
        draft = Draft.model_validate(
            {
                **draft_for().model_dump(),
                "voices": [
                    {
                        "id": "narrator",
                        "name": "Narrator",
                        "language": "en-US",
                        "reference": {"clip": "Chelsie.wav", "text": "Hi."},
                    }
                ],
            }
        )

        entry = draft.entry(make_entry(files=FILES).files, {}, references_root=tmp_path)

        assert (
            entry.voices[0].reference.sha256
            == hashlib.sha256(clip.read_bytes()).hexdigest()
        )
        assert (
            "sha256"
            not in Draft.from_entry(entry).model_dump()["voices"][0]["reference"]
        )

    def test_a_draft_round_trips_a_committed_entry(self) -> None:
        entry = make_entry(files=FILES)
        assert Draft.from_entry(entry).entry(entry.files, {}) == entry

    @pytest.mark.parametrize(
        ("path", "refusal"),
        [
            ("../elsewhere.onnx", "climb out"),
            ("weights/../../elsewhere.onnx", "climb out"),
            ("*.onnx", "glob"),
        ],
    )
    def test_a_draft_refuses_a_file_path_the_manifest_would_refuse(
        self, path: str, refusal: str
    ) -> None:
        named = {**draft_for().model_dump(mode="json"), "files": [path]}

        with pytest.raises(ValueError, match=refusal):
            Draft.model_validate(named)

    @pytest.mark.parametrize("field", ["name", "tag"])
    @pytest.mark.parametrize("value", ["../../../outside", "/abs", "a/b"])
    def test_a_draft_refuses_a_name_or_tag_that_would_leave_the_store(
        self, field: str, value: str
    ) -> None:
        """`curate` joins these two into a staging path and downloads into it
        before `entry()` exists to parse them, so an unpatterned one writes
        fetched bytes wherever it points."""
        named = {**draft_for().model_dump(mode="json"), field: value}

        with pytest.raises(ValidationError, match=field):
            Draft.model_validate(named)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("version", 0),
            ("version", "2"),
            ("display_name", ""),
            ("license", ""),
            # A licence with use-based restrictions is refused here, before
            # the draft spends a model download proving it sounds fine.
            ("license", "openrail"),
            ("provenance", ""),
            ("ram_class_gb", 0),
            ("ram_class_gb", float("inf")),
            ("files", []),
            ("files", ["model.onnx", "model.onnx"]),
            ("voices", []),
            ("default_voice", "nobody"),
            # Apache-2.0 carries no holder line, so a source for one is a
            # mistake the draft can catch before the download.
            ("copyright_source", "https://example.com/where-i-read-it"),
        ],
    )
    def test_a_draft_refuses_what_the_manifest_would_refuse(
        self, field: str, value: object
    ) -> None:
        """The editorial constraints are `CatalogEntry`'s own, applied at
        parse time — before curation spends a model download on a draft the
        manifest was always going to refuse."""
        named = {**draft_for().model_dump(mode="json"), field: value}

        with pytest.raises(ValidationError):
            Draft.model_validate(named)

    def test_a_draft_refuses_a_voice_carrying_a_preview_path(self) -> None:
        # The preview is what curation exists to generate: one written by
        # hand names a clip that does not exist, or someone else's.
        named = draft_for().model_dump(mode="json")
        named["voices"] = [
            {
                "id": "narrator",
                "name": "Narrator",
                "language": "en-US",
                "preview": "acme/1m/narrator.m4a",
            }
        ]

        with pytest.raises(ValidationError, match="preview"):
            Draft.model_validate(named)

    @pytest.mark.parametrize("name", ["LICENSE", "license.md", "NOTICE"])
    def test_a_draft_does_not_name_the_files_curation_finds_itself(
        self, name: str
    ) -> None:
        # Curation asks upstream for every spelling of its licence and
        # notice files, so a draft naming one would only pin a copy that
        # outlives the revision it came from.
        named = {**draft_for().model_dump(mode="json"), "files": [*FILES, name]}

        with pytest.raises(ValidationError, match=name):
            Draft.model_validate(named)

    def test_a_draft_reads_a_committed_entry_by_dropping_its_previews(self) -> None:
        entry = make_entry(
            files=FILES,
            voices=[
                {
                    "id": "narrator",
                    "name": "Narrator",
                    "language": "en-US",
                    "preview": "acme/1m/narrator.m4a",
                }
            ],
        )

        draft = Draft.from_entry(entry)

        assert [voice.id for voice in draft.voices] == ["narrator"]
        assert draft.entry(entry.files, {}).voices[0].preview is None


class TestPinning:
    def test_pinning_emits_the_hash_and_size_of_what_arrived(
        self, tmp_path: Path
    ) -> None:
        pinned = pin(draft_for(), tmp_path, fetch_writing(FILES))

        assert [file.path for file in pinned] == list(FILES)
        assert [file.size_bytes for file in pinned] == [
            len(content) for content in FILES.values()
        ]
        assert list(pinned) == make_entry(files=FILES).files

    def test_pinning_refuses_a_file_the_repo_did_not_supply(
        self, tmp_path: Path
    ) -> None:
        def fetch(source: Source, paths: list[str], staging_dir: Path) -> None:
            del source, paths, staging_dir

        with pytest.raises(CurationError, match=r"model\.onnx"):
            pin(draft_for(), tmp_path, fetch)

    def test_a_refused_pin_leaves_no_empty_staging_behind(self, tmp_path: Path) -> None:
        # A draft refused at the pin step downloaded nothing, so the
        # directory it staged into is litter in the user's data dir.
        def fetch(source: Source, paths: list[str], staging_dir: Path) -> None:
            del source, paths, staging_dir

        staging = tmp_path / "staging" / "acme" / "1m"

        with pytest.raises(CurationError):
            pin(draft_for(), staging, fetch)

        assert not staging.exists()
        assert not staging.parent.exists()

    def test_a_pin_that_refused_a_partial_download_keeps_it_for_the_retry(
        self, tmp_path: Path
    ) -> None:
        # Staging is deliberately resumable: bytes that did arrive are worth
        # more than a tidy directory tree.
        def fetch(source: Source, paths: list[str], staging_dir: Path) -> None:
            del source, paths
            (staging_dir / "model.onnx").write_bytes(FILES["model.onnx"])

        staging = tmp_path / "staging" / "acme" / "1m"

        with pytest.raises(CurationError, match=r"voices/pack\.bin"):
            pin(draft_for(), staging, fetch)

        assert (staging / "model.onnx").is_file()

    def test_pinning_refuses_a_symlink_standing_in_for_a_file(
        self, tmp_path: Path
    ) -> None:
        def fetch(source: Source, paths: list[str], staging_dir: Path) -> None:
            del source
            outside = tmp_path / "outside.bin"
            outside.write_bytes(b"not the model")
            for path in paths:
                target = staging_dir / path
                target.parent.mkdir(parents=True, exist_ok=True)
                # A case-insensitive filesystem finds "license" at "LICENSE".
                if not target.is_symlink():
                    target.symlink_to(outside)

        with pytest.raises(CurationError, match="symlink"):
            pin(draft_for(), tmp_path / "staging", fetch)

    def test_pinning_refuses_a_file_reached_through_a_symlinked_directory(
        self, tmp_path: Path
    ) -> None:
        """The leaf is a real file; its parent is the link. `store._verify`
        checks the leaf alone because `_keep_only_manifest_files` has already
        stripped staging by then, and pinning runs before any such step, so
        it has to walk up to staging itself."""
        outside = tmp_path / "outside"
        (outside / "").mkdir(parents=True, exist_ok=True)
        (outside / "pack.bin").write_bytes(b"never downloaded")

        def fetch(source: Source, paths: list[str], staging_dir: Path) -> None:
            del source, paths
            (staging_dir / "model.onnx").write_bytes(FILES["model.onnx"])
            (staging_dir / "voices").symlink_to(outside, target_is_directory=True)

        with pytest.raises(CurationError, match="symlink"):
            pin(draft_for(), tmp_path / "staging", fetch)


class TestCuration:
    def test_curation_pins_the_files_and_stages_a_clip_for_every_voice(
        self, tmp_path: Path
    ) -> None:
        voices = [
            {"id": "narrator", "name": "Narrator", "language": "en-US"},
            {"id": "af_sky", "name": "Sky", "language": "en-US"},
        ]
        result = curated(tmp_path, FakeSynthesizer(), voices=voices)

        assert result.entry.files == make_entry(files=FILES).files
        assert [voice.preview for voice in result.entry.voices] == [
            "acme/1m/narrator.m4a",
            "acme/1m/af_sky.m4a",
        ]
        assert set(result.clips) == {
            str(voice.preview) for voice in result.entry.voices
        }
        for clip in result.clips.values():
            assert clip.is_file()
            assert clip.is_relative_to(result.capture_dir)
        assert result.report.verdict == 0

    def test_curation_writes_nothing_outside_its_own_directories(
        self, tmp_path: Path
    ) -> None:
        """A run without `--write` is an inspection: everything it makes
        stays under the store and the capture root, so nothing a release
        reads — clips, manifest — changes until `publish` is asked to."""
        curated(tmp_path, FakeSynthesizer())

        assert {child.name for child in tmp_path.iterdir()} == {"data", "captures"}

    def test_curation_promotes_the_model_before_loading_it(
        self, tmp_path: Path
    ) -> None:
        result = curated(tmp_path, FakeSynthesizer())

        store = ModelStore(tmp_path / "data")
        assert store.installed(result.entry)

    def test_curation_refuses_a_model_the_analyzer_fails(self, tmp_path: Path) -> None:
        with pytest.raises(CurationError, match="did not qualify"):
            curated(tmp_path, FakeSynthesizer(amplitude=1.4, dc=0.2))

    def test_a_refused_model_stages_no_clip(self, tmp_path: Path) -> None:
        with pytest.raises(CurationError):
            curated(tmp_path, FakeSynthesizer(amplitude=1.4, dc=0.2))

        assert not list(tmp_path.rglob("*.m4a"))

    def test_curation_refuses_a_backend_that_only_emits_silence(
        self, tmp_path: Path
    ) -> None:
        # Every analyzer check looks for an artifact, and silence has none:
        # an unloaded voice pack would qualify with a clean verdict and ship
        # a silent Voice Preview.
        with pytest.raises(CurationError, match=r"not speaking|trimmed to nothing"):
            curated(tmp_path, FakeSynthesizer(amplitude=0.0))

        assert not list(tmp_path.rglob("*.m4a"))

    def test_curation_re_promotes_a_model_whose_weights_moved(
        self, tmp_path: Path
    ) -> None:
        """A weights bump is the case the script exists for. `promote_staged`
        replaces whatever an earlier curation left promoted, so the capture
        comes from the bytes just pinned, never the superseded weights."""
        store = ModelStore(tmp_path / "data")
        bumped = {path: content + b"-v2" for path, content in FILES.items()}

        curate(
            draft_for(),
            store=store,
            capture_root=tmp_path / "captures",
            fetch=fetch_writing(FILES),
            make_synthesizer=lambda entry, store: FakeSynthesizer(),
            encode=encode_stub,
        )
        result = curate(
            draft_for(),
            store=store,
            capture_root=tmp_path / "captures",
            fetch=fetch_writing(bumped),
            make_synthesizer=lambda entry, store: FakeSynthesizer(),
            encode=encode_stub,
        )

        promoted = store.promoted_dir(result.entry)
        for file in result.entry.files:
            assert (promoted / file.path).read_bytes() == bumped[file.path]
        assert result.entry.files != make_entry(files=FILES).files

    def test_a_curation_that_cannot_install_leaves_nothing_behind(
        self, tmp_path: Path
    ) -> None:
        """`promote_staged` retires the promoted model before verifying the
        bytes just pinned, so a refused promotion must not leave the retired
        tree — a whole model's worth of disk — for nobody to sweep.

        The corruption is what makes the store's own verification refuse,
        rather than a fake that raises in its place: `pin` hashes the bytes
        it downloaded, so the only way promotion disagrees is for them to
        change underneath it. Staging goes too, because `_verify` discards
        it, and bytes that stopped matching a hash taken a moment ago are
        worth re-downloading rather than resuming.
        """

        class CorruptingStore(ModelStore):
            def promote_staged(self, entry: CatalogEntry) -> None:
                staging = self.staging_dir(entry.name, entry.tag)
                # Same length, so it is the hash that refuses and not the size.
                (staging / "model.onnx").write_bytes(FILES["model.onnx"][::-1])
                super().promote_staged(entry)

        data_dir = tmp_path / "data"
        curated(tmp_path, FakeSynthesizer())
        store = CorruptingStore(data_dir)
        entry = make_entry(files=FILES)
        assert store.installed(entry)

        with pytest.raises(VerificationError, match="does not match its pin"):
            curate(
                draft_for(),
                store=store,
                capture_root=tmp_path / "captures",
                fetch=fetch_writing(FILES),
                make_synthesizer=lambda entry, store: FakeSynthesizer(),
                encode=encode_stub,
            )

        assert not list(data_dir.rglob("*.deleting"))
        assert not store.installed(entry)
        assert store.staged_bytes(entry) == 0

    @pytest.mark.parametrize("voice_id", ["../../escaped", "nested/one"])
    def test_curation_refuses_a_voice_id_that_is_not_one_path_segment(
        self, tmp_path: Path, voice_id: str
    ) -> None:
        """A Voice id spells a clip's filename, so `/` in one either climbs
        out of the preview root or scatters an entry's clips into directories
        the orphan sweep never looks in. The schema refuses both before a
        draft exists to curate."""
        voices = [{"id": voice_id, "name": "Escaped", "language": "en-US"}]

        with pytest.raises(ValidationError, match="id"):
            curated(
                tmp_path,
                FakeSynthesizer(),
                voices=voices,
                default_voice=voice_id,
            )

        assert not list(tmp_path.rglob("*escaped*"))
        assert not list(tmp_path.rglob("*nested*"))


class TestPublish:
    def test_publish_lands_the_clips_and_the_manifest_row_together(
        self, tmp_path: Path
    ) -> None:
        manifest_path = manifest_at(tmp_path / "manifest.json", make_entry(files=FILES))
        previews = tmp_path / "previews"
        result = curated(tmp_path, FakeSynthesizer())

        published = publish(manifest_path, previews, result)

        assert published == (previews / "acme" / "1m" / "narrator.m4a",)
        for relpath, staged in result.clips.items():
            assert (previews / relpath).read_bytes() == staged.read_bytes()
        merged = json.loads(manifest_path.read_text())["models"][0]
        assert merged["voices"][0]["preview"] == "acme/1m/narrator.m4a"

    def test_publish_sweeps_the_clips_of_voices_the_entry_dropped(
        self, tmp_path: Path
    ) -> None:
        """A dropped or renamed Voice would otherwise leave its clip in the
        app bundle forever, shipped and referenced by nothing."""
        manifest_path = manifest_at(tmp_path / "manifest.json", make_entry(files=FILES))
        previews = tmp_path / "previews"
        both = curated(
            tmp_path,
            FakeSynthesizer(),
            voices=[
                {"id": "narrator", "name": "Narrator", "language": "en-US"},
                {"id": "af_sky", "name": "Sky", "language": "en-US"},
            ],
        )
        publish(manifest_path, previews, both)
        # Another entry's clip, to prove the sweep cannot reach out of the
        # entry being published.
        elsewhere = previews / "qwen3-tts" / "0.6b" / "Chelsie.m4a"
        elsewhere.parent.mkdir(parents=True)
        elsewhere.write_bytes(b"another entry's clip")
        # A kill mid-copy leaves a scratch behind; its name keeps `.m4a`
        # last precisely so this sweep sees it as a clip no Voice names.
        litter = previews / "acme" / "1m" / "narrator.deadbeef.m4a"
        litter.write_bytes(b"scratch a kill left behind")

        publish(manifest_path, previews, curated(tmp_path, FakeSynthesizer()))

        assert (previews / "acme" / "1m" / "narrator.m4a").is_file()
        assert not (previews / "acme" / "1m" / "af_sky.m4a").exists()
        assert not litter.exists()
        assert elsewhere.is_file()

    def test_publish_refuses_clips_that_do_not_cover_the_voices(
        self, tmp_path: Path
    ) -> None:
        """A `CuratedEntry` built by hand could pair a row with fewer clips
        than its Voices name — a committed `preview` no copy ever writes."""
        from dataclasses import replace

        manifest_path = manifest_at(tmp_path / "manifest.json", make_entry(files=FILES))
        previews = tmp_path / "previews"
        before = manifest_path.read_bytes()
        clipless = replace(curated(tmp_path, FakeSynthesizer()), clips={})

        with pytest.raises(CurationError, match="do not match"):
            publish(manifest_path, previews, clipless)

        assert manifest_path.read_bytes() == before
        assert not previews.exists()

    def test_a_refused_publish_lands_no_clip(self, tmp_path: Path) -> None:
        """The prospective manifest is validated before the first clip is
        copied: a publish the manifest refuses — here, new pins under an
        unmoved version — must leave the preview root as it found it."""
        committed = make_entry(files={path: b"old" for path in FILES})
        manifest_path = manifest_at(tmp_path / "manifest.json", committed)
        previews = tmp_path / "previews"
        before = manifest_path.read_bytes()

        with pytest.raises(CurationError, match="bump version"):
            publish(manifest_path, previews, curated(tmp_path, FakeSynthesizer()))

        assert manifest_path.read_bytes() == before
        assert not previews.exists()


class TestManifestMerge:
    def test_merging_replaces_the_entry_in_place(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "manifest.json"
        original = {
            "schema_version": 1,
            "default_model": "acme",
            "models": [
                make_entry(files=FILES).model_dump(mode="json"),
                make_entry(files=FILES, name="qwen3-tts", tag="0.6b").model_dump(
                    mode="json"
                ),
            ],
        }
        manifest_path.write_text(json.dumps(original, indent=2) + "\n")
        updated = make_entry(files=FILES, display_name="Kokoro 82M")

        merge_entry(manifest_path, updated)

        models = json.loads(manifest_path.read_text())["models"]
        assert [model["name"] for model in models] == ["acme", "qwen3-tts"]
        assert models[0]["display_name"] == "Kokoro 82M"

    def test_merging_appends_a_new_entry(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "default_model": "acme",
                    "models": [make_entry(files=FILES).model_dump(mode="json")],
                },
                indent=2,
            )
            + "\n"
        )
        added = make_entry(
            files=FILES, name="qwen3-tts", tag="0.6b", architecture="qwen3"
        )

        merge_entry(manifest_path, added)

        text = manifest_path.read_text()
        assert text.endswith("}\n")
        assert json.loads(text)["models"][1]["name"] == "qwen3-tts"

    def test_merging_leaves_prose_outside_ascii_as_itself(self, tmp_path: Path) -> None:
        # An escape would rewrite rows nobody curated, and a manifest whose
        # untouched entries change is a regeneration proof that proves
        # nothing.
        manifest_path = tmp_path / "manifest.json"
        entry = make_entry(files=FILES, provenance="Hashed from a snapshot — twice.")
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "default_model": "acme",
                    "models": [entry.model_dump(mode="json")],
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        before = manifest_path.read_bytes()

        merge_entry(manifest_path, entry)

        assert manifest_path.read_bytes() == before
        assert "—" in manifest_path.read_text(encoding="utf-8")

    def test_a_refused_merge_leaves_the_manifest_untouched(
        self, tmp_path: Path
    ) -> None:
        # The manifest is the trust root every download is checked against:
        # a torn write is an Engine that cannot start.
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {"schema_version": 1, "default_model": "acme", "models": []},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        before = manifest_path.read_bytes()

        with pytest.raises(ValidationError):
            merge_entry(manifest_path, make_entry(files=FILES, default_tag=False))

        assert manifest_path.read_bytes() == before
        assert list(tmp_path.iterdir()) == [manifest_path]

    def test_merging_refuses_a_version_rollback(self, tmp_path: Path) -> None:
        # Two different pin sets would collide on one Segment-cache key.
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "default_model": "acme",
                    "models": [
                        make_entry(files=FILES, version=3).model_dump(mode="json")
                    ],
                },
                indent=2,
            )
            + "\n"
        )

        with pytest.raises(CurationError, match="rolls back"):
            merge_entry(manifest_path, make_entry(files=FILES, version=2))

    def test_merging_refuses_new_weights_under_an_unmoved_version(
        self, tmp_path: Path
    ) -> None:
        """Segment-cache keys carry `version`: a re-pin that ships different
        bytes under the same number leaves audio narrated by the old weights
        cached and playing alongside the new ones."""
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "default_model": "acme",
                    "models": [make_entry(files=FILES).model_dump(mode="json")],
                },
                indent=2,
            )
            + "\n"
        )
        bumped_bytes = {path: content + b"-v2" for path, content in FILES.items()}

        with pytest.raises(CurationError, match="bump version"):
            merge_entry(manifest_path, make_entry(files=bumped_bytes))

        merge_entry(manifest_path, make_entry(files=bumped_bytes, version=2))
        merged = json.loads(manifest_path.read_text())["models"][0]
        assert merged["version"] == 2

    def test_merging_accepts_a_pure_files_reorder_under_the_same_version(
        self, tmp_path: Path
    ) -> None:
        """Reordered `files` name the same bytes: forcing a bump for them
        would invalidate every cached Segment for unchanged weights."""
        manifest_path = manifest_at(tmp_path / "manifest.json", make_entry(files=FILES))
        reordered = dict(reversed(list(FILES.items())))

        merge_entry(manifest_path, make_entry(files=reordered))

        merged = json.loads(manifest_path.read_text())["models"][0]
        assert [file["path"] for file in merged["files"]] == list(reordered)
        assert merged["version"] == 1

    @pytest.mark.parametrize("changed", ["clip", "text", "sha256"])
    def test_merging_refuses_a_voice_reference_swap_under_the_same_version(
        self, tmp_path: Path, changed: str
    ) -> None:
        """The Segment-cache key does not carry the clip a Voice clones, so a
        reference change under an unmoved version would play Segments from
        two different narrators on one key."""

        def voiced(clip: str) -> list[dict[str, object]]:
            return [
                {
                    "id": "Chelsie",
                    "name": "Chelsie",
                    "language": "en-US",
                    "reference": {"clip": clip, "text": "Hi.", "sha256": "a" * 64},
                }
            ]

        committed = make_entry(
            files=FILES,
            voices=voiced("qwen3-tts/0.6b/Chelsie.wav"),
            default_voice="Chelsie",
        )
        manifest_path = manifest_at(tmp_path / "manifest.json", committed)
        swapped = voiced("qwen3-tts/0.6b/Chelsie.wav")
        replacement = {
            "clip": "qwen3-tts/0.6b/Chelsie-tail.wav",
            "text": "Hello.",
            "sha256": "b" * 64,
        }[changed]
        swapped[0]["reference"][changed] = replacement

        with pytest.raises(CurationError, match="bump version"):
            merge_entry(
                manifest_path,
                make_entry(files=FILES, voices=swapped, default_voice="Chelsie"),
            )

        merge_entry(
            manifest_path,
            make_entry(files=FILES, voices=swapped, default_voice="Chelsie", version=2),
        )
        merged = json.loads(manifest_path.read_text())["models"][0]
        assert merged["voices"][0]["reference"][changed] == replacement

    def test_merging_keeps_the_committed_file_byte_identical(
        self, tmp_path: Path
    ) -> None:
        """Re-curating an entry that has not changed rewrites the same bytes,
        which is what makes `--write` plus `git diff --exit-code` the
        hash-regeneration proof."""
        from readily_engine.catalog import MANIFEST_PATH, load_manifest

        original = MANIFEST_PATH.read_bytes()
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_bytes(original)

        for entry in load_manifest(manifest_path).models:
            merge_entry(manifest_path, entry)

        assert manifest_path.read_bytes() == original


DRAFTS_DIR = MANIFEST_PATH.parent / "drafts"


@pytest.mark.parametrize(
    "draft_path", sorted(DRAFTS_DIR.glob("*.json")), ids=lambda path: path.name
)
def test_a_committed_draft_is_one_the_curation_script_would_accept(
    draft_path: Path,
) -> None:
    # A draft waits in `catalog/drafts/` for a macOS curation run; if it can
    # only be refused there, the wait was for nothing.
    draft = Draft.model_validate_json(draft_path.read_text())
    assert draft_path.stem == f"{draft.name}-{draft.tag}"


def test_curated_entries_stay_valid_catalog_entries(tmp_path: Path) -> None:
    result = curated(tmp_path, FakeSynthesizer())

    assert CatalogEntry.model_validate(result.entry.model_dump(mode="json")) == (
        result.entry
    )


def test_a_draft_naming_no_architecture_is_refused_before_any_download(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # The Manifest parser accepts any well-formed id (`catalog/` imports
    # nothing from `loading/`), so the CLI is where a typo is caught: before
    # a byte downloads and before `--write` commits an entry no Architecture loads.
    from readily_engine.curation import cli

    draft = tmp_path / "parler.json"
    draft.write_text(
        json.dumps(draft_for(architecture="parler").model_dump(mode="json"))
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    monkeypatch.setattr(cli, "curate", lambda *_args, **_kwargs: pytest.fail("ran"))

    argv = [str(draft), "--write", "--manifest", str(manifest)]
    assert cli.main([*argv, "--data-dir", str(tmp_path / "store")]) == 1

    assert "refused: 'parler' names no Architecture" in capsys.readouterr().out
    assert manifest.read_text() == "{}"


def test_a_draft_setting_knobs_its_architecture_refuses_is_refused_before_any_download(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from readily_engine.curation import cli

    draft = tmp_path / "supertonic.json"
    draft.write_text(
        json.dumps(draft_for(architecture="supertonic").model_dump(mode="json"))
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    monkeypatch.setattr(cli, "curate", lambda *_args, **_kwargs: pytest.fail("ran"))

    argv = [str(draft), "--write", "--manifest", str(manifest)]
    assert cli.main([*argv, "--data-dir", str(tmp_path / "store")]) == 1

    assert "refused: acme:1m: generation_parameters" in capsys.readouterr().out
    assert manifest.read_text() == "{}"
