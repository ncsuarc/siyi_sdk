"""Wire format shared by the camera service and :class:`RemoteCamera`.

Messages are JSON-RPC 2.0 over one websocket. Requests carry an ``id`` and get a
result or an error; notifications have no ``id`` and get nothing back. The ground
sends setpoints as notifications, and the service sends events the same way.

SDK values keep their types across the link: ``siyi_sdk.models`` dataclasses and
enums are tagged (``{"$type": "GimbalAttitude", ...}``, ``{"$enum": ...}``) so
the client rebuilds the same objects a local :class:`SIYIClient` returns. Plain
JSON (untagged dicts and numbers) is accepted for arguments too, so clients in
other languages need not produce tags.

SDK exceptions are sent with their class name and fields and raised again on the
client, so ``except TimeoutError`` works the same remotely as locally.
"""

from __future__ import annotations

import base64
import dataclasses
import json
from enum import Enum, IntEnum
from typing import Any, Final

from siyi_sdk import exceptions, models

PROTOCOL_VERSION: Final = 1

# Ground -> service notifications.
SETPOINT_METHODS: Final = frozenset({"rate", "zoom_rate"})

# Service -> ground notifications.
EVENT_ATTITUDE: Final = "attitude"
EVENT_FEEDBACK: Final = "feedback"
EVENT_LINK: Final = "link"
EVENT_LOCK: Final = "lock_status"
EVENT_ZOOM: Final = "zoom"
EVENT_CONTROL: Final = "control"

# JSON-RPC 2.0 error codes; ``data.kind`` says which SDK condition it was.
PARSE_ERROR: Final = -32700
INVALID_REQUEST: Final = -32600
METHOD_NOT_FOUND: Final = -32601
INVALID_PARAMS: Final = -32602
INTERNAL_ERROR: Final = -32603
SERVER_ERROR: Final = -32000


# =============================================================================
# Service exceptions
# =============================================================================


class RemoteError(exceptions.SIYIError):
    """The camera service refused or failed a request for a reason of its own."""

    kind = "internal"


class UnknownMethodError(RemoteError):
    """The service has no such method (or does not expose it)."""

    kind = "unknown_method"


class NotControllerError(RemoteError):
    """Another ground client has control; this one may only observe."""

    kind = "not_controller"


class ServiceUnavailableError(RemoteError):
    """The service can't do that right now, e.g. a lock without live video."""

    kind = "unavailable"


class ConflictError(RemoteError):
    """Something else is in progress, or the request contradicts the stored setup."""

    kind = "conflict"


class OperationFailedError(RemoteError):
    """The operation ran but did not succeed, e.g. a loop measurement that didn't fit."""

    kind = "failed"


# Exceptions rebuilt on the client by class name. Anything else arrives as RemoteError.
_EXCEPTIONS: Final[dict[str, type[BaseException]]] = {
    cls.__name__: cls
    for cls in (
        exceptions.ConfigurationError,
        exceptions.ConnectionError,
        exceptions.TimeoutError,
        exceptions.SendError,
        exceptions.NotConnectedError,
        exceptions.NACKError,
        exceptions.UnsupportedCommandError,
        exceptions.ResponseError,
        exceptions.MalformedPayloadError,
        UnknownMethodError,
        NotControllerError,
        ServiceUnavailableError,
        ConflictError,
        OperationFailedError,
        RemoteError,
    )
}


def error_kind(exc: BaseException) -> str:
    """Classify an exception for ``error.data.kind``."""
    if isinstance(exc, RemoteError):
        return exc.kind
    if isinstance(exc, (exceptions.ConfigurationError, ValueError)):
        return "bad_args"
    if isinstance(exc, exceptions.UnsupportedCommandError):
        return "unsupported"
    if isinstance(exc, exceptions.TimeoutError):
        return "camera_timeout"
    if isinstance(exc, (exceptions.NotConnectedError, exceptions.ConnectionError)):
        return "camera_offline"
    if isinstance(exc, (exceptions.NACKError, exceptions.ResponseError)):
        return "rejected"
    return "internal"


def _json_code(kind: str) -> int:
    return {
        "bad_args": INVALID_PARAMS,
        "unknown_method": METHOD_NOT_FOUND,
        "internal": INTERNAL_ERROR,
    }.get(kind, SERVER_ERROR)


