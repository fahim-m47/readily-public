"""Control declarations shared by curation, override validation and the picker."""

import math
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

type ControlValue = StrictBool | StrictInt | StrictFloat | StrictStr | None
type Overrides = dict[str, ControlValue]


class Declaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    label: str = Field(min_length=1)
    unit: str
    description: str = Field(min_length=1)


class Range(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    # `int | float` rather than `float`, so an integer control's bounds
    # serialize as `1`, not `1.0`, in the manifest and on the wire.
    min: int | float
    max: int | float | None
    min_exclusive: bool = Field(default=False, serialization_alias="minExclusive")

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.max is not None and self.min > self.max:
            raise ValueError("range minimum must not exceed maximum")
        return self

    def contains(self, value: float) -> bool:
        above_min = self.min < value if self.min_exclusive else self.min <= value
        return above_min and (self.max is None or value <= self.max)


class NumberControl(Declaration):
    kind: Literal["integer", "number"]
    hard_range: Range = Field(serialization_alias="hardRange")
    recommended_range: Range | None = Field(
        default=None, serialization_alias="recommendedRange"
    )
    nullable: bool = False

    def validate_value(self, value: ControlValue) -> None:
        if value is None and self.nullable:
            return
        if (
            type(value) not in (int, float)
            or (self.kind == "integer" and type(value) is not int)
            or (type(value) is float and not math.isfinite(value))
            or not self.hard_range.contains(value)
        ):
            raise ValueError("value is outside the control's hard range or type")

    @model_validator(mode="after")
    def the_recommendation_lies_inside_the_hard_range(self) -> Self:
        recommended = self.recommended_range
        if recommended is not None and (
            not self.hard_range.contains(recommended.min)
            or (recommended.max is None and self.hard_range.max is not None)
            or (
                recommended.max is not None
                and not self.hard_range.contains(recommended.max)
            )
        ):
            raise ValueError("recommended range must lie inside the hard range")
        return self


class ChoiceControl(Declaration):
    kind: Literal["choice"] = "choice"
    choices: list[str] = Field(min_length=1)

    def validate_value(self, value: ControlValue) -> None:
        if type(value) is not str or value not in self.choices:
            raise ValueError("value must be one of the declared choices")

    @model_validator(mode="after")
    def choices_are_named_once_each(self) -> Self:
        if len(set(self.choices)) != len(self.choices):
            raise ValueError("choices must be unique")
        return self


class BooleanControl(Declaration):
    kind: Literal["boolean"] = "boolean"

    def validate_value(self, value: ControlValue) -> None:
        if type(value) is not bool:
            raise ValueError("value must be a boolean")


type Control = Annotated[
    NumberControl | ChoiceControl | BooleanControl, Field(discriminator="kind")
]


def _readily_controls() -> dict[str, Control]:
    controls: dict[str, Control] = {
        name: NumberControl(
            label=label,
            kind="integer",
            unit=unit,
            hard_range=Range(min=minimum, max=maximum),
            description=description,
        )
        for name, label, unit, minimum, maximum, description in (
            (
                "chunk_budget_chars",
                "Characters per Block",
                "characters",
                1,
                4096,
                "Text narrated in one pass. "
                "Shorter Blocks start sooner, longer ones flow better.",
            ),
            (
                "first_block_chars",
                "First Block characters",
                "characters",
                1,
                4096,
                "A shorter first Block starts playback sooner.",
            ),
            (
                "pause_sentence_ms",
                "Sentence pause",
                "ms",
                0,
                60000,
                "Silence between sentences.",
            ),
            (
                "pause_paragraph_break_ms",
                "Paragraph pause",
                "ms",
                0,
                60000,
                "Silence between paragraphs.",
            ),
        )
    }
    controls["seed"] = NumberControl(
        label="Seed",
        kind="integer",
        unit="",
        hard_range=Range(min=0, max=2**32 - 1),
        nullable=True,
        description=(
            "Automatic rolls a fresh seed. Set one to reproduce a Narration exactly."
        ),
    )
    controls["prepare_first"] = BooleanControl(
        label="Prepare the whole Narration first",
        unit="",
        description=(
            "Generate every Block before playback starts, so it never waits "
            "on generation and the player knows its length from the first "
            "word. Off, it starts reading as soon as the first Block is ready."
        ),
    )
    return controls


READILY_CONTROLS: dict[str, Control] = _readily_controls()
