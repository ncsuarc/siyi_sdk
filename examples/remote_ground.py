"""Ground-station side of the camera service: control, telemetry and point lock.

Start the service on the computer wired to the camera first::

    python -m siyi_sdk.remote.server --camera-ip 192.168.144.25

then run this on the ground::

    python examples/remote_ground.py ws://<companion-ip>:8765 [--video]

With ``--video`` the ground decodes the camera's RTSP stream itself and locks on
the frame it last showed, passing that frame's arrival time so the service locks
on the same picture.
"""

from __future__ import annotations

import argparse
import asyncio

from siyi_sdk import configure_logging
from siyi_sdk.exceptions import SIYIError
from siyi_sdk.remote import RemoteCamera


async def main(url: str, camera_ip: str, use_video: bool) -> None:
    """Connect, nudge the gimbal, then lock the image centre for ten seconds."""
    async with RemoteCamera(url, name="example") as cam:
        state = await cam.get_state()
        print("camera alive:", state["camera"]["alive"], "| video:", state["video"])
        print("in control:", cam.is_controller)

        cam.on_attitude(
            lambda att: print(f"\ryaw {att.yaw_deg:7.1f}  pitch {att.pitch_deg:6.1f}", end="")
        )
        cam.on_lock_status(lambda s: s["state"] == "idle" and print("\nlock:", s))
        cam.on_link(lambda link: print("\nlink:", link))

        # Nudge right for one second; the service stops the gimbal if we go quiet.
        cam.rate(30, 0)
        await asyncio.sleep(1.0)
        cam.rate(0, 0)
        await cam.set_attitude(0.0, -30.0)
        await asyncio.sleep(1.0)

        frame_time = None
        stream = None
        if use_video:
            from siyi_sdk import SIYIStream, StreamConfig, build_rtsp_url

            stream = SIYIStream(StreamConfig(build_rtsp_url(camera_ip)))
            await stream.start()
            await asyncio.sleep(1.0)
            if stream.last_frame is not None:
                frame_time = stream.last_frame.timestamp

        try:
            print("\nlocking the centre:", await cam.lock(0.0, 0.0, frame_time=frame_time))
            await asyncio.sleep(10.0)
            print("\nreleased:", await cam.release())
        except SIYIError as exc:
            print("\nlock failed:", type(exc).__name__, exc)
        finally:
            if stream is not None:
                await stream.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("url", nargs="?", default="ws://localhost:8765")
    parser.add_argument("--camera-ip", default="192.168.144.25", help="for --video")
    parser.add_argument("--video", action="store_true", help="decode RTSP on the ground")
    args = parser.parse_args()
    configure_logging(level="WARNING")
    asyncio.run(main(args.url, args.camera_ip, args.video))
