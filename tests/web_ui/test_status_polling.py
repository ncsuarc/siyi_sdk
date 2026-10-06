"""The dashboard leaves the camera alone while the firmware link streams its video."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("cv2")
pytest.importorskip("numpy")

from web_ui import server


def camera(*, link: bool, status_age: float | None = None, telemetry_age: float | None = None):
    state = server.CameraState()
    state.is_connected = True
    state.firmware_version = server.FirmwareVersion(0x0307, 0x0409, 0)
    if link:
        state.firmware_link = SimpleNamespace(ready=True)
    now = time.monotonic()
    if status_age is not None:
        state.status_time = now - status_age
    if telemetry_age is not None:
        state.attitude_time = now - telemetry_age
    return state


async def test_status_is_polled_every_second_without_the_link():
    assert camera(link=False, status_age=1.0).status_due()
    assert camera(link=False).status_due()


async def test_status_is_polled_rarely_while_the_link_streams():
    assert not camera(link=True, status_age=1.0).status_due()
    assert not camera(link=True, status_age=server.LINK_STATUS_INTERVAL - 1).status_due()
    assert camera(link=True, status_age=server.LINK_STATUS_INTERVAL + 0.1).status_due()
    assert camera(link=True).status_due()  # but the first reading is not skipped


async def test_poll_loop_skips_the_query_while_the_link_streams(monkeypatch):
    state = camera(link=True, status_age=1.0)
    queries = 0

    async def refresh_status():
        nonlocal queries
        queries += 1

    async def stop_after_one_pass(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(state, "refresh_status", refresh_status)
    monkeypatch.setattr(server.asyncio, "sleep", stop_after_one_pass)
    with pytest.raises(asyncio.CancelledError):
        await state.poll_status()
    assert queries == 0
    state.firmware_link = None  # the link drops: polling resumes at once
    with pytest.raises(asyncio.CancelledError):
        await state.poll_status()
    assert queries == 1


async def test_a_slow_status_still_counts_as_fresh_while_the_link_streams():
    assert camera(link=True, status_age=8.0).status_snapshot()["status_fresh"]
    assert not camera(link=False, status_age=8.0).status_snapshot()["status_fresh"]
    assert camera(link=False, status_age=1.0).status_snapshot()["status_fresh"]
    stale = server.LINK_STATUS_INTERVAL + 4
    assert not camera(link=True, status_age=stale).status_snapshot()["status_fresh"]


async def test_telemetry_stands_in_for_the_watchdog_ping_only_while_everything_is_up():
    assert camera(link=True, telemetry_age=0.05).link_telemetry_alive()
    assert not camera(link=True, telemetry_age=2.0).link_telemetry_alive()  # telemetry stopped
    assert not camera(link=True).link_telemetry_alive()  # none seen yet
    assert not camera(link=False, telemetry_age=0.05).link_telemetry_alive()  # no link
    unknown_version = camera(link=True, telemetry_age=0.05)
    unknown_version.firmware_version = None
    assert not unknown_version.link_telemetry_alive()
    disconnected = camera(link=True, telemetry_age=0.05)
    disconnected.is_connected = False
    assert not disconnected.link_telemetry_alive()
