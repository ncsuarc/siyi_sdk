# Copyright (c) 2026 Mohamed Abdelkader <mohamedashraf123@gmail.com>
# All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Keep the gimbal pointed at whatever is in the image centre right now.

Locks the spot at the centre of the first video frame, then steers the gimbal
with rotation-speed commands so that spot stays centred while the aircraft
moves. Put the gimbal in Lock mode first so aircraft rotation is already
stabilized. Install with ``pip install -e ".[tracking,stream]"``.

Tune ``LockGains.kp`` on your hardware: if the camera oscillates around the
spot, lower it; if it lags behind, raise it.
"""

from __future__ import annotations

import asyncio

from siyi_sdk import StreamFrame, configure_logging, connect_udp
from siyi_sdk.tracking import GimbalPointLock, LockGains

RUN_SECONDS = 60.0


async def main() -> None:
    """Lock the image centre and hold it for ``RUN_SECONDS``."""
    configure_logging(level="WARNING")
    client = await connect_udp("192.168.144.25", 37260)
    stream = client.create_stream()
    lock = GimbalPointLock(client, hfov_deg=81.0, gains=LockGains(kp=2.0))

    async def on_frame(frame: StreamFrame) -> None:
        if not lock.active:
            lock.lock(frame.frame, frame.width / 2, frame.height / 2)
            print("Locked the image centre.")
            return
        await lock.update(frame.frame, timestamp=frame.timestamp)

    stream.on_frame(on_frame)
    try:
        await stream.start()
        for _ in range(int(RUN_SECONDS)):
            await asyncio.sleep(1.0)
            s = lock.status
            print(
                f"{s.state.value:9}  score {s.score:.2f}  "
                f"error yaw {s.error_deg[0]:+5.1f} pitch {s.error_deg[1]:+5.1f} deg  "
                f"command {s.command}"
            )
    except KeyboardInterrupt:
        pass
    finally:
        await lock.release()
        await stream.stop()
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
