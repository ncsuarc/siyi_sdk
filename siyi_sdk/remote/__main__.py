"""Command line checks against a running camera service.

Usage::

    python -m siyi_sdk.remote status ws://iris:8765
    python -m siyi_sdk.remote calibrate ws://iris:8765
    python -m siyi_sdk.remote release ws://iris:8765
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from siyi_sdk import configure_logging
from siyi_sdk.exceptions import SIYIError
from siyi_sdk.remote.client import RemoteCamera


def _print_status(state: dict[str, Any], cam: RemoteCamera) -> None:
    camera, video, lock = state["camera"], state["video"], state["lock"]
    control = state["control"]
    print(f"service      {cam.url}  (round trip {_ms(cam.rtt)})")
    print(f"camera       {'alive' if camera['alive'] else 'NOT ANSWERING'}  ({camera['ip']})")
    if camera["firmware"] is not None:
        print(f"firmware     {camera['firmware']}")
    if camera["attitude"] is not None:
        att = camera["attitude"]
        print(f"attitude     yaw {att.yaw_deg:.1f}  pitch {att.pitch_deg:.1f}  ({camera['mount']})")
    if camera["zoom"] is not None:
        print(f"zoom         {camera['zoom']}x (max {camera['zoom_max']}x)")
    source = f" from {video['source']}" if video["live"] else ""
    print(f"video        {'live' if video['live'] else 'NONE'}{source}  (mode {video['mode']})")
    print(f"lock         {lock['state']}" + (f"  ({lock['reason']})" if lock.get("reason") else ""))
    holder = "this client" if control["controller"] else (control["holder"] or "nobody")
    print(f"control      {holder}  ({control['clients']} connected)")


def _ms(seconds: float | None) -> str:
    return "?" if seconds is None else f"{seconds * 1000:.0f} ms"


async def _run(action: str, url: str) -> int:
    async with RemoteCamera(url, name=f"cli-{action}", take_control=action != "status") as cam:
        if action == "status":
            await cam.ping()
            _print_status(await cam.get_state(), cam)
        elif action == "calibrate":
            print("Measuring: the gimbal turns about 20 degrees on each axis and back...")
            result = await cam.calibrate()
            config = result["config"]
            print(f"done; fit error {result['fit_error_deg']} deg")
            print(
                f"turn rate {config['deg_per_unit_yaw']}/{config['deg_per_unit_pitch']} deg/s per "
                f"unit, command delay {config['command_delay_ms']} ms, "
                f"video delay {config['frame_delay_ms']} ms, field of view {config['hfov_deg']} deg"
            )
            for note in result["notes"]:
                print("note:", note)
        elif action == "release":
            print(await cam.release())
    return 0


def main(argv: list[str] | None = None) -> None:
    """Run one action against the service and exit."""
    parser = argparse.ArgumentParser(
        prog="python -m siyi_sdk.remote", description="Check or control a camera service."
    )
    parser.add_argument(
        "action",
        choices=("status", "calibrate", "release"),
        help="status: what the service sees; calibrate: measure the point-lock loop "
        "(turns the gimbal); release: stop a point lock",
    )
    parser.add_argument("url", nargs="?", default="ws://localhost:8765")
    args = parser.parse_args(argv)
    configure_logging(level="WARNING")
    try:
        code = asyncio.run(_run(args.action, args.url))
    except SIYIError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
