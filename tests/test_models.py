# Copyright (c) 2026 Mohamed Abdelkader <mohamedashraf123@gmail.com>
# All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for siyi_sdk.models module."""

from __future__ import annotations

from dataclasses import fields
from ipaddress import IPv4Address

import pytest

from siyi_sdk import models


class TestProductID:
    """Test ProductID enumeration."""

    def test_a8_mini(self):
        """A8_MINI should have value 0x73."""
        assert models.ProductID.A8_MINI == 0x73
        assert models.ProductID(0x73).name == "A8_MINI"


class TestGimbalMotionMode:
    """Test GimbalMotionMode enumeration."""

    def test_values(self):
        """GimbalMotionMode values should match spec."""
        assert models.GimbalMotionMode.LOCK == 0
        assert models.GimbalMotionMode.FOLLOW == 1
        assert models.GimbalMotionMode.FPV == 2


class TestMountingDirection:
    """Test MountingDirection enumeration."""

    def test_values(self):
        """MountingDirection values should match spec."""
        assert models.MountingDirection.RESERVED == 0
        assert models.MountingDirection.NORMAL == 1
        assert models.MountingDirection.INVERTED == 2


class TestHDMICVBSOutput:
    """Test HDMICVBSOutput enumeration."""

    def test_values(self):
        """HDMICVBSOutput values should match spec."""
        assert models.HDMICVBSOutput.HDMI_ON_CVBS_OFF == 0
        assert models.HDMICVBSOutput.HDMI_OFF_CVBS_ON == 1


class TestRecordingState:
    """Test RecordingState enumeration."""

    def test_values(self):
        """RecordingState values should match spec."""
        assert models.RecordingState.NOT_RECORDING == 0
        assert models.RecordingState.RECORDING == 1
        assert models.RecordingState.NO_TF_CARD == 2
        assert models.RecordingState.DATA_LOSS == 3


class TestFunctionFeedback:
    """Test FunctionFeedback enumeration."""

    def test_values(self):
        """FunctionFeedback values should match spec."""
        assert models.FunctionFeedback.PHOTO_OK == 0
        assert models.FunctionFeedback.PHOTO_FAILED == 1
        assert models.FunctionFeedback.HDR_ON == 2
        assert models.FunctionFeedback.HDR_OFF == 3
        assert models.FunctionFeedback.RECORDING_FAILED == 4
        assert models.FunctionFeedback.RECORDING_STARTED == 5
        assert models.FunctionFeedback.RECORDING_STOPPED == 6


class TestCaptureFuncType:
    """Test CaptureFuncType enumeration."""

    def test_values(self):
        """CaptureFuncType values should match spec."""
        assert models.CaptureFuncType.PHOTO == 0
        assert models.CaptureFuncType.START_RECORD == 2
        assert models.CaptureFuncType.LOCK_MODE == 3
        assert models.CaptureFuncType.FOLLOW_MODE == 4
        assert models.CaptureFuncType.FPV_MODE == 5
        assert models.CaptureFuncType.ENABLE_HDMI == 6
        assert models.CaptureFuncType.ENABLE_CVBS == 7
        assert models.CaptureFuncType.DISABLE_HDMI_CVBS == 8
        assert models.CaptureFuncType.TILT_DOWNWARD == 9


class TestCenteringAction:
    """Test CenteringAction enumeration."""

    def test_values(self):
        """CenteringAction values should match spec."""
        assert models.CenteringAction.ONE_KEY_CENTER == 1
        assert models.CenteringAction.CENTER_DOWNWARD == 2
        assert models.CenteringAction.CENTER == 3
        assert models.CenteringAction.DOWNWARD == 4


class TestVideoEncType:
    """Test VideoEncType enumeration."""

    def test_values(self):
        """VideoEncType values should match spec."""
        assert models.VideoEncType.H264 == 1
        assert models.VideoEncType.H265 == 2


class TestStreamType:
    """Test StreamType enumeration."""

    def test_values(self):
        """StreamType values should match spec."""
        assert models.StreamType.RECORDING == 0
        assert models.StreamType.MAIN == 1
        assert models.StreamType.SUB == 2


