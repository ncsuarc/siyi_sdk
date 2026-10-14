# Copyright (c) 2026 Mohamed Abdelkader <mohamedashraf123@gmail.com>
# All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Websocket front end for :class:`CameraSession`: the service a ground station talks to.

Run it on the computer wired to the camera::

    python -m siyi_sdk.remote.server --camera-ip 192.168.144.25 --port 8765

One connected client is the *controller*; the rest are observers that receive
events and may call read-only methods. See ``docs/remote.md`` for the protocol.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import itertools
import json
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

import structlog
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from siyi_sdk import __version__, configure_logging
from siyi_sdk.exceptions import ConfigurationError
from siyi_sdk.remote import protocol, registry
from siyi_sdk.remote.lock_config import DEFAULT_PATH, LockConfig
from siyi_sdk.remote.protocol import NotControllerError, UnknownMethodError
from siyi_sdk.remote.session import CameraSession

logger: Final = structlog.get_logger(__name__)

OnDisconnect = Literal["keep-lock", "release"]

# Passthrough commands that would fight a running point lock.
_LOCK_BREAKING: Final = frozenset(
    {
        "set_attitude",
        "set_attitude_nowait",
        "set_single_axis",
        "set_single_axis_nowait",
        "one_key_centering",
        "soft_reboot",
        "set_encoding_params",
    }
)
# Commands after which the zoom level may have changed.
_ZOOM_CHANGING: Final = frozenset({"absolute_zoom", "set_encoding_params"})
# Events dropped for a client whose link can't keep up (replies are never dropped).
_MAX_QUEUED_EVENTS: Final = 64

Handler = Callable[["_Peer", dict[str, Any]], Awaitable[Any]]


@dataclass(eq=False)
class _Peer:
    connection: ServerConnection
    number: int
    name: str = ""
    outbox: asyncio.Queue[tuple[bool, str]] = field(default_factory=asyncio.Queue)
    tasks: set[asyncio.Task[None]] = field(default_factory=set)

    @property
    def label(self) -> str:
        return self.name or f"client-{self.number}"

    def send(self, message: str, *, event: bool = False) -> None:
        if event and self.outbox.qsize() >= _MAX_QUEUED_EVENTS:
            return
        self.outbox.put_nowait((event, message))


@dataclass(frozen=True)
class _Method:
    handler: Handler
    writes: bool  # needs control


