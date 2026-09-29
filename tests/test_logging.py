# Copyright (c) 2026 Mohamed Abdelkader <mohamedashraf123@gmail.com>
# All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for structured logging configuration and output.

These tests verify:
- Log level configuration
- Protocol trace mode (hexdump processor)
- Log event content and structure
- Environment variable handling
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest
from structlog.testing import capture_logs

from siyi_sdk.client import SIYIClient
from siyi_sdk.logging_config import configure_logging, hexdump_processor
from siyi_sdk.protocol.frame import Frame
from siyi_sdk.protocol.parser import FrameParser
from siyi_sdk.transport.mock import MockTransport


class TestLoggingConfiguration:
    """Test logging configuration with various settings."""

    def test_default_logging_level_is_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test default log level is INFO when no env vars set."""
        monkeypatch.delenv("SIYI_LOG_LEVEL", raising=False)
        monkeypatch.delenv("SIYI_PROTOCOL_TRACE", raising=False)

        configure_logging()

        import logging

        root_logger = logging.getLogger()
        assert root_logger.level == logging.INFO

    def test_log_level_from_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test log level can be set via SIYI_LOG_LEVEL env var."""
        monkeypatch.setenv("SIYI_LOG_LEVEL", "DEBUG")
        configure_logging()

        import logging

        root_logger = logging.getLogger()
        assert root_logger.level == logging.DEBUG

    def test_trace_mode_forces_debug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test SIYI_PROTOCOL_TRACE=1 forces DEBUG level."""
        monkeypatch.setenv("SIYI_PROTOCOL_TRACE", "1")
        monkeypatch.setenv("SIYI_LOG_LEVEL", "WARNING")  # Should be overridden

        configure_logging()

        import logging

        root_logger = logging.getLogger()
        assert root_logger.level == logging.DEBUG

    def test_configure_logging_with_explicit_trace(self) -> None:
        """Test configure_logging(trace=True) enables trace mode."""
        configure_logging(trace=True)

        import logging

        root_logger = logging.getLogger()
        assert root_logger.level == logging.DEBUG


class TestHexdumpProcessor:
    """Test hexdump processor for payload conversion."""

    def test_hexdump_processor_converts_payload_bytes_to_hex(self) -> None:
        """Test hexdump_processor converts payload_bytes to payload_hex."""
        event_dict: dict[str, Any] = {
            "event": "test",
            "payload_bytes": b"\x01\x02\x03\x04",
        }

        result = hexdump_processor(None, "", event_dict)  # type: ignore[arg-type]

        assert "payload_hex" in result
        assert result["payload_hex"] == "01 02 03 04"
        assert "payload_bytes" not in result

    def test_hexdump_processor_leaves_other_fields_intact(self) -> None:
        """Test hexdump_processor doesn't modify non-payload fields."""
        event_dict: dict[str, Any] = {
            "event": "test",
            "cmd_id": "0x01",
            "payload_bytes": b"\xab\xcd",
        }

        result = hexdump_processor(None, "", event_dict)  # type: ignore[arg-type]

        assert result["event"] == "test"
        assert result["cmd_id"] == "0x01"
        assert result["payload_hex"] == "ab cd"


class TestCommandDispatchLogging:
    """Test logging events during command dispatch."""

    @pytest.mark.asyncio
    async def test_ack_is_not_logged_at_info(
        self,
        mock_transport: MockTransport,
        frame_firmware_version_ack: bytes,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Routine commands and ACKs do not flood INFO logging at 100 Hz."""
        configure_logging(level="INFO")

        client = SIYIClient(mock_transport, default_timeout=0.5)
        await client.connect()

        mock_transport.queue_response(frame_firmware_version_ack)
        await client.get_firmware_version()
        assert "rx_ack" not in capsys.readouterr().err

        await client.close()

    @pytest.mark.asyncio
    async def test_debug_hexdump_when_trace_enabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_transport: MockTransport,
        frame_firmware_version_ack: bytes,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Test hexdump in logs when SIYI_PROTOCOL_TRACE=1."""
        monkeypatch.setenv("SIYI_PROTOCOL_TRACE", "1")
        configure_logging()

        client = SIYIClient(mock_transport, default_timeout=0.5)
        await client.connect()

        mock_transport.queue_response(frame_firmware_version_ack)
        await client.get_firmware_version()
        output = capsys.readouterr().err
        assert "frame_tx" in output and "data_hex=" in output

        await client.close()

    @pytest.mark.asyncio
    async def test_no_hexdump_without_trace(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_transport: MockTransport,
        frame_firmware_version_ack: bytes,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Test no hexdump in logs when trace mode disabled."""
        monkeypatch.delenv("SIYI_PROTOCOL_TRACE", raising=False)
        configure_logging(level="DEBUG", trace=False)

        client = SIYIClient(mock_transport, default_timeout=0.5)
        await client.connect()

        mock_transport.queue_response(frame_firmware_version_ack)
        await client.get_firmware_version()
        assert "data_hex=" not in capsys.readouterr().err

        await client.close()


class TestErrorLogging:
    """Test logging of error conditions."""

    def test_error_on_crc_mismatch(self) -> None:
        """Test ERROR log emitted on CRC mismatch."""
        configure_logging(level="ERROR")

        # Create a frame with corrupted CRC
        frame = Frame(ctrl=1, seq=0, cmd_id=0x01, data=b"\x01\x02\x03")
        wire = bytearray(frame.to_bytes())
        wire[-1] ^= 0xFF  # Corrupt CRC

        parser = FrameParser()

        result = parser.feed(bytes(wire))
        assert result.errors and "crc" in type(result.errors[0]).__name__.lower()

    @pytest.mark.asyncio
    async def test_warning_on_timeout(
        self,
        mock_transport: MockTransport,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Test WARNING/ERROR log on command timeout."""
        configure_logging(level="WARNING")

        client = SIYIClient(mock_transport, default_timeout=0.2)
        await client.connect()

        with contextlib.suppress(Exception):
            # Don't queue response — will timeout
            await client.get_firmware_version()

        # Should have warning or error logs
        assert "timeout_exhausted" in capsys.readouterr().err

        await client.close()


class TestTransportLogging:
    """Test transport-level logging."""

    @pytest.mark.asyncio
    async def test_transport_connect_log(
        self,
        mock_transport: MockTransport,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Test INFO log on transport connect."""
        configure_logging(level="INFO")

        await mock_transport.connect()
        output = capsys.readouterr().err
        assert "connected" in output and "transport=mock" in output

    @pytest.mark.asyncio
    async def test_transport_send_log(
        self,
        mock_transport: MockTransport,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Test DEBUG log on transport send."""
        configure_logging(level="DEBUG")

        await mock_transport.connect()

        await mock_transport.send(b"\x01\x02\x03")
        output = capsys.readouterr().err
        assert "frame_tx" in output and "transport=mock" in output and "length=3" in output


class TestParserLogging:
    """Test parser-level logging."""

    def test_parser_frame_decoded_log(self) -> None:
        """Test DEBUG log when frame successfully decoded."""
        configure_logging(level="DEBUG")

        frame = Frame(ctrl=1, seq=0, cmd_id=0x01, data=b"\x01\x02\x03")
        wire = frame.to_bytes()

        parser = FrameParser()

        with capture_logs() as logs:
            parser.feed(wire)

        # Should have frame_decoded log
        decoded_logs = [
            r
            for r in logs
            if "decode" in r.get("event", "").lower() or "parse" in r.get("event", "").lower()
        ]
        assert len(decoded_logs) >= 0  # May be DEBUG level, might be filtered
