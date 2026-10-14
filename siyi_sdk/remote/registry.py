"""Typed allowlist of :class:`SIYIClient` methods that may be invoked by name.

Used by the camera service and the debugging dashboard's command explorer. Each
command's arguments are validated against a pydantic model built from the method
signature before anything is sent, so no client method can be reached just by
guessing its name, and bad arguments never reach the camera.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Final, get_type_hints

from pydantic import BaseModel, ConfigDict, create_model

from siyi_sdk import models
from siyi_sdk.client import SIYIClient


@dataclass(frozen=True)
class Command:
    """One invocable client method and its argument model."""

    name: str
    group: str
    input_model: type[BaseModel]
    confirmation: str | None = None

    def describe(self) -> dict[str, Any]:
        """JSON-schema description for UIs."""
        schema = self.input_model.model_json_schema()
        for name, definition in schema.get("$defs", {}).items():
            enum_type = getattr(models, name, None)
            if isinstance(enum_type, type) and issubclass(enum_type, IntEnum):
                definition["x-enumNames"] = [item.name for item in enum_type]
        return {
            "name": self.name,
            "group": self.group,
            "description": inspect.getdoc(getattr(SIYIClient, self.name)) or "",
            "schema": schema,
            "confirmation": self.confirmation,
            "read_only": self.read_only,
        }

    @property
    def read_only(self) -> bool:
        """Queries are safe to repeat; everything else changes camera state."""
        return self.name.startswith("get_") and self.confirmation is None


# Methods whose commands the A8 mini firmware never answers (system time, gimbal
# system info, IP get/set, picture naming, FC stream, weak-control and motor
# voltage queries) are left out; see _UNSUPPORTED_ON_A8 in siyi_sdk/client.py.
_GROUPS: Final[dict[str, tuple[str, ...]]] = {
    "System and network": (
        "heartbeat",
        "get_firmware_version",
        "get_hardware_id",
        "set_utc_time",
        "soft_reboot",
    ),
    "Gimbal": (
        "rotate",
        "rotate_nowait",
        "one_key_centering",
        "set_attitude",
        "set_attitude_nowait",
        "set_single_axis",
        "set_single_axis_nowait",
        "get_gimbal_mode",
        "get_gimbal_attitude",
        "get_magnetic_encoder",
    ),
    "Digital zoom": (
        "manual_zoom",
        "manual_zoom_nowait",
        "absolute_zoom",
        "get_zoom_range",
        "get_current_zoom",
    ),
    "Camera and media": (
        "get_camera_system_info",
        "capture",
        "get_encoding_params",
        "set_encoding_params",
        "format_sd_card",
        "get_osd_flag",
        "set_osd_flag",
    ),
    "Autopilot and telemetry": (
        "send_aircraft_attitude",
        "request_gimbal_stream",
        "send_raw_gps",
    ),
}

_CONFIRMATIONS: Final[dict[str, str]] = {
    "format_sd_card": "Format the SD card and erase its files?",
    "soft_reboot": "Reboot the selected camera or gimbal modules?",
}

# Not offered by the camera service. Speed commands keep the gimbal or zoom moving until
# the next command, so remote clients use the ``rate``/``zoom_rate`` setpoints, which stop
# by themselves when updates stop. The service owns the attitude stream and uses UDP.
REMOTE_EXCLUDED: Final = frozenset(
    {
        "heartbeat",
        "rotate",
        "rotate_nowait",
        "manual_zoom",
        "manual_zoom_nowait",
        "request_gimbal_stream",
    }
)


def _input_model(name: str) -> type[BaseModel]:
    method = getattr(SIYIClient, name)
    signature = inspect.signature(method)
    hints = get_type_hints(method)
    fields: dict[str, Any] = {}
    for param in signature.parameters.values():
        if param.name == "self":
            continue
        annotation = hints[param.name]
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[param.name] = (annotation, default)
    return create_model(
        f"{''.join(part.title() for part in name.split('_'))}Input",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )


COMMANDS: Final[dict[str, Command]] = {
    name: Command(name, group, _input_model(name), _CONFIRMATIONS.get(name))
    for group, names in _GROUPS.items()
    for name in names
}

REMOTE_COMMANDS: Final[dict[str, Command]] = {
    name: command for name, command in COMMANDS.items() if name not in REMOTE_EXCLUDED
}


async def execute(client: SIYIClient, command: Command, args: dict[str, Any]) -> object:
    """Validate named arguments, then invoke one registered SDK method.

    Raises:
        pydantic.ValidationError: (a ``ValueError``) if the arguments don't fit.
    """
    parsed = command.input_model.model_validate(args)
    kwargs = {name: getattr(parsed, name) for name in command.input_model.model_fields}
    return await getattr(client, command.name)(**kwargs)
