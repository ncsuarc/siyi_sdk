"""Typed, explicit A8 Mini SDK command catalog for the local dashboard.

The allowlist itself lives in :mod:`siyi_sdk.remote.registry`, shared with the camera
service; this module adds the JSON encoding the dashboard's browser code expects.
"""

from __future__ import annotations

from typing import Any

from fastapi.encoders import jsonable_encoder

from siyi_sdk import SIYIClient
from siyi_sdk.remote import registry
from siyi_sdk.remote.registry import COMMANDS, Command

__all__ = ["COMMANDS", "Command", "execute"]


async def execute(client: SIYIClient, command: Command, args: dict[str, Any]) -> Any:  # noqa: ANN401
    """Validate named arguments, then invoke one registered SDK method."""
    return jsonable_encoder(await registry.execute(client, command, args))
