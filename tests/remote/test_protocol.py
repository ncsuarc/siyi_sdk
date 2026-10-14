"""Wire format: SDK values and exceptions survive the trip unchanged."""

from __future__ import annotations

import json

import pytest

from siyi_sdk import exceptions
from siyi_sdk.models import (
    CaptureFuncType,
    EncodingParams,
    FunctionFeedback,
    GimbalAttitude,
    HardwareID,
    StreamType,
    VideoEncType,
)
from siyi_sdk.remote import protocol, registry
from siyi_sdk.remote.protocol import (
    ConflictError,
    NotControllerError,
    RemoteError,
    decode_error,
    decode_value,
    encode_error,
    encode_value,
)


def roundtrip(value: object) -> object:
    return decode_value(json.loads(json.dumps(encode_value(value))))


class TestValues:
    def test_models_and_enums_keep_their_types(self) -> None:
        att = GimbalAttitude(10.5, -20.0, 0.1, 1.0, 2.0, 3.0)
        assert roundtrip(att) == att
        assert roundtrip(FunctionFeedback.PHOTO_OK) is FunctionFeedback.PHOTO_OK
        params = EncodingParams(
            stream_type=StreamType.MAIN,
            enc_type=VideoEncType.H265,
            resolution_w=1920,
            resolution_h=1080,
            bitrate_kbps=4000,
            frame_rate=30,
        )
        decoded = roundtrip(params)
        assert decoded == params
        assert isinstance(decoded.stream_type, StreamType)  # type: ignore[attr-defined]

    def test_bytes_and_nesting(self) -> None:
        hw = HardwareID(raw=b"\x01\x02\xff")
        assert roundtrip({"ids": [hw], "n": 3}) == {"ids": [hw], "n": 3}

    def test_unknown_tags_stay_plain(self) -> None:
        assert decode_value({"$type": "os.system", "x": 1}) == {"x": 1}
        assert decode_value({"$enum": "Nope", "name": "A", "value": 2}) == 2


class TestErrors:
    @pytest.mark.parametrize(
        ("exc", "kind"),
        [
            (exceptions.TimeoutError(cmd_id=0x0D, timeout_s=2.0), "camera_timeout"),
            (exceptions.UnsupportedCommandError(cmd_id=0x81), "unsupported"),
            (exceptions.ResponseError(cmd_id=0x21, sta=0), "rejected"),
            (exceptions.NACKError(cmd_id=0x0E, error_code=3, message="no"), "rejected"),
            (exceptions.ConfigurationError("zoom too high"), "bad_args"),
            (exceptions.NotConnectedError("down"), "camera_offline"),
            (NotControllerError("someone else"), "not_controller"),
            (ConflictError("busy"), "conflict"),
        ],
    )
    def test_sdk_exceptions_are_rebuilt(self, exc: BaseException, kind: str) -> None:
        encoded = json.loads(json.dumps(encode_error(exc)))
        assert encoded["data"]["kind"] == kind
        rebuilt = decode_error(encoded)
        assert type(rebuilt) is type(exc)
        assert str(rebuilt) == str(exc)

    def test_value_error_becomes_configuration_error(self) -> None:
        rebuilt = decode_error(encode_error(ValueError("x must be a number")))
        assert isinstance(rebuilt, exceptions.ConfigurationError)
        assert encode_error(ValueError("x"))["code"] == protocol.INVALID_PARAMS

    def test_unexpected_exceptions_become_remote_error(self) -> None:
        rebuilt = decode_error(encode_error(KeyError("boom")))
        assert type(rebuilt) is RemoteError

    def test_unknown_type_name_is_not_instantiated(self) -> None:
        rebuilt = decode_error({"message": "hi", "data": {"type": "SystemExit"}})
        assert type(rebuilt) is RemoteError


class TestRegistry:
    def test_speed_commands_are_not_remote(self) -> None:
        for name in ("rotate", "rotate_nowait", "manual_zoom", "manual_zoom_nowait", "heartbeat"):
            assert name in registry.COMMANDS
            assert name not in registry.REMOTE_COMMANDS
        assert "set_attitude" in registry.REMOTE_COMMANDS

    def test_unsupported_firmware_commands_are_absent(self) -> None:
        assert "set_ip_config" not in registry.COMMANDS
        assert "get_system_time" not in registry.COMMANDS

    def test_arguments_are_validated(self) -> None:
        command = registry.COMMANDS["capture"]
        parsed = command.input_model.model_validate({"func": 0})
        assert parsed.func is CaptureFuncType.PHOTO  # type: ignore[attr-defined]
        with pytest.raises(ValueError):
            command.input_model.model_validate({"func": 0, "extra": 1})
        with pytest.raises(ValueError):
            registry.COMMANDS["set_attitude"].input_model.model_validate({"yaw_deg": "left"})
