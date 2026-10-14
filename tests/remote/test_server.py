"""Camera service end to end: RemoteCamera -> websocket -> CameraServer -> fake camera."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import pytest
import pytest_asyncio

from siyi_sdk import SIYIClient, exceptions
from siyi_sdk.models import FirmwareVersion, GimbalAttitude, SetAttitudeAck
from siyi_sdk.remote import (
    CameraServer,
    CameraSession,
    LockConfig,
    NotControllerError,
    RemoteCamera,
    ServiceUnavailableError,
    UnknownMethodError,
)
from tests.remote.fake_camera import FakeCamera


@dataclass
class Service:
    camera: FakeCamera
    session: CameraSession
    server: CameraServer

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.server.port}"


async def wait_for(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


@pytest_asyncio.fixture
async def service(request: pytest.FixtureRequest) -> AsyncIterator[Service]:
    options = getattr(request, "param", {})
    camera = FakeCamera()
    client = SIYIClient(camera, default_timeout=0.3, max_retries=0)
    session = CameraSession(
        client=client,
        config=LockConfig(),
        video="none",
        setpoint_timeout=options.get("setpoint_timeout", 0.3),
    )
    server = CameraServer(
        session,
        host="127.0.0.1",
        port=0,
        on_disconnect=options.get("on_disconnect", "keep-lock"),
    )
    await server.start()
    # The session connects and starts the attitude stream in the background.
    await wait_for(lambda: session.camera_alive)
    try:
        yield Service(camera, session, server)
    finally:
        await server.close()
        await client.close()


async def test_commands_return_sdk_types(service: Service) -> None:
    async with RemoteCamera(service.url) as cam:
        version = await cam.get_firmware_version()
        assert isinstance(version, FirmwareVersion)
        ack = await cam.set_attitude(15.0, -30.0)
        assert isinstance(ack, SetAttitudeAck)
        assert service.camera.angles[-1] == (15.0, -30.0)
        assert isinstance(await cam.get_gimbal_attitude(), GimbalAttitude)


async def test_errors_raise_sdk_exceptions(service: Service) -> None:
    async with RemoteCamera(service.url) as cam:
        with pytest.raises(exceptions.ConfigurationError):
            await cam.absolute_zoom(9.0)
        with pytest.raises(exceptions.ConfigurationError, match="confirm"):
            await cam.format_sd_card()
        with pytest.raises(UnknownMethodError):
            await cam.call("rotate", yaw=50, pitch=0)  # speed commands only as setpoints
        service.camera.alive = False
        with pytest.raises(exceptions.TimeoutError):
            await cam.get_gimbal_attitude()


async def test_held_rate_keeps_turning_and_zero_stops(service: Service) -> None:
    camera = service.camera
    async with RemoteCamera(service.url) as cam:
        cam.rate(40, -10)
        await wait_for(lambda: camera.last_rate == (40, -10))
        # Held well past the 0.3 s setpoint timeout: the client keeps it alive.
        await asyncio.sleep(0.8)
        assert camera.last_rate == (40, -10)
        cam.rate(0, 0)
        await wait_for(lambda: camera.last_rate == (0, 0))


async def test_setpoint_stops_when_updates_stop(service: Service) -> None:
    camera = service.camera
    async with RemoteCamera(service.url) as cam:
        sent = time.monotonic()
        await cam._notify("rate", yaw=50, pitch=0)  # one update, never repeated
        await wait_for(lambda: camera.last_rate == (50, 0))
        await wait_for(lambda: camera.last_rate == (0, 0), timeout=1.0)
        assert 0.25 <= time.monotonic() - sent <= 0.6


@pytest.mark.parametrize("service", [{"setpoint_timeout": 5.0}], indirect=True)
async def test_controller_disconnect_stops_motion(service: Service) -> None:
    camera = service.camera
    cam = RemoteCamera(service.url, reconnect=False)
    await cam.connect()
    cam.rate(30, 0)
    await wait_for(lambda: camera.last_rate == (30, 0))
    # Drop the link without the client's own clean stop.
    assert cam._connection is not None
    await cam._connection.close()
    await wait_for(lambda: camera.last_rate == (0, 0), timeout=1.0)
    await cam.close()


async def test_second_client_observes_until_it_takes_control(service: Service) -> None:
    async with (
        RemoteCamera(service.url, name="pilot") as pilot,
        RemoteCamera(service.url, name="observer") as observer,
    ):
        assert pilot.is_controller
        await wait_for(lambda: observer.control_holder == "pilot")
        assert not observer.is_controller
        assert isinstance(await observer.get_gimbal_attitude(), GimbalAttitude)
        with pytest.raises(NotControllerError):
            await observer.set_attitude(0.0, 0.0)
        with pytest.raises(NotControllerError):
            await observer.take_control()
        observer.rate(60, 0)  # ignored: not the controller
        await asyncio.sleep(0.2)
        assert (60, 0) not in service.camera.rates
        observer.rate(0, 0)

        await observer.take_control(force=True)
        assert observer.is_controller
        await wait_for(lambda: not pilot.is_controller)
        await observer.set_attitude(1.0, -1.0)


async def test_events_reach_the_ground(service: Service) -> None:
    attitudes: list[GimbalAttitude] = []
    links: list[dict[str, object]] = []
    async with RemoteCamera(service.url) as cam:
        cam.on_attitude(attitudes.append)
        cam.on_link(links.append)
        await asyncio.sleep(1.0)
        state = await cam.get_state()
    # The camera streams at 50 Hz; the service forwards at most 20 Hz.
    assert 8 <= len(attitudes) <= 25
    assert all(isinstance(a, GimbalAttitude) for a in attitudes)
    assert state["camera"]["alive"] is True
    assert state["control"]["controller"] is True


async def test_camera_loss_is_reported(service: Service) -> None:
    links: list[dict[str, object]] = []
    async with RemoteCamera(service.url) as cam:
        cam.on_link(links.append)
        service.camera.alive = False
        await wait_for(lambda: any(link.get("camera") is False for link in links), timeout=3.0)
        service.camera.alive = True
        await wait_for(lambda: links[-1].get("camera") is True, timeout=5.0)


async def test_lock_needs_video(service: Service) -> None:
    async with RemoteCamera(service.url) as cam:
        with pytest.raises(ServiceUnavailableError):
            await cam.lock(0.0, 0.0)
        with pytest.raises(exceptions.ConfigurationError):
            await cam.lock(0.9, 0.0)


async def test_clock_sync(service: Service) -> None:
    async with RemoteCamera(service.url) as cam:
        await cam.ping()
        assert cam.rtt is not None and cam.rtt < 0.5
        # Same machine, same monotonic clock.
        assert cam.clock_offset is not None and abs(cam.clock_offset) < 0.05


async def test_lock_settings_roundtrip(service: Service) -> None:
    async with RemoteCamera(service.url) as cam:
        config = await cam.set_lock_config(control="rate", response=0.8)
        assert config["control"] == "rate"
        assert service.session.config.response == 0.8
        with pytest.raises(exceptions.ConfigurationError):
            await cam.set_lock_config(control="sideways")
        with pytest.raises(exceptions.ConfigurationError):
            await cam.set_lock_config(no_such_setting=1)
