"""Typed, explicit A8 Mini SDK command catalog for the local dashboard."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, get_type_hints

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, create_model

from siyi_sdk import SIYIClient
from siyi_sdk import models


@dataclass(frozen=True)
class Command:
    name: str
    group: str
    input_model: type[BaseModel]
    confirmation: str | None = None

    def describe(self) -> dict[str, Any]:
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


# Fixed allowlist: no client method can be invoked solely by guessing its name.
# Methods whose commands the A8 mini firmware never answers (system time, gimbal
# system info, IP get/set, picture naming, FC stream, weak-control and motor
# voltage queries) are left out; see _UNSUPPORTED_ON_A8 in siyi_sdk/client.py.
_GROUPS: dict[str, tuple[str, ...]] = {
    "System and network": (
        "heartbeat", "get_firmware_version", "get_hardware_id", "set_utc_time",
        "soft_reboot",
    ),
    "Gimbal": (
        "rotate", "rotate_nowait", "one_key_centering", "set_attitude",
        "set_attitude_nowait", "set_single_axis", "set_single_axis_nowait",
        "get_gimbal_mode", "get_gimbal_attitude", "get_magnetic_encoder",
    ),
    "Digital zoom": (
        "manual_zoom", "manual_zoom_nowait", "absolute_zoom", "get_zoom_range",
        "get_current_zoom",
    ),
    "Camera and media": (
        "get_camera_system_info", "capture", "get_encoding_params",
        "set_encoding_params", "format_sd_card", "get_osd_flag", "set_osd_flag",
    ),
    "Autopilot and telemetry": (
        "send_aircraft_attitude", "request_gimbal_stream", "send_raw_gps",
    ),
}

_CONFIRMATIONS = {
    "format_sd_card": "Format the SD card and erase its files?",
    "soft_reboot": "Reboot the selected camera or gimbal modules?",
}


def _input_model(name: str) -> type[BaseModel]:
    method = getattr(SIYIClient, name)
    signature = inspect.signature(method)
    hints = get_type_hints(method)
    fields: dict[str, tuple[Any, Any]] = {}
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


COMMANDS: dict[str, Command] = {
    name: Command(name, group, _input_model(name), _CONFIRMATIONS.get(name))
    for group, names in _GROUPS.items()
    for name in names
}


async def execute(client: SIYIClient, command: Command, args: dict[str, Any]) -> Any:
    """Validate named arguments, then invoke one registered SDK method."""
    parsed = command.input_model.model_validate(args)
    kwargs = {name: getattr(parsed, name) for name in command.input_model.model_fields}
    result = await getattr(client, command.name)(**kwargs)
    return jsonable_encoder(result)
