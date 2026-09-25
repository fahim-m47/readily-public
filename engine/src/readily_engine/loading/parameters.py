"""The cross-check between an entry's knobs and its Architecture's schema.

`readily-curate --write` and Engine boot call `check_parameters` once they
know the entry's Architecture (ADR 0014): the Manifest parser cannot, since
`catalog/` imports nothing from `loading/`. A declared control must be
contained by the schema field it drives — its kind, hard range and
nullability — so no value the webview can send reaches `generate` only to be
refused on a Narration. `model_validator` rules on a schema are outside this
contract."""

import json
from types import NoneType
from typing import get_args

from annotated_types import Ge, Gt, Le, Lt
from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo

from readily_engine.catalog import EntryEditorial
from readily_engine.catalog.controls import Control, NumberControl, Range

# Record fields Readily reads itself, never an Architecture's knobs (ADR 0014 §5).
_READILY_SIDE = {"decode_mode", "word_timing"}

_KINDS = {"integer": int, "number": float}


class ParameterMismatch(ValueError):
    """An entry sets a knob its Architecture does not read or would refuse."""


def check_parameters(schema: type[BaseModel], entry: EntryEditorial) -> None:
    """Refuse an entry whose pinned or declared knobs `schema` would not take."""
    label = f"{entry.name}:{entry.tag}"
    fields = schema.model_fields
    knobs = (entry.generation_parameters.keys() | entry.parameters.keys()) - (
        _READILY_SIDE
    )
    if unread := sorted(knobs - fields.keys()):
        raise ParameterMismatch(f"{label}: its Architecture reads no {unread}")
    try:
        parsed = schema.model_validate(entry.generation_parameters)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(map(str, detail['loc']))}: {detail['msg']}"
            for detail in error.errors()
        )
        raise ParameterMismatch(f"{label}: generation_parameters: {problems}") from None
    # The Record hashes the pinned mapping as written, so it must already be
    # the form the schema hands the model: `1` for a real knob would not be.
    if _canonical(parsed.model_dump(exclude_none=True)) != _canonical(
        entry.generation_parameters
    ):
        raise ParameterMismatch(
            f"{label}: generation_parameters must be written as the schema "
            f"serializes them: {parsed.model_dump(exclude_none=True)}"
        )
    for name in sorted(entry.parameters.keys() - _READILY_SIDE):
        if problem := _uncontained(entry.parameters[name], fields[name]):
            raise ParameterMismatch(f"{label}: control {name!r} {problem}")


def _canonical(values: object) -> str:
    return json.dumps(values, sort_keys=True)


def _uncontained(control: Control, field: FieldInfo) -> str | None:
    if not isinstance(control, NumberControl):
        return "must be a number control"
    types = set(get_args(field.annotation) or (field.annotation,))
    if types - {NoneType} != {_KINDS[control.kind]}:
        return f"of kind {control.kind} drives a {field.annotation} field"
    if control.nullable and (field.is_required() or NoneType not in types):
        return "may be cleared, but the knob is required"
    if not _within(control.hard_range, field):
        return f"admits {control.hard_range} beyond the schema's bounds"
    return None


def _within(bounds: Range, field: FieldInfo) -> bool:
    for rule in field.metadata:
        match rule:
            case Ge(ge=floor) if bounds.min < floor:
                return False
            case Gt(gt=floor) if bounds.min < floor or (
                bounds.min == floor and not bounds.min_exclusive
            ):
                return False
            case Le(le=ceiling) if bounds.max is None or bounds.max > ceiling:
                return False
            case Lt(lt=ceiling) if bounds.max is None or bounds.max >= ceiling:
                return False
    return True
