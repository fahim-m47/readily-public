"""What a curator writes by hand, and the refusal curation answers with.

A Draft is a Catalog entry minus the two things nobody should type: the
per-file SHA-256s and the Voice Preview paths. Everything else is editorial
— the Tier a model belongs in, the licence it ships under, the RAM class the
sheet warns on, the tunables the chunker reads — and stays a human judgement
in a reviewed PR (ADR 0003 §2). Curation computes the rest and hands back a
`CatalogEntry`, so the manifest only ever gains entries that parsed.
"""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Self

from pydantic import Field, field_validator, model_validator

from readily_engine.catalog import (
    REFERENCE_ROOT,
    CatalogEntry,
    EntryEditorial,
    ManifestFile,
    Tunables,
    VoiceIdentity,
    model_file_path,
)
from readily_engine.catalog.licence_table import (
    LICENCE_FILE_NAMES,
    LICENCE_OBLIGATIONS,
    is_licence_file,
)
from readily_engine.catalog.manifest import (
    SupportEditorial,
    SupportModel,
    UnpinnedVoiceReference,
    reject_duplicates,
)
from readily_engine.store import file_sha256

# The files curation finds for itself rather than a curator naming: every
# spelling of upstream's licence and notice files (ADR 0009 §3). A draft
# that named one would pin a copy that outlives the revision it came from,
# since pinning only fetches what upstream still ships under these names.
_CURATIONS_OWN = frozenset(name.casefold() for name in LICENCE_FILE_NAMES)


class CurationError(RuntimeError):
    """Curation refused to emit an entry.

    The one exception every stage raises, because every stage is answering
    the same question — is this model fit to ship? — and a curator who sees
    it has one thing to do: fix the model or fix the draft, never pass a
    flag that makes the check quieter.
    """


class DraftVoice(VoiceIdentity):
    """A curator names the reference clip; curation computes its digest."""

    reference: UnpinnedVoiceReference | None = None


class Draft(EntryEditorial):
    """One Voice Model as a curator describes it, before curation runs.

    The editorial fields and their constraints come from `EntryEditorial` —
    the same base `CatalogEntry` parses with, so a draft that would not be
    publishable is refused here, before a byte of the model downloads. What
    differs is exactly what nobody should type: `files` names paths only
    (the hashes do not exist yet, and inventing placeholders for them is
    the hand-pinning this script replaces), and each voice is a
    `DraftVoice` with no `preview` field or reference digest — a draft carrying one
    is refused, because the clip it names was never generated.
    """

    files: list[str] = Field(min_length=1)
    voices: list[DraftVoice] = Field(min_length=1)
    default_voice: str
    tunables: Tunables

    @field_validator("files")
    @classmethod
    def files_name_exact_paths_inside_the_model_directory(
        cls, value: list[str]
    ) -> list[str]:
        # The same rules `CatalogEntry` applies to its files, one step
        # earlier and for the same reasons: these strings reach
        # huggingface_hub as allow-patterns and the staging directory as
        # join targets before any hash exists to pin them.
        reject_duplicates(value)
        for path in value:
            if path.casefold() in _CURATIONS_OWN:
                raise ValueError(f"{path} is found by curation, not named in a draft")
        return [model_file_path(path) for path in value]

    @model_validator(mode="after")
    def voices_are_named_once_each(self) -> Self:
        reject_duplicates(voice.id for voice in self.voices)
        return self

    @model_validator(mode="after")
    def the_default_voice_is_one_this_draft_offers(self) -> Self:
        if self.default_voice not in {voice.id for voice in self.voices}:
            raise ValueError(f"default_voice {self.default_voice!r} is not offered")
        return self

    @model_validator(mode="after")
    def a_copyright_source_belongs_only_to_a_notice_licence(self) -> Self:
        # The half of `CatalogEntry`'s exactly-one-source rule a draft can
        # settle before pinning: whether upstream ships a licence file is
        # only known after the download, but a licence that carries no
        # holder line never wants a source.
        needs_notice = LICENCE_OBLIGATIONS[self.license].requires_holder_notice
        if self.copyright_source is not None and not needs_notice:
            raise ValueError(
                f"{self.license} carries no holder line, so copyright_source "
                "must be null"
            )
        return self

    @classmethod
    def from_entry(cls, entry: CatalogEntry) -> Self:
        """The draft a committed entry came from — how re-curation reads its
        input, so regenerating a shipped entry needs no second source of
        truth to drift out of step with the manifest. The licence and
        notice files the entry pins are curation's to find again, so they
        are not read back as files the draft names."""
        return cls.model_validate(
            {
                **entry.model_dump(mode="json"),
                "files": [
                    file.path
                    for file in entry.files
                    if file.path.casefold() not in _CURATIONS_OWN
                ],
                "voices": [
                    voice.model_dump(
                        mode="json",
                        exclude={
                            "preview": True,
                            "qualification": True,
                            "reference": {"sha256"},
                        },
                    )
                    for voice in entry.voices
                ],
            }
        )

    def entry(
        self,
        files: Sequence[ManifestFile],
        previews: Mapping[str, str],
        *,
        references_root: Path = REFERENCE_ROOT,
    ) -> CatalogEntry:
        """This draft plus what curation computed, parsed as a real entry.

        A Voice with no clip in `previews` keeps a `null` preview rather
        than a guessed path, which is the state the Catalog sheet already
        knows how to render.
        """
        return CatalogEntry.model_validate(
            {
                **self.model_dump(mode="json"),
                # A pinned upstream licence file is itself the provenance;
                # the reviewed URL stands in only when pinning found none.
                "copyright_source": (
                    None
                    if any(is_licence_file(file.path) for file in files)
                    else self.copyright_source
                ),
                "files": [file.model_dump(mode="json") for file in files],
                "voices": [
                    {
                        **voice.model_dump(mode="json"),
                        "preview": previews.get(voice.id),
                        "reference": (
                            {
                                **voice.reference.model_dump(mode="json"),
                                "sha256": file_sha256(
                                    references_root / voice.reference.clip
                                ),
                            }
                            if voice.reference is not None
                            else None
                        ),
                    }
                    for voice in self.voices
                ],
            }
        )


class SupportDraft(SupportEditorial):
    """Exact upstream files for the shared aligner, before curation hashes them."""

    files: list[str] = Field(min_length=1)

    @field_validator("files")
    @classmethod
    def files_name_exact_paths(cls, value: list[str]) -> list[str]:
        return Draft.files_name_exact_paths_inside_the_model_directory(value)

    @classmethod
    def from_entry(cls, entry: SupportModel) -> Self:
        return cls.model_validate(
            {
                **entry.model_dump(mode="json"),
                "files": [
                    file.path
                    for file in entry.files
                    if file.path.casefold() not in _CURATIONS_OWN
                ],
            }
        )

    def entry(self, files: Sequence[ManifestFile]) -> SupportModel:
        return SupportModel.model_validate(
            {
                **self.model_dump(mode="json"),
                "files": [file.model_dump(mode="json") for file in files],
                "copyright_source": None
                if any(is_licence_file(file.path) for file in files)
                else self.copyright_source,
            }
        )
