# Copyright (c) 2026 Mohamed Abdelkader <mohamedashraf123@gmail.com>
# All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Keep the gimbal pointed at whatever is in the image centre right now.

Locks the spot at the centre of the first video frame, then steers the gimbal
so that spot stays centred while the aircraft moves. Put the gimbal in Lock
mode first. Install with ``pip install -e ".[tracking,stream]"``.

With ``--calibrate`` the gimbal first turns about 20 degrees on each axis and
back to measure turn rate, delays, axis directions and field of view; point the
camera at a textured, still scene for that. Without it, rough defaults are used
and the video delay is refined while locked.

    python examples/point_lock.py --calibrate
    python examples/point_lock.py --control rate
"""

from __future__ import annotations

import argparse
import asyncio

from siyi_sdk import StreamFrame, configure_logging, connect_udp
from siyi_sdk.models import DataStreamFreq, GimbalDataType
from siyi_sdk.tracking import (
    AttitudeHistory,
    FrameMotionRecorder,
    GimbalPointLock,
    LoopModel,
    calibrate_loop,
)


async def main(args: argparse.Namespace) -> None:
    """Optionally measure the loop, then lock the image centre for ``args.seconds``."""
    configure_logging(level="WARNING")
    client = await connect_udp(args.ip, 37260)
    history = AttitudeHistory()
    history.attach(client)
    await client.request_gimbal_stream(GimbalDataType.ATTITUDE, DataStreamFreq.HZ50)
    stream = client.create_stream()
    recorder = FrameMotionRecorder()
    lock: GimbalPointLock | None = None

    async def on_frame(frame: StreamFrame) -> None:
        recorder.add(frame.frame, frame.timestamp)
        if lock is None:
            return
        if not lock.active:
            lock.lock(frame.frame, frame.width / 2, frame.height / 2)
            print("Locked the image centre.")
            return
        await lock.update(frame.frame, timestamp=frame.timestamp)

    stream.on_frame(on_frame)
    try:
        await stream.start()
        loop, signs, hfov = LoopModel(), (1, 1), args.hfov
        if args.calibrate:
            print("Measuring: the gimbal will turn about 20 degrees on each axis and back.")
            result = await calibrate_loop(client.rotate_nowait, history, recorder, hfov_deg=hfov)
            loop, signs, hfov = result.model, result.attitude_signs, result.hfov_deg
            print(f"  turn rate {loop.deg_per_unit} deg/s per unit")
            print(
                f"  command delay {loop.command_delay_s * 1000:.0f} ms, "
                f"video delay {loop.video_delay_s * 1000:.0f} ms"
            )
            print(f"  attitude signs {signs}, field of view {hfov:.1f} deg")
            for note in result.notes:
                print(f"  warning: {note}")
        lock = GimbalPointLock(
            client,
            hfov_deg=hfov,
            loop=loop,
            attitude=history,
            attitude_signs=signs,
            control=args.control,
            trust_video_delay=args.calibrate,
        )
        for _ in range(int(args.seconds)):
            await asyncio.sleep(1.0)
            s = lock.status
            print(
                f"{s.state.value:9}  score {s.score:.2f}  "
                f"error yaw {s.error_deg[0]:+5.2f} pitch {s.error_deg[1]:+5.2f} deg  "
                f"video delay {loop.video_delay_s * 1000:.0f} ms"
            )
    except KeyboardInterrupt:
        pass
    finally:
        if lock is not None:
            await lock.release()
        await stream.stop()
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ip", default="192.168.144.25")
    parser.add_argument("--calibrate", action="store_true", help="measure the loop first")
    parser.add_argument("--control", choices=("angle", "rate"), default="angle")
    parser.add_argument("--hfov", type=float, default=81.0, help="field of view at 1x")
    parser.add_argument("--seconds", type=float, default=60.0)
    asyncio.run(main(parser.parse_args()))
