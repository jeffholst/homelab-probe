"""Errors of the API: a code and a fixed sentence, never the text of an exception.

The text of a controller error holds its address and sometimes a fragment of a response, so an answer built from it
could leak both to whoever asks. ``UniFiAPIError.kind`` says what happened; each kind has one sentence.
"""

from typing import Any, Dict, Optional, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse

from ..client import UniFiAPIError

# kind -> (HTTP status, error code, message)
CONTROLLER_ERRORS: Dict[Optional[str], Tuple[int, str, str]] = {
    "site": (404, "site_not_found", "The controller has no such site."),
    "unauthorized": (502, "controller_unauthorized", "The controller rejected the API key."),
    "forbidden": (502, "controller_forbidden", "The API key is not allowed to read this from the controller."),
    "timeout": (504, "controller_timeout", "The controller did not answer in time."),
    "tls": (502, "controller_tls", "The controller's certificate could not be verified."),
    "connection": (502, "controller_unreachable", "The controller could not be reached."),
}
OTHER_CONTROLLER_ERROR = (502, "controller_error", "The controller's answer could not be used.")


ERROR_SCHEMA = {"type": "object", "required": ["error", "message"],
                "properties": {"error": {"type": "string"}, "message": {"type": "string"}}}
_STATUS_TEXT = {401: "Not logged in", 403: "Not allowed", 404: "No such site, snapshot or user",
                409: "The request conflicts with the current state", 422: "A parameter is not valid",
                500: "The local files cannot be used", 502: "The controller could not be read",
                504: "The controller timed out"}


def error_responses(*codes: int) -> Dict[int | str, Dict[str, Any]]:
    """The error responses of a route, for the OpenAPI document."""
    return {code: {"description": _STATUS_TEXT[code], "content": {"application/json": {"schema": ERROR_SCHEMA}}}
            for code in codes}


class ApiError(Exception):
    """An error with its status, code and (fixed or caller-checked) message, and optional extra fields."""

    def __init__(self, status: int, code: str, message: str, headers: Optional[Dict[str, str]] = None,
                 **extra: Any) -> None:
        super().__init__(code)
        self.status, self.code, self.message, self.extra = status, code, message, extra
        self.headers = headers or {}


def from_controller(error: UniFiAPIError) -> ApiError:
    """The API error for a failed read: chosen by ``error.kind``, with a fixed message."""
    status, code, message = CONTROLLER_ERRORS.get(error.kind, OTHER_CONTROLLER_ERROR)
    return ApiError(status, code, message)


async def api_error_handler(request: Request, error: ApiError) -> JSONResponse:
    return JSONResponse({"error": error.code, "message": error.message, **error.extra}, status_code=error.status,
                        headers=error.headers)


async def request_validation_error_handler(request: Request, error: Exception) -> JSONResponse:
    return JSONResponse({"error": "invalid_parameter", "message": "A parameter is not valid."}, status_code=422)
