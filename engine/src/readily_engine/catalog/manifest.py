"""The Catalog Manifest: the definitive description of the Catalog, parsed.

`catalog/manifest.json` is baked into the release (ADR 0003 §1) — there is no
runtime catalog fetch, so "a compromised catalog host swaps a model URL" is
structurally impossible and this file is the whole trust root for what the
Engine will download and run. The models below are the schema: CI validates
the committed manifest by parsing it with exactly the code the Engine parses
it with, so a manifest that passes is one the Engine can read.

Everything here is offline and pure: no filesystem beyond reading the given
path, no network, no model loading.
"""

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

from readily_engine.catalog.controls import (
    READILY_CONTROLS,
    ChoiceControl,
    Control,
    Overrides,
)
from readily_engine.catalog.licence_table import LICENCE_OBLIGATIONS, is_licence_file
from readily_engine.catalog.recipes import Qualification
from readily_engine.generation import Parameters

# ADR 0003 §1: the manifest ships at `catalog/manifest.json`, declared as an
# app resource in `tauri.conf.json`. Resolved from the package so the same
# path finds the repo checkout's copy and the bundle's.
MANIFEST_PATH = Path(__file__).resolve().parents[4] / "catalog" / "manifest.json"

# Voice References are Engine resources, unlike webview-facing Voice Previews.
REFERENCE_ROOT = MANIFEST_PATH.parent / "references"

SCHEMA_VERSION = 1

_ENTRY_NAME = r"^[a-z0-9]+(-[a-z0-9]+)*$"
_ENTRY_TAG = r"^[a-z0-9]+([.-][a-z0-9]+)*$"
_SHA256 = r"^[0-9a-f]{64}$"
_GIT_COMMIT = r"^[0-9a-f]{40}$"
_HF_REPO = r"^[A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*$"
_PREVIEW_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_VOICE_ID = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"


class Tier(StrEnum):
    """The Catalog's editorial label for what a Voice Model is *for*, as the
    picker's pill renders it (CONTEXT.md). Curated, not computed, and
    deliberately not a scale: a model is instant or it is expressive."""

    INSTANT = "instant"
    EXPRESSIVE = "expressive"


