"""An entry's knobs are ones its Architecture reads, at values it accepts.

The Manifest parser cannot see an Architecture (`catalog/` imports nothing
from `loading/`), so `check_parameters` is where `readily-curate --write` and
Engine boot cross-check an entry's pinned knobs and declared controls against
the schema its Architecture declares (ADR 0014)."""

from typing import Any

import pytest
from conftest import make_entry
from pydantic import BaseModel, ConfigDict, Field

from readily_engine.loading.parameters import check_parameters


class Sampling(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    temperature: float | None = Field(default=None, ge=0)
    top_k: int | None = Field(default=None, ge=0, strict=True)
    top_p: float | None = Field(default=None, gt=0, le=1)


class Diffusion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    steps: int = Field(gt=0, strict=True)


def number(
    kind: str = "number",
    low: float = 0,
    high: float | None = None,
    *,
    exclusive: bool = False,
    nullable: bool = False,
) -> dict[str, Any]:
    return {
        "label": "Knob",
        "unit": "",
        "description": "A knob.",
        "kind": kind,
        "hard_range": {"min": low, "max": high, "min_exclusive": exclusive},
        "nullable": nullable,
    }


def entry(pinned: dict[str, int | float], **controls: dict[str, Any]):
    return make_entry(generation_parameters=pinned, parameters=controls)


def test_knobs_the_schema_reads_at_values_it_accepts_pass():
    check_parameters(
        Sampling,
        entry(
            {"temperature": 0.9, "top_k": 50, "top_p": 1.0},
            temperature=number(),
            top_k=number("integer"),
            top_p=number(low=0, high=1, exclusive=True),
            decode_mode={
                "label": "Decode mode",
                "unit": "",
                "description": "How it decodes.",
                "kind": "choice",
                "choices": ["streaming", "non-streaming"],
            },
            word_timing={
                "label": "Word timing",
                "unit": "",
                "description": "Where word times come from.",
                "kind": "choice",
                "choices": ["off", "wav2vec2:base-960h"],
            },
        ),
    )


def test_a_pinned_knob_the_architecture_does_not_read_is_refused():
    with pytest.raises(ValueError, match="'steps'"):
        check_parameters(Sampling, entry({"steps": 8}))


def test_an_entry_leaving_out_a_required_knob_is_refused():
    with pytest.raises(ValueError, match="steps"):
        check_parameters(Diffusion, entry({}))


def test_a_pinned_value_outside_the_schema_is_refused():
    with pytest.raises(ValueError, match="top_p"):
        check_parameters(Sampling, entry({"top_p": 1.5}))


def test_a_whole_number_pinned_for_a_real_knob_is_refused():
    # 1 and 1.0 are different bytes in the Generation Record, so an entry
    # pins the value in the form the schema would hand the model.
    with pytest.raises(ValueError, match="temperature"):
        check_parameters(Sampling, entry({"temperature": 1}))


def test_a_control_admitting_values_the_schema_refuses_is_refused():
    with pytest.raises(ValueError, match="top_p"):
        check_parameters(Sampling, entry({"top_p": 0.9}, top_p=number(low=0, high=1)))


def test_an_unbounded_control_over_a_bounded_knob_is_refused():
    with pytest.raises(ValueError, match="top_p"):
        check_parameters(
            Sampling, entry({"top_p": 0.9}, top_p=number(low=0.5, high=None))
        )


def test_a_control_of_the_wrong_kind_is_refused():
    with pytest.raises(ValueError, match="top_k"):
        check_parameters(Sampling, entry({"top_k": 50}, top_k=number("number")))


def test_a_nullable_control_over_a_required_knob_is_refused():
    # Clearing the control would leave the Record without the knob.
    with pytest.raises(ValueError, match="steps"):
        check_parameters(
            Diffusion,
            entry({"steps": 8}, steps=number("integer", low=1, nullable=True)),
        )
