"""TCP transport whose reads run in a dedicated thread.

``TCPTransport`` reads through the event loop, 4 KiB at a time. The camera's private video
channel (port 37256) can deliver over 100 KB in one burst, so those reads fall behind whenever the
loop is busy, and a chunk's arrival time is when the loop got to it, not when it came in. The SIYI
AI module reads the same channel with a blocking ``recv`` of up to 3 MB in its own thread. This
transport does the same: the thread takes what has arrived the moment it arrives, stamps it, and
hands it to the event loop through a queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import struct
import threading
import time
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from typing import Final, TypeAlias

import structlog

from ..constants import DEFAULT_IP, DEFAULT_TCP_PORT
from ..logging_config import trace_fields
from .base import AbstractTransport

logger: Final = structlog.get_logger(__name__)

READ_SIZE: Final = 1 << 20
# How often a blocked read or write gives up, so a closed connection is noticed promptly.
_POLL: Final = 0.25

# What the reader thread hands over: (data, arrival time), the error that ended it, or None at EOF.
_Item: TypeAlias = "tuple[bytes, float] | BaseException | None"
# The class has a ``socket`` property, which would shadow the module in its own annotations.
_Socket: TypeAlias = socket.socket


def _close_late(opening: asyncio.Future[_Socket]) -> None:
    """Close a socket whose connect finished after the caller gave up on it."""
    if not opening.cancelled() and opening.exception() is None:
        opening.result().close()


class ThreadedTCPTransport(AbstractTransport):
    """TCP transport that reads in a thread and stamps each chunk on arrival.

    Besides the usual :meth:`stream`, :meth:`stream_timed` yields each chunk with the monotonic
    time the thread received it. Writes go through one worker thread, so a stalled peer cannot
    block the event loop, and they stay in order.

    Example:
        >>> transport = ThreadedTCPTransport(ip="192.168.144.25", port=37256)
        >>> await transport.connect()
        >>> async for chunk, arrived in transport.stream_timed():
        ...     print(len(chunk), arrived)
    """

    def __init__(
        self,
        ip: str = DEFAULT_IP,
        port: int = DEFAULT_TCP_PORT,
        *,
        connect_timeout: float = 5.0,
        read_size: int = READ_SIZE,
    ) -> None:
        """Initialize the transport.

        Args:
            ip: Target IP address.
            port: Target TCP port.
            connect_timeout: Seconds to wait for the TCP handshake.
            read_size: Largest read, in bytes, taken from the socket at once.
        """
        self._ip: str = ip
        self._port: int = port
        self._connect_timeout: float = connect_timeout
        self._read_size: int = read_size
        self._connected: bool = False
        self._sock: _Socket | None = None
        self._thread: threading.Thread | None = None
        self._sender: ThreadPoolExecutor | None = None
        self._queue: asyncio.Queue[_Item] | None = None
        self._closing: threading.Event = threading.Event()

    async def connect(self) -> None:
        """Establish the TCP connection and start the reader thread.

        Raises:
            ConnectionError: If the connection fails.
        """
        if self._sock is not None:
            raise RuntimeError("Close the previous TCP connection before connecting")
        loop = asyncio.get_running_loop()
        opening = loop.run_in_executor(None, self._open)
        try:
            sock = await opening
        except asyncio.CancelledError:
            # A connect timeout cancels us mid-handshake; don't leak the socket if it arrives.
            opening.add_done_callback(_close_late)
            raise
        except OSError as e:
            from ..exceptions import ConnectionError as ConnError

            raise ConnError(f"Failed to connect to {self._ip}:{self._port}: {e}") from e
        self._sock = sock
        self._closing = threading.Event()
        self._queue = asyncio.Queue()
        self._sender = ThreadPoolExecutor(max_workers=1, thread_name_prefix="siyi-tcp-send")
        self._connected = True
        self._thread = threading.Thread(
            target=self._read_loop,
            args=(sock, loop, self._queue, self._closing),
            name="siyi-tcp-recv",
            daemon=True,
        )
        self._thread.start()
        logger.info("connected", transport="threaded_tcp", peer=f"{self._ip}:{self._port}")

    def _open(self) -> _Socket:
        sock = socket.create_connection((self._ip, self._port), timeout=self._connect_timeout)
        try:
            # Small command frames must not wait behind Nagle/delayed-ACK.
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # Blocking, but waking every _POLL seconds so no read or write hangs for good.
            sock.settimeout(_POLL)
        except OSError:
            sock.close()
            raise
        return sock

    def _read_loop(
        self,
        sock: _Socket,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[_Item],
        closing: threading.Event,
    ) -> None:
        buffer = memoryview(bytearray(self._read_size))
        ended: BaseException | None = None
        try:
            while not closing.is_set():
                try:
                    count = sock.recv_into(buffer)
                except TimeoutError:
                    continue
                arrival = time.monotonic()
                if count == 0:
                    break  # the peer closed the connection
                self._post(loop, queue, (bytes(buffer[:count]), arrival))
        except OSError as e:
            if not closing.is_set():
                ended = e
        if self._sock is sock:
            self._connected = False
        self._post(loop, queue, ended)

    @staticmethod
    def _post(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[_Item], item: _Item) -> None:
        with contextlib.suppress(RuntimeError):  # the loop has already shut down
            loop.call_soon_threadsafe(queue.put_nowait, item)

    async def close(self) -> None:
        """Close the connection and stop the reader thread."""
        await self._shutdown(graceful=True)
        logger.info("disconnected", transport="threaded_tcp")

    async def abort(self) -> None:
        """Drop the connection at once with a TCP reset, discarding unsent data.

        Unlike :meth:`close`, this never waits for the peer, so a stalled peer cannot keep the
        connection half-open on its side.
        """
        await self._shutdown(graceful=False)
        logger.info("aborted", transport="threaded_tcp")

    async def _shutdown(self, *, graceful: bool) -> None:
        sock, thread, sender, queue = self._sock, self._thread, self._sender, self._queue
        self._sock = self._thread = self._sender = self._queue = None
        self._connected = False
        self._closing.set()
        if sock is not None:
            with contextlib.suppress(OSError):
                if graceful:
                    sock.shutdown(socket.SHUT_RDWR)
                else:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            with contextlib.suppress(OSError):
                sock.close()
        if queue is not None:
            queue.put_nowait(None)  # wake a consumer that is waiting for data
        if sender is not None:
            sender.shutdown(wait=False, cancel_futures=True)
        if thread is not None:
            await asyncio.get_running_loop().run_in_executor(None, thread.join, 1.0)

    @property
    def socket(self) -> _Socket | None:
        """The connected socket object, or None (for socket options and diagnostics)."""
        return self._sock

    async def send(self, data: bytes) -> None:
        """Send data over TCP.

        Args:
            data: Bytes to send.

        Raises:
            NotConnectedError: If not connected.
            SendError: If send fails.
        """
        sock, sender = self._sock, self._sender
        if not self._connected or sock is None or sender is None:
            from ..exceptions import NotConnectedError

            raise NotConnectedError("ThreadedTCPTransport.send() called before connect()")
        try:
            await asyncio.get_running_loop().run_in_executor(sender, sock.sendall, data)
        except OSError as e:
            from ..exceptions import SendError

            self._connected = False
            raise SendError(f"TCP send failed: {e}") from e
        logger.debug(
            "frame_tx",
            transport="threaded_tcp",
            peer=f"{self._ip}:{self._port}",
            length=len(data),
            **trace_fields(data, __name__),
        )

    async def stream_timed(self) -> AsyncIterator[tuple[bytes, float]]:
        """Yield received chunks with the monotonic time each one arrived.

        Yields:
            tuple[bytes, float]: A chunk (up to ``read_size`` bytes) and its arrival time.
        """
        queue = self._queue
        if queue is None:
            return
        while True:
            item = await queue.get()
            if item is None:
                # EOF: the peer closed the connection (or we did).
                logger.info("eof_received", transport="threaded_tcp")
                break
            if isinstance(item, BaseException):
                logger.error("stream_error", transport="threaded_tcp", error=str(item))
                break
            logger.debug(
                "frame_rx",
                transport="threaded_tcp",
                length=len(item[0]),
                **trace_fields(item[0], __name__),
            )
            yield item
        self._connected = False

    async def stream(self) -> AsyncIterator[bytes]:
        """Yield received TCP data chunks.

        Yields:
            bytes: Received data chunks (up to ``read_size`` bytes each).
        """
        async for data, _ in self.stream_timed():
            yield data

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
