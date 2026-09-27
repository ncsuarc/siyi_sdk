"""Real aiortsp/PyAV integration using local recorded H.264 RTP sessions."""

import asyncio

import pytest

pytest.importorskip("av")
pytest.importorskip("aiortsp")

from siyi_sdk.stream import SIYIStream, StreamBackend, StreamConfig, StreamState

from .rtsp_server import LocalRTSP


@pytest.mark.parametrize("transport", ["tcp", "udp"])
@pytest.mark.parametrize("packetization", ["single", "stap", "fu"])
async def test_real_decode_sdp_parameters_transport_and_faults(transport, packetization):
    async with LocalRTSP(packetization, faults=True) as server:
        stream = SIYIStream(
            StreamConfig(server.url, backend=StreamBackend.AIORTSP, transport=transport)
        )
        try:
            await stream.start()
            assert stream.is_running
            image = stream.last_frame
            assert (image.width, image.height) == (64, 48)
            assert image.frame[:, :, 2].mean() > 210
            assert image.frame[:, :, :2].mean() < 10
            assert ("RTP/AVP/TCP" in server.setup_headers[0]) == (transport == "tcp")
        finally:
            await stream.stop()
        assert stream.state is StreamState.STOPPED


async def test_five_second_decoded_stall_reconnects_and_tears_down():
    async with LocalRTSP("fu", stall=True) as server:
        stream = SIYIStream(
            StreamConfig(server.url, backend=StreamBackend.AIORTSP, reconnect_delay=0.01)
        )
        try:
            await stream.start()

            async def reconnected():
                while server.sessions < 2:  # noqa: ASYNC110 - intentional predicate polling
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(reconnected(), 7)
        finally:
            await stream.stop()
        assert stream._backend is None and stream._task is None
