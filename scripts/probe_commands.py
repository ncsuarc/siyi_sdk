"""Send each read-only SDK query once and report the raw reply or a timeout.

Nothing here changes camera state: only request/query commands are sent. The
client's fail-fast guard for commands A8 mini firmware never answers is bypassed,
so this also confirms on hardware that those really time out.

    python scripts/probe_commands.py --ip 192.168.144.25
"""

from __future__ import annotations

import argparse
import asyncio

import siyi_sdk.client
from siyi_sdk import SIYIClient
from siyi_sdk.exceptions import TimeoutError
from siyi_sdk.transport.udp import UDPTransport

# Probe everything, including commands the client normally refuses to send.
siyi_sdk.client._UNSUPPORTED_ON_A8 = frozenset()

# (cmd_id, payload, label)
QUERIES: list[tuple[int, bytes, str]] = [
    (0x01, b"", "firmware version"),
    (0x02, b"", "hardware id"),
    (0x0A, b"", "camera system info"),
    (0x0D, b"", "gimbal attitude"),
    (0x16, b"", "zoom range"),
    (0x18, b"", "current zoom"),
    (0x19, b"", "gimbal mode"),
    (0x20, b"\x00", "encoding params (recording)"),
    (0x20, b"\x01", "encoding params (main)"),
    (0x20, b"\x02", "encoding params (sub)"),
    (0x26, b"", "magnetic encoder"),
    (0x27, b"", "control mode"),
    (0x28, b"", "weak control threshold"),
    (0x2A, b"", "motor voltage"),
    (0x31, b"", "gimbal system info"),
    (0x40, b"", "system time"),
    (0x49, b"\x00", "picture name type (photo)"),
    (0x4B, b"", "HDMI OSD flag"),
    (0x70, b"", "weak control mode"),
    (0x81, b"", "IP config"),
]


async def main(ip: str, timeout: float) -> None:
    """Probe every query in QUERIES once, without retries."""
    async with SIYIClient(UDPTransport(ip=ip), default_timeout=timeout, max_retries=0) as client:
        for cmd_id, payload, label in QUERIES:
            try:
                reply = await client._send_command(cmd_id, payload)
                print(f"0x{cmd_id:02X} {label:30s} OK   {len(reply):2d} B  {reply.hex(' ')}")
            except TimeoutError:
                print(f"0x{cmd_id:02X} {label:30s} TIMEOUT (no reply in {timeout}s)")
            except Exception as exc:  # report every failure and keep probing
                print(f"0x{cmd_id:02X} {label:30s} ERROR {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ip", default="192.168.144.25")
    parser.add_argument("--timeout", type=float, default=1.5)
    args = parser.parse_args()
    asyncio.run(main(args.ip, args.timeout))
