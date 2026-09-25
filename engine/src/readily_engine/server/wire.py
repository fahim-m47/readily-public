"""Frozen v1 HTTP and SSE shapes shared by routes and middleware."""

import json
from enum import StrEnum

from fastapi.responses import JSONResponse

from readily_engine.wire import WIRE_VERSION, WireError, wire_error

__all__ = [
    "ERROR_MESSAGES",
    "WIRE_VERSION",
    "ErrorCode",
    "error_body",
    "error_bytes",
    "error_response",
    "sse_event",
]


class ErrorCode(StrEnum):
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN_ORIGIN = "forbidden_origin"
    INVALID_REQUEST = "invalid_request"
    RECIPE_NOT_QUALIFIED = "recipe_not_qualified"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    UNKNOWN_MODEL = "unknown_model"
    MODEL_NOT_INSTALLED = "model_not_installed"
    DOWNLOAD_IN_PROGRESS = "download_in_progress"
    ENGINE_UNAVAILABLE = "engine_unavailable"
    NARRATION_NOT_RESUMABLE = "narration_not_resumable"
    EXPORT_IN_PROGRESS = "export_in_progress"
    INVALID_DESTINATION = "invalid_destination"
    INTERNAL_ERROR = "internal_error"


ERROR_MESSAGES = {
    ErrorCode.RECIPE_NOT_QUALIFIED: (
        "This recipe is not qualified for Simple mode. "
        "Choose a qualified Voice or use Advanced."
    ),
    ErrorCode.UNAUTHORIZED: "A valid Engine bearer token is required.",
    ErrorCode.FORBIDDEN_ORIGIN: "This browser origin may not call the Engine.",
    ErrorCode.INVALID_REQUEST: (
        "The request body does not match the v1 speech contract."
    ),
    ErrorCode.NOT_FOUND: "The requested Engine route does not exist.",
    ErrorCode.METHOD_NOT_ALLOWED: (
        "The requested Engine route does not accept this method."
    ),
    ErrorCode.UNKNOWN_MODEL: "This Voice Model is not in the Catalog.",
    ErrorCode.MODEL_NOT_INSTALLED: "This Voice Model has not been downloaded.",
    ErrorCode.DOWNLOAD_IN_PROGRESS: "A model download is already in progress.",
    ErrorCode.ENGINE_UNAVAILABLE: "The Engine cannot start a Narration right now.",
    ErrorCode.NARRATION_NOT_RESUMABLE: (
        "This Narration is not interrupted or stopped."
    ),
    ErrorCode.EXPORT_IN_PROGRESS: "An Export is already in progress.",
    ErrorCode.INVALID_DESTINATION: (
        "The Engine will not write an Export to that location."
    ),
    ErrorCode.INTERNAL_ERROR: "The Engine could not complete the request.",
}


def error_body(code: ErrorCode) -> dict[str, WireError]:
    """Return the only error envelope exposed by the v1 API."""
    return {"error": wire_error(code.value, ERROR_MESSAGES[code])}


def error_bytes(code: ErrorCode) -> bytes:
    """Encode an error for pure-ASGI middleware without a Response object."""
    return json.dumps(error_body(code), separators=(",", ":")).encode()


def error_response(status_code: int, code: ErrorCode) -> JSONResponse:
    return JSONResponse(error_body(code), status_code=status_code)


def sse_event(event_id: int, data: dict[str, object], event: str = "narration") -> str:
    """Frame one v1 snapshot as an SSE event consumable by streamed fetch."""
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return f"event: {event}\nid: {event_id}\ndata: {payload}\n\n"
