"""Tests for the dashboard's protocol tap and command catalog."""

from __future__ import annotations

import pytest

from siyi_sdk.constants import CMD_REQUEST_FIRMWARE_VERSION, CMD_REQUEST_GIMBAL_ATTITUDE
from siyi_sdk.protocol.frame import Frame
from siyi_sdk.transport.mock import MockTransport

pytest.importorskip("fastapi")
from web_ui.frame_tap import TapTransport  # noqa: E402


async def _drain(tap: TapTransport, count: int) -> list[bytes]:
    chunks = []
    async for chunk in tap.stream():
        chunks.append(chunk)
        if len(chunks) == count:
            break
    return chunks


async def test_records_tx_frames() -> None:
    inner = MockTransport()
    tap = TapTransport(inner)
    await tap.connect()
    raw = Frame.build(CMD_REQUEST_FIRMWARE_VERSION, b"", seq=7).to_bytes()
    await tap.send(raw)

    assert inner.sent_frames == [raw]
    [record] = tap.records
    assert record["dir"] == "tx"
    assert record["cmd"] == "REQUEST_FIRMWARE_VERSION"
    assert record["seq"] == 7
    assert record["hex"] == raw.hex(" ")
    assert record["push"] is False


async def test_decodes_rx_frame_split_across_chunks() -> None:
    inner = MockTransport()
    tap = TapTransport(inner)
    await tap.connect()
    raw = Frame.build(CMD_REQUEST_GIMBAL_ATTITUDE, bytes(12), seq=3, need_ack=False).to_bytes()
    inner.queue_response(raw[:5])
    inner.queue_response(raw[5:])

    chunks = await _drain(tap, 2)

    assert b"".join(chunks) == raw  # Bytes pass through unchanged.
    [record] = tap.records
    assert record["dir"] == "rx"
    assert record["cmd_id"] == CMD_REQUEST_GIMBAL_ATTITUDE
    assert record["push"] is True
    assert tap.between(0, float("inf")) == []  # Pushes are excluded from per-command traces.


async def test_records_parse_errors_and_since() -> None:
    inner = MockTransport()
    tap = TapTransport(inner)
    await tap.connect()
    raw = bytearray(Frame.build(CMD_REQUEST_FIRMWARE_VERSION, b"", seq=1).to_bytes())
    raw[-1] ^= 0xFF  # Corrupt the CRC.
    inner.queue_response(bytes(raw))

    await _drain(tap, 1)

    [record] = tap.records
    assert record["dir"] == "err"
    assert record["error"].startswith("rx:")
    assert tap.since(record["id"]) == []
    assert tap.since(0) == [record]
    assert tap.last_id == record["id"]


def test_command_catalog_marks_read_only_commands() -> None:
    pytest.importorskip("cv2")
    testclient = pytest.importorskip("fastapi.testclient")
    from web_ui.server import app

    commands = {item["name"]: item for item in testclient.TestClient(app).get("/api/sdk/commands").json()}
    assert commands["get_firmware_version"]["read_only"] is True
    assert commands["rotate"]["read_only"] is False
    assert commands["format_sd_card"]["read_only"] is False