class TestFCDataType:
    """Test FCDataType enumeration."""

    def test_values(self):
        """FCDataType values should match spec."""
        assert models.FCDataType.ATTITUDE == 1
        assert models.FCDataType.RC_CHANNELS == 2


class TestGimbalDataType:
    """Test GimbalDataType enumeration."""

    def test_values(self):
        """GimbalDataType values should match spec."""
        assert models.GimbalDataType.ATTITUDE == 1
        assert models.GimbalDataType.MAGNETIC_ENCODER == 3
        assert models.GimbalDataType.MOTOR_VOLTAGE == 4


class TestDataStreamFreq:
    """Test DataStreamFreq enumeration."""

    def test_values(self):
        """DataStreamFreq values should match spec."""
        assert models.DataStreamFreq.OFF == 0
        assert models.DataStreamFreq.HZ2 == 1
        assert models.DataStreamFreq.HZ4 == 2
        assert models.DataStreamFreq.HZ5 == 3
        assert models.DataStreamFreq.HZ10 == 4
        assert models.DataStreamFreq.HZ20 == 5
        assert models.DataStreamFreq.HZ50 == 6
        assert models.DataStreamFreq.HZ100 == 7


class TestControlMode:
    """Test ControlMode enumeration."""

    def test_values(self):
        """ControlMode values should match spec."""
        assert models.ControlMode.ATTITUDE == 0
        assert models.ControlMode.WEAK == 1
        assert models.ControlMode.MIDDLE == 2
        assert models.ControlMode.FPV == 3
        assert models.ControlMode.MOTOR_CLOSE == 4


class TestFileType:
    """Test FileType enumeration."""

    def test_values(self):
        """FileType values should match spec."""
        assert models.FileType.PICTURE == 0
        assert models.FileType.RECORD_VIDEO == 2


class TestFileNameType:
    """Test FileNameType enumeration."""

    def test_values(self):
        """FileNameType values should match spec."""
        assert models.FileNameType.RESERVE == 0
        assert models.FileNameType.INDEX == 1
        assert models.FileNameType.TIMESTAMP == 2

    def test_picname_alias(self):
        """PicName should be an alias for FileNameType."""
        assert models.PicName is models.FileNameType


class TestFirmwareVersion:
    """Test FirmwareVersion dataclass."""

    def test_frozen(self):
        """FirmwareVersion should be frozen."""
        fw = models.FirmwareVersion(camera=1, gimbal=2, zoom=3)
        with pytest.raises(AttributeError):
            fw.camera = 4  # type: ignore

    def test_slots(self):
        """FirmwareVersion should have slots."""
        assert hasattr(models.FirmwareVersion, "__slots__")

    def test_decode_word(self):
        """decode_word should extract major.minor.patch from word."""
        # Example from spec: 0x6E030203 -> v3.2.3 (high byte 0x6E ignored)
        # Actually: patch=3, minor=2, major=3 for 0x030203
        major, minor, patch = models.FirmwareVersion.decode_word(0x6E030203)
        assert major == 3
        assert minor == 2
        assert patch == 3

    def test_decode_word_simple(self):
        """decode_word with simple version number."""
        major, minor, patch = models.FirmwareVersion.decode_word(0x00010203)
        assert major == 1
        assert minor == 2
        assert patch == 3


class TestHardwareID:
    """Test HardwareID dataclass."""

    def test_frozen(self):
        """HardwareID should be frozen."""
        hw = models.HardwareID(raw=b"6b" + b"\x00" * 10)
        with pytest.raises(AttributeError):
            hw.raw = b"test"  # type: ignore

    def test_slots(self):
        """HardwareID should have slots."""
        assert hasattr(models.HardwareID, "__slots__")

    def test_product_id_a8_mini(self):
        """product_id property should return ProductID for A8_MINI."""
        hw = models.HardwareID(raw=b"73" + b"\x00" * 10)
        assert hw.product_id == models.ProductID.A8_MINI