class Strict(BaseModel):
    """Unknown keys are refused throughout: a misspelled tunable that is
    silently dropped is a tunable that does nothing, and a baked manifest has
    no runtime to report it."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Source(Strict):
    """Where an entry's files come from, pinned so they cannot move.

    ADR 0003 §2 pins twice: the repo is named with an immutable revision
    commit, and every file carries its SHA-256. A branch or tag would be
    exactly the pin a compromised upstream can quietly re-point.
    """

    hf_repo: str = Field(pattern=_HF_REPO)
    revision: str = Field(pattern=_GIT_COMMIT)

    @property
    def page_url(self) -> str:
        """The Hugging Face page for the pinned revision."""
        return f"https://huggingface.co/{self.hf_repo}/tree/{self.revision}"


class LicenceAttribution(Strict):
    """Reader-facing attribution facts for a licence that asks for them."""

    creator: str = Field(min_length=1)
    copyright_notice: str = Field(min_length=1)
    modified: bool = Field(strict=True)

    @field_validator("creator", "copyright_notice")
    @classmethod
    def attribution_text_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("attribution text is blank")
        return value


# The file `store/` leaves inside a broken promoted directory it could not
# retire, and reads as "not installed". Named here so `model_file_path` can
# refuse an artifact that would pass for it.
BROKEN_MARKER = ".broken"


def model_file_path(value: str) -> str:
    """Validate one relative path the Engine will download and promote.

    Shared with `curation/`, whose draft names these paths before any hash
    exists to pin them: a draft that could name `../` or a glob would hand
    the same two openings to the download step that this rule closes for
    the manifest, one step earlier.
    """
    # `store/` joins this onto the model directory. A path that climbs
    # out of it writes wherever it likes, hash-verified all the way.
    segments = value.split("/")
    if (
        not value
        or "\\" in value
        or any(segment in ("", ".", "..") for segment in segments)
    ):
        raise ValueError(
            "file paths are relative and never climb out of the model directory"
        )
    # `download/` hands these to huggingface_hub as allow-patterns,
    # which are fnmatch globs. A path that is also a pattern could
    # match more than the one file the manifest names (B1).
    if any(wildcard in value for wildcard in "*?[]"):
        raise ValueError("file paths name exact files, never glob patterns")
    # `store/` leaves `BROKEN_MARKER` at the top of a promoted directory it
    # found corrupt and could not retire, and reads its presence as "not
    # installed". An artifact of that name would look like the marker, in
    # any spelling: the reader's volume ignores case.
    if segments[0].casefold() == BROKEN_MARKER:
        raise ValueError(
            f"file paths never start with {BROKEN_MARKER}, the store's own marker"
        )
    return value


def _bundled_clip_path(value: str, *, kind: str, suffix: str) -> str:
    if not all(_PREVIEW_SEGMENT.fullmatch(segment) for segment in value.split("/")):
        raise ValueError(
            f"{kind} paths are relative and name a file inside the bundled {kind} root"
        )
    if not value.endswith(suffix):
        raise ValueError(f"{kind} clips are {suffix}")
    return value


def preview_clip_path(value: str) -> str:
    """Validate one relative path to a bundled Voice Preview clip.

    Shared with `curation/`, which builds this path out of a Voice's id and
    writes a file at it: validating there too means an id that could climb
    out is refused before the first byte lands, rather than after, by the
    schema this rule already guards.
    """
    # This one goes on the wire and the webview turns it into a URL it
    # loads audio from. A path that climbs out, or one that starts `//`
    # and becomes protocol-relative, would be the UI fetching from
    # somewhere other than the app bundle — which the privacy invariant
    # forbids outright. One container, so the shell has one `<audio>` story
    # and the curation script has one encoder (ADR 0004 §4 already spends
    # afconvert's AAC lane on Export).
    return _bundled_clip_path(value, kind="preview", suffix=".m4a")


def reference_clip_path(value: str) -> str:
    """Validate one relative path to a bundled Voice reference clip.

    This one never goes on the wire — the Engine decodes it and hands the
    samples to the model in memory — but it is still a path a hand-edited
    manifest could point outside the bundle, so it gets the preview rule.
    WAV, because the Engine reads it with the standard library: mlx-audio's
    own decoder is the SciPy path ADR 0006 keeps out of the shipped Engine.
    """
    return _bundled_clip_path(value, kind="reference", suffix=".wav")


class ManifestFile(Strict):
    """One file the Engine downloads and hash-verifies before promoting it.

    The Engine fetches exactly the files named here, never "the whole repo"
    (threat model B1), so an upstream that gains a malicious extra file
    gains nothing.
    """

    path: str
    sha256: str = Field(pattern=_SHA256)
    size_bytes: int = Field(gt=0)

    @field_validator("path")
    @classmethod
    def path_stays_inside_the_model_directory(cls, value: str) -> str:
        return model_file_path(value)


class ReferenceAttribution(LicenceAttribution):
    """Where a Voice Reference clip came from, when it is not Readily's own.

    A clip cut from a consented speech corpus ships under that corpus's
    licence, and the same allowlist that admits weights admits clips (ADR
    0009, clip amendment). Only a licence that asks for reader-facing
    attribution has anything to say here, so `license` is held to that,
    and `source` is the corpus page the credit points a reader at.
    """

    license: str = Field(min_length=1)
    source: HttpUrl

    @field_validator("license")
    @classmethod
    def the_licence_is_one_that_asks_for_a_credit(cls, value: str) -> str:
        obligations = LICENCE_OBLIGATIONS.get(value)
        if obligations is None:
            raise ValueError(
                f"{value!r} is not on the Catalog's redistributable licence "
                f"allowlist ({', '.join(sorted(LICENCE_OBLIGATIONS))})"
            )
        if not obligations.requires_attribution:
            raise ValueError(
                f"{value} asks for no reader-facing attribution, so a clip "
                "under it is Readily's own: declare attribution null"
            )
        return value


class UnpinnedVoiceReference(Strict):
    """The clip a curator names to condition a Voice, and its verbatim
    transcript, before curation pins the clip's bytes. `attribution` is
    the clip's credit when it came from a consented corpus, and `null`
    for a clip Readily recorded itself; a curator says which."""

    clip: str
    text: str
    attribution: ReferenceAttribution | None

    @field_validator("clip")
    @classmethod
    def clip_names_a_file_inside_the_bundled_reference_root(cls, value: str) -> str:
        return reference_clip_path(value)

    @field_validator("text")
    @classmethod
    def text_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a reference transcript cannot be blank")
        return value


class VoiceReference(UnpinnedVoiceReference):
    """A Voice Reference pinned to the exact bundled WAV bytes."""

    sha256: str = Field(pattern=_SHA256)


class VoiceIdentity(Strict):
    """The name, id and language shared by draft and curated Voices."""

    # One path segment, because `curation/` spells a clip
    # `<name>/<tag>/<id>.m4a` and sweeps orphans from that one directory. An
    # id carrying a `/` would scatter one entry's clips across several
    # parents, and the sweep would silently leave the ones it never looked in.
    id: str = Field(pattern=_VOICE_ID)
    name: str = Field(min_length=1)
    language: str = Field(min_length=1)


class Voice(VoiceIdentity):
    """One named speaking identity an entry offers (CONTEXT.md).

    `preview` names this Voice's bundled Voice Preview clip — the curation
    artifact that lets a Voice be auditioned before its Voice Model is
    downloaded (ADR 0003 consequences). It is `None` until the curation
    script has made one, which is what lets the Catalog carry an entry whose
    clips have not been generated yet.
    """

    qualification: Qualification | None = None
    reference: VoiceReference | None = None
    preview: str | None = None

    @field_validator("preview")
    @classmethod
    def preview_names_a_clip_inside_the_bundled_preview_root(
        cls, value: str | None
    ) -> str | None:
        return None if value is None else preview_clip_path(value)


# What each pause policy version keeps around the trimmed speech, in ms.
# Version 1 cut on the active frame; version 2 keeps a quiet release or a
# breath. A new version needs a row here, `LATEST_PAUSE_POLICY_VERSION`, the
# `Literal` on `pause_policy_version`, and History's CHECK constraint on the
# saved column (`storage/history.py`).
_TRIM_MARGIN_MS = {1: 0, 2: 40}
LATEST_PAUSE_POLICY_VERSION = 2


class PausePolicy(Strict):
    """Assembly settings frozen with a Narration, independent of Segment keys.

    Pause values are saved with the version, so a later Catalog edit cannot
    move a saved Narration's timeline. Keep old versions executable for
    replay and Export.
    """

    pause_policy_version: Literal[1, 2] = LATEST_PAUSE_POLICY_VERSION
    pause_sentence_ms: int = Field(ge=0)
    pause_paragraph_break_ms: int = Field(ge=0)

    @property
    def trim_margin_ms(self) -> int:
        return _TRIM_MARGIN_MS[self.pause_policy_version]


class Tunables(PausePolicy):
    """The per-entry chunker and assembler settings ADR 0002 puts in the
    Catalog entry rather than in the pipeline, because they are properties of
    the model: how much text it narrates well in one pass, and how long its
    pauses want to be."""

    chunk_budget_chars: int = Field(gt=0)
    first_block_chars: int = Field(gt=0)

    @property
    def pause_policy(self) -> PausePolicy:
        """Snapshot only assembly settings, without the chunker's budgets."""
        return PausePolicy(**self.model_dump(include=set(PausePolicy.model_fields)))

    @model_validator(mode="after")
    def the_first_block_is_short(self) -> Self:
        # ADR 0002 §2: the first Block is one short inference so first audio
        # arrives fast. Longer than the budget, it is not a first Block.
        if self.first_block_chars > self.chunk_budget_chars:
            raise ValueError("first_block_chars must not exceed chunk_budget_chars")
        return self


