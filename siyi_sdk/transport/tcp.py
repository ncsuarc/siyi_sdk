# Copyright (c) 2026 Mohamed Abdelkader <mohamedashraf123@gmail.com>
# All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""TCP transport implementation for SIYI SDK.

This module provides a TCP transport using asyncio streams.
TCP requires periodic heartbeat frames at 1 Hz.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from typing import Final

import structlog

from ..constants import DEFAULT_IP, DEFAULT_TCP_PORT
from ..logging_config import trace_fields
from .base import AbstractTransport

logger: Final = structlog.get_logger(__name__)


class TCPTransport(AbstractTransport):
    """TCP transport for SIYI SDK.

    This transport uses asyncio streams (StreamReader/StreamWriter) for
    bidirectional TCP communication. TCP connections require periodic
    heartbeat frames (1 Hz) to maintain the connection.

    TCP needs A8 mini camera firmware 0.3.6+ / gimbal firmware 0.4.8+. The
    camera serves one TCP client at a time and drops it after about 4 s
    without a 0x00 heartbeat; other traffic does not keep the link alive.

    Example:
        >>> transport = TCPTransport(ip="192.168.144.25", port=37260)
        >>> await transport.connect()
        >>> await transport.send(b"...")
        >>> async for chunk in transport.stream():
        ...     print(chunk)
    """

    def __init__(
        self,
        ip: str = DEFAULT_IP,
        port: int = DEFAULT_TCP_PORT,
        *,
        connect_timeout: float = 5.0,
    ) -> None:
        """Initialize TCP transport.

        Args:
            ip: Target gimbal IP address.
            port: Target TCP port.
            connect_timeout: Seconds to wait for the TCP handshake.
        """
        self._ip: str = ip
        self._port: int = port
        self._connect_timeout: float = connect_timeout
        self._connected: bool = False
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        """Establish TCP connection.

        Raises:
            ConnectionError: If connection fails.
        """
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._ip, self._port), self._connect_timeout
            )
            self._reader, self._writer = reader, writer
            # Small command frames must not wait behind Nagle/delayed-ACK.
            sock = writer.get_extra_info("socket")
            if sock is not None:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._connected = True
            logger.info(
                "connected",
                transport="tcp",
                peer=f"{self._ip}:{self._port}",
            )
        except (OSError, asyncio.TimeoutError) as e:
            from ..exceptions import ConnectionError as ConnError

            raise ConnError(f"Failed to connect to {self._ip}:{self._port}: {e}") from e

    async def close(self) -> None:
        """Close the TCP connection."""
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception as e:
                logger.warning("close_error", transport="tcp", error=str(e))
            finally:
                self._writer = None
                self._reader = None

        self._connected = False
        logger.info("disconnected", transport="tcp")

    async def abort(self) -> None:
        """Drop the connection at once with a TCP reset, discarding unsent data.

        Unlike :meth:`close`, this never waits for the peer, so a stalled peer
        cannot keep the connection half-open on its side.
        """
        if self._writer:
            self._writer.transport.abort()
            self._writer = None
            self._reader = None
        self._connected = False
        logger.info("aborted", transport="tcp")

    async def send(self, data: bytes) -> None:
        """Send data over TCP.

        Args:
            data: Bytes to send.

        Raises:
            NotConnectedError: If not connected.
            SendError: If send fails.
        """
        if not self._connected or not self._writer:
            from ..exceptions import NotConnectedError

            raise NotConnectedError("TCPTransport.send() called before connect()")

        try:
            self._writer.write(data)
            await self._writer.drain()
            logger.debug(
                "frame_tx",
                transport="tcp",
                peer=f"{self._ip}:{self._port}",
                length=len(data),
                **trace_fields(data, __name__),
            )
        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            from ..exceptions import SendError

            self._connected = False
            raise SendError(f"TCP send failed: {e}") from e

    async def stream(self) -> AsyncIterator[bytes]:
        """Yield received TCP data chunks.

        Yields:
            bytes: Received data chunks (up to 4096 bytes each).
        """
        if not self._reader:
            return

        while self._connected:
            try:
                data = await self._reader.read(4096)
                if not data:
                    # EOF — connection closed by remote
                    logger.info("eof_received", transport="tcp")
                    self._connected = False
                    break

                logger.debug(
                    "frame_rx",
                    transport="tcp",
                    length=len(data),
                    **trace_fields(data, __name__),
                )
                yield data
            except (ConnectionResetError, BrokenPipeError, OSError) as e:
                logger.error("stream_error", transport="tcp", error=str(e))
                self._connected = False
                break

    @property
    def is_connected(self) -> bool:
        """Return True if connected.

        Returns:
            bool: Connection state.
        """
        return self._connected

    @property
    def supports_heartbeat(self) -> bool:
        """Return True (TCP requires heartbeat).

        Returns:
            bool: Always True for TCP.
        """
        return True