class CameraServer:
    """Serve one :class:`CameraSession` to ground stations over websockets.

    Args:
        session: The camera session (started and closed by this server).
        host: Interface to listen on.
        port: TCP port (0 picks a free one; see :attr:`port`).
        on_disconnect: What happens to a running point lock when the controller's
            connection drops. Manual motion always stops.
        ping_interval: Websocket keepalive interval; a dead radio link is detected
            within ``ping_interval + ping_timeout``.
        ping_timeout: Keepalive reply timeout.
    """

    def __init__(
        self,
        session: CameraSession,
        *,
        host: str = "0.0.0.0",
        port: int = 8765,
        on_disconnect: OnDisconnect = "keep-lock",
        ping_interval: float = 1.0,
        ping_timeout: float = 3.0,
    ) -> None:
        """Configure; nothing listens until :meth:`start`."""
        self.session = session
        self.host = host
        self.port = port
        self.on_disconnect = on_disconnect
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.controller: _Peer | None = None
        self._peers: set[_Peer] = set()
        self._numbers = itertools.count(1)
        self._server: Server | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._methods = self._build_methods()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Start the session and listen."""
        await self.session.start()
        self._unsubscribe = self.session.subscribe(self._broadcast)
        self._server = await serve(
            self._handle,
            self.host,
            self.port,
            ping_interval=self.ping_interval,
            ping_timeout=self.ping_timeout,
        )
        sockets = list(self._server.sockets)
        if sockets:
            self.port = sockets[0].getsockname()[1]
        logger.info("camera_service_listening", host=self.host, port=self.port)

    async def close(self) -> None:
        """Disconnect everyone, then stop the session."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        await self.session.close()

    async def __aenter__(self) -> CameraServer:  # noqa: D105
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:  # noqa: D105
        await self.close()

    # ------------------------------------------------------------------ connections

    async def _handle(self, connection: ServerConnection) -> None:
        peer = _Peer(connection, next(self._numbers))
        self._peers.add(peer)
        writer = asyncio.create_task(self._write(peer))
        if self.controller is None:
            self._set_controller(peer)
        else:
            self._send_control(peer)
        logger.info("ground_connected", peer=peer.label, remote=str(connection.remote_address))
        try:
            async for message in connection:
                if isinstance(message, bytes):
                    message = message.decode("utf-8", errors="replace")
                await self._receive(peer, message)
        except ConnectionClosed:
            pass
        finally:
            self._peers.discard(peer)
            for task in peer.tasks:
                task.cancel()
            writer.cancel()
            await asyncio.gather(writer, *peer.tasks, return_exceptions=True)
            logger.info("ground_disconnected", peer=peer.label)
            if self.controller is peer:
                await self._controller_lost(peer)

    async def _write(self, peer: _Peer) -> None:
        with contextlib.suppress(ConnectionClosed):
            while True:
                _, message = await peer.outbox.get()
                await peer.connection.send(message)

    async def _controller_lost(self, peer: _Peer) -> None:
        self.controller = None
        await self.session.stop_motion()
        if self.on_disconnect == "release":
            await self.session.release_lock(f"Point lock released: {peer.label} disconnected")
        for other in self._peers:
            self._send_control(other)

    def _set_controller(self, peer: _Peer | None) -> None:
        self.controller = peer
        for other in self._peers:
            self._send_control(other)

    def _send_control(self, peer: _Peer) -> None:
        holder = self.controller
        params = {"controller": holder is peer, "holder": holder.label if holder else None}
        peer.send(protocol.notification(protocol.EVENT_CONTROL, params), event=True)

    def _broadcast(self, event: str, params: dict[str, Any]) -> None:
        message = protocol.notification(event, params)
        for peer in self._peers:
            peer.send(message, event=True)

    # ------------------------------------------------------------------ dispatch

    async def _receive(self, peer: _Peer, text: str) -> None:
        try:
            msg = json.loads(text)
        except ValueError:
            peer.send(protocol.raw_error(None, protocol.PARSE_ERROR, "Invalid JSON", "bad_request"))
            return
        if not isinstance(msg, dict) or not isinstance(msg.get("method"), str):
            msg_id = msg.get("id") if isinstance(msg, dict) else None
            peer.send(
                protocol.raw_error(
                    msg_id, protocol.INVALID_REQUEST, "Not a JSON-RPC request", "bad_request"
                )
            )
            return
        method = msg["method"]
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        if "id" not in msg:
            await self._notification(peer, method, params)
            return
        # Run requests concurrently: a slow one (SD format, calibration) must not hold up
        # setpoints arriving on the same connection.
        task = asyncio.create_task(self._request(peer, msg["id"], method, params))
        peer.tasks.add(task)
        task.add_done_callback(peer.tasks.discard)

    async def _notification(self, peer: _Peer, method: str, params: dict[str, Any]) -> None:
        # Notifications get no reply, so bad ones are only logged.
        if method not in protocol.SETPOINT_METHODS:
            logger.warning("unknown_notification", peer=peer.label, method=method)
            return
        if self.controller is not peer:
            return
        try:
            if method == "rate":
                await self.session.set_rate(
                    float(params.get("yaw", 0)), float(params.get("pitch", 0))
                )
            else:
                await self.session.set_zoom_rate(float(params.get("direction", 0)))
        except (TypeError, ValueError) as exc:
            logger.warning("bad_setpoint", peer=peer.label, method=method, error=str(exc))

    async def _request(
        self,
        peer: _Peer,
        msg_id: object,
        method: str,
        params: dict[str, Any],
    ) -> None:
        try:
            entry = self._methods.get(method)
            if entry is None:
                raise UnknownMethodError(f"Unknown method {method!r}")
            if entry.writes and self.controller is not peer:
                holder = self.controller.label if self.controller else "nobody"
                raise NotControllerError(f"{holder} has control; call take_control first")
            value = await entry.handler(peer, protocol.decode_value(params))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if protocol.error_kind(exc) == "internal":
                logger.exception("request_failed", method=method)
            peer.send(protocol.error(msg_id, exc))
            return
        peer.send(protocol.result(msg_id, value))

    # ------------------------------------------------------------------ methods

    def _build_methods(self) -> dict[str, _Method]:
        session = self.session

        async def hello(peer: _Peer, params: dict[str, Any]) -> dict[str, Any]:
            peer.name = str(params.get("name") or "")[:64]
            self._send_control(peer)
            return {
                "protocol": protocol.PROTOCOL_VERSION,
                "sdk": __version__,
                "t": time.monotonic(),
                "controller": self.controller is peer,
            }

        async def ping(peer: _Peer, params: dict[str, Any]) -> dict[str, float]:
            return {"t": time.monotonic()}

        async def take_control(peer: _Peer, params: dict[str, Any]) -> dict[str, Any]:
            holder = self.controller
            if holder is not None and holder is not peer and not params.get("force"):
                raise NotControllerError(f"{holder.label} has control; pass force=true to take it")
            if holder is not peer:
                if holder is not None:
                    await session.stop_motion()
                self._set_controller(peer)
            return {"controller": True}

        async def release_control(peer: _Peer, params: dict[str, Any]) -> dict[str, Any]:
            if self.controller is peer:
                await session.stop_motion()
                self._set_controller(None)
            return {"controller": False}

        async def get_state(peer: _Peer, params: dict[str, Any]) -> dict[str, Any]:
            state = session.state()
            state["control"] = {
                "controller": self.controller is peer,
                "holder": self.controller.label if self.controller else None,
                "clients": len(self._peers),
            }
            return state

        async def list_commands(peer: _Peer, params: dict[str, Any]) -> list[dict[str, Any]]:
            return [command.describe() for command in registry.REMOTE_COMMANDS.values()]

        async def stop(peer: _Peer, params: dict[str, Any]) -> dict[str, Any]:
            await session.stop_motion()
            return {"stopped": True}

        async def lock(peer: _Peer, params: dict[str, Any]) -> dict[str, Any]:
            return await session.start_lock(
                _number(params, "x"),
                _number(params, "y"),
                frame_time=_optional_number(params, "frame_time"),
                controller=params.get("controller"),
                size=int(params["size"]) if params.get("size") else None,
            )

        async def release(peer: _Peer, params: dict[str, Any]) -> dict[str, Any]:
            return await session.release_lock(
                params.get("reason") or "Point lock released by ground station"
            )

        async def get_lock(peer: _Peer, params: dict[str, Any]) -> dict[str, Any]:
            return session.lock_snapshot()

        async def calibrate(peer: _Peer, params: dict[str, Any]) -> dict[str, Any]:
            return await session.calibrate()

        async def get_lock_config(peer: _Peer, params: dict[str, Any]) -> dict[str, Any]:
            return session.config.to_dict()

        async def set_lock_config(peer: _Peer, params: dict[str, Any]) -> dict[str, Any]:
            return session.set_lock_config(params)

        async def get_learned_loop(peer: _Peer, params: dict[str, Any]) -> dict[str, Any]:
            return session.learned_loop()

        async def apply_learned_loop(peer: _Peer, params: dict[str, Any]) -> dict[str, Any]:
            return await session.apply_learned_loop()

        methods: dict[str, _Method] = {
            "hello": _Method(hello, writes=False),
            "ping": _Method(ping, writes=False),
            "take_control": _Method(take_control, writes=False),
            "release_control": _Method(release_control, writes=False),
            "get_state": _Method(get_state, writes=False),
            "list_commands": _Method(list_commands, writes=False),
            "get_lock": _Method(get_lock, writes=False),
            "get_lock_config": _Method(get_lock_config, writes=False),
            "get_learned_loop": _Method(get_learned_loop, writes=False),
            "stop": _Method(stop, writes=True),
            "lock": _Method(lock, writes=True),
            "release": _Method(release, writes=True),
            "calibrate": _Method(calibrate, writes=True),
            "set_lock_config": _Method(set_lock_config, writes=True),
            "apply_learned_loop": _Method(apply_learned_loop, writes=True),
        }
        for name, command in registry.REMOTE_COMMANDS.items():
            methods[name] = _Method(self._passthrough(command), writes=not command.read_only)
        return methods

    def _passthrough(self, command: registry.Command) -> Handler:
        session = self.session

        async def run(peer: _Peer, params: dict[str, Any]) -> Any:  # noqa: ANN401
            args = dict(params)
            confirmed = args.pop("confirm", False) is True
            if command.confirmation and not confirmed:
                raise ConfigurationError(
                    f"{command.name} needs confirm=true: {command.confirmation}"
                )
            if command.name in _LOCK_BREAKING:
                await session.release_lock(f"Point lock released by {command.name}")
            value = await registry.execute(session.client, command, args)
            if command.name in _ZOOM_CHANGING:
                with contextlib.suppress(Exception):
                    await session.refresh_zoom()
            return value

        return run