class TestZoomRange:
    """Test ZoomRange dataclass."""

    def test_max_zoom_property(self):
        """max_zoom property should combine integer and decimal parts."""
        zr = models.ZoomRange(max_int=30, max_float=5)
        assert zr.max_zoom == 30.5

    def test_max_zoom_whole(self):
        """max_zoom with zero decimal part."""
        zr = models.ZoomRange(max_int=10, max_float=0)
        assert zr.max_zoom == 10.0


class TestCurrentZoom:
    """Test CurrentZoom dataclass."""

    def test_zoom_property(self):
        """zoom property should combine integer and decimal parts."""
        cz = models.CurrentZoom(integer=5, decimal=3)
        assert cz.zoom == 5.3

    def test_zoom_whole(self):
        """zoom with zero decimal part."""
        cz = models.CurrentZoom(integer=10, decimal=0)
        assert cz.zoom == 10.0


class TestAngleLimits:
    """Test AngleLimits dataclass and ANGLE_LIMITS table."""

    def test_angle_limits_has_all_products(self):
        """ANGLE_LIMITS should have entry for every ProductID."""
        for product in models.ProductID:
            assert product in models.ANGLE_LIMITS, f"Missing {product.name}"

    def test_a8_mini_limits(self):
        """A8 Mini angle limits should match its documented control range."""
        limits = models.ANGLE_LIMITS[models.ProductID.A8_MINI]
        assert limits.yaw_min == -135.0
        assert limits.yaw_max == 135.0
        assert limits.pitch_min == -90.0
        assert limits.pitch_max == 25.0


_DATACLASSES = [
    models.FirmwareVersion,
    models.HardwareID,
    models.CameraSystemInfo,
    models.GimbalAttitude,
    models.SetAttitudeAck,
    models.AircraftAttitude,
    models.EncodingParams,
    models.ZoomRange,
    models.CurrentZoom,
    models.RawGPS,
    models.MagneticEncoderAngles,
    models.MotorVoltage,
    models.WeakControlThreshold,
    models.SystemTime,
    models.GimbalSystemInfo,
    models.IPConfig,
    models.AngleLimits,
]


class TestDataclassProperties:
    """Test that all dataclasses have required properties."""

    @pytest.mark.parametrize("cls", _DATACLASSES)
    def test_has_slots(self, cls):
        """Dataclass should have __slots__."""
        assert hasattr(cls, "__slots__"), f"{cls.__name__} missing __slots__"

    @pytest.mark.parametrize("cls", _DATACLASSES)
    def test_is_frozen(self, cls):
        """Dataclass should be frozen (immutable)."""
        # Get field info to create an instance
        field_info = fields(cls)
        # Create dummy values for each field
        values = {}
        for field in field_info:
            if field.type == "int" or field.type is int:
                values[field.name] = 0
            elif field.type == "float" or field.type is float:
                values[field.name] = 0.0
            elif field.type == "bytes" or field.type is bytes:
                values[field.name] = b""
            elif field.type == "bool" or field.type is bool:
                values[field.name] = False
            elif "tuple" in str(field.type):
                values[field.name] = ()
            elif field.type == "IPv4Address" or field.type is IPv4Address:
                values[field.name] = IPv4Address("0.0.0.0")
            elif field.type == "float | None":
                values[field.name] = None
            elif hasattr(models, str(field.type).split(".")[-1].strip("'")):
                # It's an enum or dataclass - use first value or create instance
                type_name = str(field.type).split(".")[-1].strip("'")
                type_cls = getattr(models, type_name, None)
                if type_cls and hasattr(type_cls, "__members__"):
                    # It's an enum
                    values[field.name] = next(iter(type_cls.__members__.values()))
                elif type_cls:
                    # It's a dataclass - skip this test for nested dataclasses
                    pytest.skip(f"Skipping nested dataclass test for {cls.__name__}")
                else:
                    values[field.name] = 0
            else:
                values[field.name] = 0

        instance = cls(**values)
        first_field = field_info[0].name

        with pytest.raises(AttributeError):
            setattr(instance, first_field, values[first_field])
