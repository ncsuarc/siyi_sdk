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
        }


# Fixed allowlist: no client method can be invoked solely by guessing its name.
_GROUPS: dict[str, tuple[str, ...]] = {
    "System and network": (
        "heartbeat", "get_firmware_version", "get_hardware_id", "get_system_time",
        "set_utc_time", "get_gimbal_system_info", "soft_reboot", "get_ip_config",
        "set_ip_config",
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
        "set_encoding_params", "format_sd_card", "get_picture_name_type",
        "set_picture_name_type", "get_osd_flag", "set_osd_flag",
    ),
    "Autopilot and telemetry": (
        "send_aircraft_attitude", "request_fc_stream", "request_gimbal_stream",
        "send_raw_gps", "get_control_mode", "get_weak_threshold",
        "set_weak_threshold", "get_motor_voltage", "get_weak_control_mode",
        "set_weak_control_mode",
    ),
}

_CONFIRMATIONS = {
    "format_sd_card": "Format the SD card and erase its files?",
    "soft_reboot": "Reboot the selected camera or gimbal modules?",
    "set_ip_config": "Change the camera network configuration and reconnect?",
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