def _number(params: dict[str, Any], name: str) -> float:
    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{name} must be a number")
    return float(value)


def _optional_number(params: dict[str, Any], name: str) -> float | None:
    return None if params.get(name) is None else _number(params, name)


# ============================================================================
# Command line
# ============================================================================


def main(argv: list[str] | None = None) -> None:
    """Run the camera service until interrupted."""
    parser = argparse.ArgumentParser(
        prog="python -m siyi_sdk.remote.server",
        description="Serve an A8 Mini to ground stations over websockets (JSON-RPC 2.0).",
    )
    parser.add_argument("--camera-ip", default="192.168.144.25")
    parser.add_argument("--host", default="0.0.0.0", help="interface to listen on")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_PATH,
        help=f"lock settings file (default {DEFAULT_PATH})",
    )
    parser.add_argument(
        "--video",
        choices=("auto", "rtsp", "firmware", "both", "none"),
        default="auto",
        help="tracking video source (default: firmware link when supported, else RTSP)",
    )
    parser.add_argument("--on-disconnect", choices=("keep-lock", "release"), default="keep-lock")
    parser.add_argument("--setpoint-timeout", type=float, default=0.3, help="seconds")
    parser.add_argument("--attitude-hz", type=float, default=20.0)
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--log-format", choices=("json", "console"), default="json")
    args = parser.parse_args(argv)

    configure_logging(level=args.log_level, fmt=args.log_format)
    session = CameraSession(
        args.camera_ip,
        config=LockConfig.load(args.config),
        config_path=args.config,
        video=args.video,
        setpoint_timeout=args.setpoint_timeout,
        attitude_hz=args.attitude_hz,
    )
    server = CameraServer(session, host=args.host, port=args.port, on_disconnect=args.on_disconnect)

    async def run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        if sys.platform != "win32":
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)
        async with server:
            with contextlib.suppress(asyncio.CancelledError):
                await stop.wait()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
