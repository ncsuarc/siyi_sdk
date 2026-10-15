"""Run the camera service against the test suite's fake camera, for the TypeScript tests.

Prints ``PORT <n>`` once listening and runs until stdin closes.

    python clients/typescript/test/fake_service.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from siyi_sdk import SIYIClient  # noqa: E402
from siyi_sdk.remote import CameraServer, CameraSession, LockConfig  # noqa: E402
from tests.remote.fake_camera import FakeCamera  # noqa: E402


async def main() -> None:
    """Serve until stdin closes."""
    client = SIYIClient(FakeCamera(), default_timeout=0.3, max_retries=0)
    session = CameraSession(client=client, config=LockConfig(), video="none")
    server = CameraServer(session, host="127.0.0.1", port=0)
    await server.start()
    while not session.camera_alive:  # noqa: ASYNC110 - no event to wait on
        await asyncio.sleep(0.01)
    print(f"PORT {server.port}", flush=True)
    try:
        await asyncio.get_running_loop().run_in_executor(None, sys.stdin.read)
    finally:
        await server.close()
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