class DerivedFile(Strict):
    """A file the store writes beside a pinned ONNX export at install: the
    export's bytes with `outputs` appended to its graph's output list, so
    a lane can read a tensor the export computes but does not expose (ADR
    0011). Derived rather than fetched, so it carries no pin: the store
    rebuilds it from the verified source whenever the file on disk is not
    what it would write.
    """

    path: str
    source: str
    outputs: tuple[str, ...] = Field(min_length=1)

    @field_validator("path", "source")
    @classmethod
    def paths_stay_inside_the_model_directory(cls, value: str) -> str:
        return model_file_path(value)


class ArtifactEditorial(Strict):
    """Identity, provenance and licence shared by Voice and Support Models."""

    name: str = Field(pattern=_ENTRY_NAME)
    tag: str = Field(pattern=_ENTRY_TAG)
    display_name: str = Field(min_length=1)
    version: int = Field(ge=1, strict=True)
    license: str = Field(min_length=1)
    copyright_notice: str | None
    copyright_source: HttpUrl | None
    provenance: str = Field(min_length=1)
    source: Source
    attribution: LicenceAttribution | None
    derived_files: tuple[DerivedFile, ...] = ()

    @field_validator("license")
    @classmethod
    def the_licence_is_one_readily_may_hand_a_reader(cls, value: str) -> str:
        if value not in LICENCE_OBLIGATIONS:
            raise ValueError(
                f"{value!r} is not on the Catalog's redistributable licence "
                f"allowlist ({', '.join(sorted(LICENCE_OBLIGATIONS))})"
            )
        return value

    @model_validator(mode="after")
    def the_notice_is_declared_when_the_licence_is_one(self) -> Self:
        # Under any licence the notice is whatever upstream's file writes
        # beside the terms, declared so curation can hold the file to it.
        # Where the licence is that notice, the declaration is required even
        # if upstream has no file and the store must write the notice itself.
        if self.copyright_notice is None:
            if LICENCE_OBLIGATIONS[self.license].requires_holder_notice:
                raise ValueError(
                    f"{self.license} is the holder's notice, so copyright_notice "
                    "must say what upstream's licence file writes above its terms"
                )
        elif not self.copyright_notice.strip():
            raise ValueError("copyright_notice is blank; declare it null instead")
        return self

    @model_validator(mode="after")
    def the_attribution_matches_the_licence(self) -> Self:
        needs_attribution = LICENCE_OBLIGATIONS[self.license].requires_attribution
        if needs_attribution and self.attribution is None:
            raise ValueError(
                f"{self.license} asks for reader-facing attribution, so "
                "attribution must declare it"
            )
        if not needs_attribution and self.attribution is not None:
            raise ValueError(
                f"{self.license} asks for no reader-facing attribution, so "
                "attribution must be null"
            )
        return self