def _is_dataclass(obj: object) -> bool:
    # A plain bool: dataclasses.is_dataclass narrows exception types to nonsense.
    return hasattr(obj, "__dataclass_fields__")


def encode_error(exc: BaseException) -> dict[str, Any]:
    """JSON-RPC ``error`` object for an exception."""
    kind = error_kind(exc)
    data: dict[str, Any] = {"kind": kind, "type": type(exc).__name__}
    if _is_dataclass(exc):
        data["fields"] = {name: getattr(exc, name) for name in exc.__dataclass_fields__}  # type: ignore[attr-defined]
    message = str(exc) or type(exc).__name__
    if type(exc).__name__ not in _EXCEPTIONS:
        # ValueError from argument validation, or an unexpected failure.
        data["type"] = "ConfigurationError" if kind == "bad_args" else "RemoteError"
    return {"code": _json_code(kind), "message": message, "data": data}


def decode_error(error: dict[str, Any]) -> BaseException:
    """Rebuild the exception a service error describes."""
    data = error.get("data") or {}
    message = str(error.get("message", "Remote error"))
    cls = _EXCEPTIONS.get(str(data.get("type")), RemoteError)
    fields = data.get("fields")
    if _is_dataclass(cls) and isinstance(fields, dict):
        try:
            return cls(**fields)
        except TypeError:
            pass
    if _is_dataclass(cls):
        return RemoteError(message)
    return cls(message)


# =============================================================================
# Values
# =============================================================================


def encode_value(value: Any) -> Any:  # noqa: ANN401 - any JSON-able value
    """Turn SDK models, enums and bytes into tagged JSON values."""
    if isinstance(value, Enum):
        return {"$enum": type(value).__name__, "name": value.name, "value": value.value}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        body = {f.name: encode_value(getattr(value, f.name)) for f in dataclasses.fields(value)}
        return {"$type": type(value).__name__, **body}
    if isinstance(value, (bytes, bytearray)):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {str(k): encode_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode_value(v) for v in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _model(name: str) -> Any:  # noqa: ANN401
    # Only names defined in siyi_sdk.models can be rebuilt; nothing else is instantiated.
    cls = getattr(models, name, None)
    return cls if isinstance(cls, type) else None


def decode_value(value: Any) -> Any:  # noqa: ANN401 - any JSON value
    """Inverse of :func:`encode_value`; unknown tags are left as plain dicts."""
    if isinstance(value, list):
        return [decode_value(v) for v in value]
    if not isinstance(value, dict):
        return value
    if "$bytes" in value:
        return base64.b64decode(value["$bytes"])
    if "$enum" in value:
        cls = _model(str(value["$enum"]))
        if cls is not None and issubclass(cls, IntEnum):
            return cls(value["value"])
        return value.get("value")
    body = {k: decode_value(v) for k, v in value.items() if k != "$type"}
    if "$type" in value:
        cls = _model(str(value["$type"]))
        if cls is not None and dataclasses.is_dataclass(cls):
            names = {f.name for f in dataclasses.fields(cls)}
            try:
                return cls(**{k: v for k, v in body.items() if k in names})
            except TypeError:
                pass
    return body


# =============================================================================
# Messages
# =============================================================================


def request(msg_id: int, method: str, params: dict[str, Any] | None = None) -> str:
    """Serialize a request."""
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params:
        msg["params"] = encode_value(params)
    return json.dumps(msg)


def notification(method: str, params: dict[str, Any] | None = None) -> str:
    """Serialize a notification (no reply expected)."""
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = encode_value(params)
    return json.dumps(msg)


def result(msg_id: Any, value: Any) -> str:  # noqa: ANN401
    """Serialize a successful reply."""
    return json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": encode_value(value)})


def error(msg_id: Any, exc: BaseException) -> str:  # noqa: ANN401
    """Serialize an error reply."""
    return json.dumps({"jsonrpc": "2.0", "id": msg_id, "error": encode_error(exc)})


def raw_error(msg_id: Any, code: int, message: str, kind: str) -> str:  # noqa: ANN401
    """Serialize a protocol-level error (bad JSON, bad envelope)."""
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {
                "code": code,
                "message": message,
                "data": {"kind": kind, "type": "RemoteError"},
            },
        }
    )