class PinnedArtifact(ArtifactEditorial):
    """A complete set of files admitted through the Catalog's download boundary."""

    files: list[ManifestFile] = Field(min_length=1)

    @property
    def id(self) -> str:
        return f"{self.name}:{self.tag}"

    @property
    def download_bytes(self) -> int:
        """What accepting this entry costs the user in disk and bandwidth."""
        return sum(file.size_bytes for file in self.files)

    @property
    def derived_bytes(self) -> int:
        """What the store writes beside the download at install: a copy of
        each derived file's source (ADR 0011), give or take the few dozen
        bytes of its appended tail."""
        sizes = {file.path: file.size_bytes for file in self.files}
        return sum(sizes[derived.source] for derived in self.derived_files)

    @model_validator(mode="after")
    def files_are_named_once_each(self) -> Self:
        reject_duplicates(file.path for file in self.files)
        return self

    @model_validator(mode="after")
    def derived_files_come_from_pinned_ones_and_shadow_none(self) -> Self:
        pinned = {file.path for file in self.files}
        for derived in self.derived_files:
            if derived.source not in pinned:
                raise ValueError(
                    f"{derived.path} derives from {derived.source}, which is not pinned"
                )
        reject_duplicates([*pinned, *(derived.path for derived in self.derived_files)])
        return self

    @model_validator(mode="after")
    def a_licence_that_is_a_notice_has_exactly_one_source(self) -> Self:
        needs_notice = LICENCE_OBLIGATIONS[self.license].requires_holder_notice
        has_licence_file = any(is_licence_file(file.path) for file in self.files)
        needs_declared_source = needs_notice and not has_licence_file
        if needs_declared_source and self.copyright_source is None:
            raise ValueError(
                f"{self.license} has no pinned licence file, so copyright_source "
                "must record where its declared holder line was read"
            )
        if not needs_declared_source and self.copyright_source is not None:
            raise ValueError(
                "copyright_source must be null unless a notice licence has no "
                "pinned licence file"
            )
        return self


class SupportEditorial(ArtifactEditorial):
    """The shared English aligner; never a speaking Voice Model."""

    kind: Literal["forced-aligner"]
    license: Literal["MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "CC0-1.0"]
    ram_class_gb: float = Field(gt=0, allow_inf_nan=False)


class SupportModel(SupportEditorial, PinnedArtifact):
    """A curated wav2vec2 Base ONNX graph with its exact text/audio interface."""

    @model_validator(mode="after")
    def the_alignment_interface_is_pinned(self) -> Self:
        required = {"onnx/model.onnx", "vocab.json", "preprocessor_config.json"}
        if not required.issubset(file.path for file in self.files):
            raise ValueError(
                "the aligner's graph, vocabulary and preprocessor must be pinned"
            )
        return self


class EntryEditorial(ArtifactEditorial):
    """The editorial half of a Catalog entry: every field a curator authors
    by hand, with the constraints that make it publishable."""

    default_tag: bool
    tier: Tier
    # The Architecture the entry runs on: an id `loading/registry.py` names
    # (ADR 0014). The parser cannot check it resolves — `catalog/` imports
    # nothing from `loading/` — so `readily-curate --write` and Engine boot
    # refuse one the registry does not know. Off the wire, like the runtime
    # it implies (CONTEXT.md).
    architecture: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    word_timing_languages: tuple[
        Annotated[str, Field(pattern=r"^[a-z]{2,3}$")], ...
    ] = ()
    word_timing: str = "off"
    # Whether an Architecture that streams its decoder should, kept off the wire.
    # Qwen's streaming decoder starts cold at every Block until upstream
    # fixes reference priming; the ONNX Architectures ignore it.
    decode_mode: Literal["streaming", "non-streaming"] = "streaming"
    # The knobs this entry pins for its Architecture, which is what says which
    # names and values are valid: `loading/parameters.py` checks them at
    # `readily-curate --write` and Engine boot, since the parser cannot.
    generation_parameters: Parameters = Field(default_factory=dict)
    parameters: dict[str, Control] = Field(default_factory=dict)
    excluded_parameters: dict[str, str] = Field(default_factory=dict)
    ram_class_gb: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def declared_parameters_are_inputs_this_entry_sets(self) -> Self:
        settable = self.generation_parameters.keys() | {"decode_mode", "word_timing"}
        if unsupported := self.parameters.keys() - settable:
            raise ValueError(
                f"parameters are not set by this entry: {sorted(unsupported)}"
            )
        if self.parameters.keys() & self.excluded_parameters.keys():
            raise ValueError("a parameter cannot be both declared and excluded")
        timing = self.parameters.get("word_timing")
        if timing is not None and (
            not isinstance(timing, ChoiceControl) or "off" not in timing.choices
        ):
            raise ValueError("word_timing must choose off or a Support Model")
        modes = self.parameters.get("decode_mode")
        if modes is not None:
            known = set(get_args(type(self).model_fields["decode_mode"].annotation))
            if not isinstance(modes, ChoiceControl) or set(modes.choices) - known:
                raise ValueError(f"decode_mode must choose among {sorted(known)}")
        return self


class CatalogEntry(EntryEditorial, PinnedArtifact):
    """One Voice Model in the Catalog, identified `name:tag`.

    Tags name variants, not versions (ADR 0003 §5): `version` increments on a
    weight bump, so History rows stay meaningful and Segment-cache keys never
    let cached audio cross a change in the weights that made it.
    """

    voices: list[Voice] = Field(min_length=1)
    default_voice: str
    tunables: Tunables

    def voice(self, voice_id: str) -> Voice:
        """The Voice this entry offers under `voice_id`."""
        for voice in self.voices:
            if voice.id == voice_id:
                return voice
        raise ValueError(f"{self.id} offers no voice {voice_id!r}")

    def reports_word_times(self, language: str) -> bool:
        """Whether synthesis supplies word coordinates in this Voice's language."""
        return language.split("-")[0].lower() in self.word_timing_languages

    def timing_choice(self, voice_id: str) -> str:
        """The Support Model this Voice's words come from, or `off` when
        synthesis supplies them itself. Frozen into the Generation Record."""
        if self.reports_word_times(self.voice(voice_id).language):
            return "off"
        return self.word_timing

    @property
    def control_schema(self) -> dict[str, Control]:
        return {**READILY_CONTROLS, **self.parameters}

    @property
    def control_values(self) -> Overrides:
        """This entry's own value for every control it sets itself."""
        own = {
            "decode_mode": self.decode_mode,
            "word_timing": self.word_timing,
            **self.generation_parameters,
            **self.tunables.model_dump(),
        }
        return {name: own[name] for name in self.control_schema if name in own}

    def compose(self, overrides: Overrides) -> "Effective":
        """Validate overrides and resolve all inputs before a Narration is planned."""
        schema = self.control_schema
        for name, value in overrides.items():
            declaration = schema.get(name)
            if declaration is None:
                raise ValueError(f"undeclared control: {name}")
            declaration.validate_value(value)
        return Effective(
            entry=self.model_copy(
                update={
                    "decode_mode": overrides.get("decode_mode", self.decode_mode),
                    "word_timing": overrides.get("word_timing", self.word_timing),
                    "generation_parameters": self._knobs(overrides),
                    "tunables": Tunables.model_validate(
                        self.tunables.model_dump() | _taken(overrides, Tunables)
                    ),
                }
            ),
            seed=overrides.get("seed"),
            prepare_first=bool(overrides.get("prepare_first", True)),
        )

    def _knobs(self, overrides: Overrides) -> Parameters:
        # A cleared knob is left out, as if the entry never pinned it. The
        # webview's JSON sends `1.0` as `1`, so a real knob's whole number is
        # made a float again: the Segment key hashes the value as written.
        knobs = dict(self.generation_parameters)
        for name in knobs.keys() & overrides.keys():
            value = overrides[name]
            if value is None:
                del knobs[name]
            elif isinstance(value, int) and self.parameters[name].kind == "number":
                knobs[name] = float(value)
            else:
                knobs[name] = value
        return knobs

    @model_validator(mode="after")
    def every_control_default_lies_inside_its_declaration(self) -> Self:
        schema = self.control_schema
        for name, value in self.control_values.items():
            schema[name].validate_value(value)
        return self

    @model_validator(mode="after")
    def voices_are_named_once_each(self) -> Self:
        reject_duplicates(voice.id for voice in self.voices)
        return self

    @model_validator(mode="after")
    def the_default_voice_is_one_this_entry_offers(self) -> Self:
        if self.default_voice not in {voice.id for voice in self.voices}:
            raise ValueError(f"default_voice {self.default_voice!r} is not offered")
        return self


def _taken(overrides: Overrides, model: type[BaseModel]) -> Overrides:
    return {name: overrides[name] for name in model.model_fields if name in overrides}


@dataclass(frozen=True)
class Effective:
    """One Catalog entry with a Voice's overrides composed over its defaults."""

    entry: CatalogEntry
    seed: int | None
    prepare_first: bool

    @property
    def values(self) -> Overrides:
        return {
            **self.entry.control_values,
            "seed": self.seed,
            "prepare_first": self.prepare_first,
        }


class Manifest(Strict):
    """The parsed Catalog Manifest."""

    # An Engine that guessed at an unknown shape would be guessing about
    # hashes. A future remote+signed catalog is an addition, and its readers
    # are the ones that widen this (ADR 0003 §1).
    schema_version: Literal[SCHEMA_VERSION]
    # A Catalog reference naming what a fresh install narrates with. Explicit
    # rather than "the first entry", so a curation PR that reorders the list
    # cannot silently change the default.
    default_model: str
    # A Catalog reference naming the instant-Tier entry the shell offers as
    # the way out of a wait on an expressive Narration. Named for the same
    # reason `default_model` is; `None` when the Catalog carries nothing to
    # escape to.
    default_fast_model: str | None = None
    models: list[CatalogEntry] = Field(min_length=1)
    support_models: list[SupportModel] = Field(default_factory=list, max_length=1)

    def required_support(self, choices: Iterable[str]) -> tuple[SupportModel, ...]:
        """The curated Support Models behind a set of timing choices."""
        chosen = set(choices)
        return tuple(support for support in self.support_models if support.id in chosen)

    def required_artifacts(
        self, entry: CatalogEntry, choices: Iterable[str]
    ) -> tuple[PinnedArtifact, ...]:
        """Everything that must be installed to run `entry` under these
        timing choices, in download order. Readiness, download planning and
        the download itself all read this one answer."""
        return (*self.required_support(choices), entry)

    @model_validator(mode="after")
    def timing_choices_name_curated_support(self) -> Self:
        allowed = {"off", *(support.id for support in self.support_models)}
        for entry in self.models:
            declaration = entry.parameters.get("word_timing")
            if entry.word_timing not in allowed or (
                isinstance(declaration, ChoiceControl)
                and set(declaration.choices) - allowed
            ):
                raise ValueError("word_timing must name a curated Support Model")
        return self

    @property
    def artifacts(self) -> tuple[PinnedArtifact, ...]:
        return (*self.models, *self.support_models)

    @model_validator(mode="after")
    def entries_are_identified_once_each(self) -> Self:
        reject_duplicates(entry.id for entry in self.artifacts)
        return self

    @model_validator(mode="after")
    def every_name_has_exactly_one_default_tag(self) -> Self:
        # A bare name must always resolve (ADR 0003 §5), and must resolve to
        # one thing.
        defaults: dict[str, list[str]] = {}
        for entry in self.models:
            defaults.setdefault(entry.name, [])
            if entry.default_tag:
                defaults[entry.name].append(entry.tag)
        for name, tags in defaults.items():
            if len(tags) != 1:
                raise ValueError(
                    f"{name!r} has {len(tags)} default tags; exactly one is required"
                )
        return self

    def find(self, entry_id: str) -> CatalogEntry | None:
        """Look an entry up by its full `name:tag` identity."""
        return next((e for e in self.models if e.id == entry_id), None)

    def resolve(self, reference: str) -> CatalogEntry | None:
        """Resolve a Catalog reference: `name:tag` names one entry, and a
        bare `name` resolves to that name's default tag (ADR 0003 §5)."""
        if ":" in reference:
            return self.find(reference)
        return next(
            (e for e in self.models if e.name == reference and e.default_tag), None
        )

    @model_validator(mode="after")
    def the_default_model_resolves(self) -> Self:
        if self.resolve(self.default_model) is None:
            raise ValueError(
                f"default_model {self.default_model!r} does not resolve to an entry"
            )
        return self

    @property
    def default_entry(self) -> CatalogEntry:
        """What a fresh install narrates with."""
        entry = self.resolve(self.default_model)
        if entry is None:  # unreachable: validated at parse time
            raise LookupError(self.default_model)
        return entry

    @model_validator(mode="after")
    def the_default_fast_model_resolves(self) -> Self:
        if self.default_fast_model is not None and (
            self.resolve(self.default_fast_model) is None
        ):
            raise ValueError(
                f"default_fast_model {self.default_fast_model!r} does not resolve "
                "to an entry"
            )
        return self

    @property
    def default_fast_entry(self) -> CatalogEntry | None:
        """The Voice Model offered as the escape from a wait, if any."""
        return (
            None
            if self.default_fast_model is None
            else self.resolve(self.default_fast_model)
        )


def reject_duplicates(values: Iterable[str]) -> None:
    """Refuse two values that name the same thing — including two that only
    case-folding tells apart. Public because `curation/` applies the same
    rule to a hand-written draft, before an entry exists to validate it.

    Case-folded, because these values become file paths: a Voice's id spells
    its preview clip and capture directory, and a manifest file path lands in
    the model directory. On APFS's default case-insensitive volumes,
    `Chelsie` and `chelsie` are one file, silently overwriting each other.
    """
    seen: dict[str, str] = {}
    for value in values:
        folded = value.casefold()
        if folded in seen:
            detail = (
                "appears more than once"
                if value == seen[folded]
                else f"collides with {seen[folded]!r} on a case-insensitive filesystem"
            )
            raise ValueError(f"{value!r} {detail}")
        seen[folded] = value


def load_manifest(path: Path = MANIFEST_PATH) -> Manifest:
    """Read and validate the Catalog Manifest.

    Raises rather than degrading: a release whose baked manifest does not
    parse has no trustworthy answer to "what may I download", and CI calls
    this on the committed file precisely so that never ships.
    """
    return Manifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
