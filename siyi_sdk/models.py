# Copyright (c) 2026 Mohamed Abdelkader <mohamedashraf123@gmail.com>
# All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Data models for the SIYI SDK protocol.

This module contains all enumerations and dataclasses representing
protocol-level data structures from the SIYI SDK specification.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from ipaddress import IPv4Address
from typing import Final

# =============================================================================
# Product and Hardware Enumerations
# =============================================================================


class ProductID(IntEnum):
    """Product identification codes (first byte of hardware ID)."""

    A8_MINI = 0x73

    @property
    def label(self) -> str:
        """Human-readable product name (e.g. 'A8 Mini')."""
        return "A8 Mini"


# =============================================================================
# Gimbal Mode Enumerations
# =============================================================================


class GimbalMotionMode(IntEnum):
    """Gimbal motion mode."""

    LOCK = 0
    FOLLOW = 1
    FPV = 2


class MountingDirection(IntEnum):
    """Gimbal mounting direction."""

    RESERVED = 0
    NORMAL = 1
    INVERTED = 2


class CenteringAction(IntEnum):
    """One-key centering action types."""

    ONE_KEY_CENTER = 1
    CENTER_DOWNWARD = 2
    CENTER = 3
    DOWNWARD = 4


class ControlMode(IntEnum):
    """Gimbal control mode (ArduPilot debugging)."""

    ATTITUDE = 0
    WEAK = 1
    MIDDLE = 2
    FPV = 3
    MOTOR_CLOSE = 4


# =============================================================================
# Video and Recording Enumerations
# =============================================================================


class HDMICVBSOutput(IntEnum):
    """HDMI/CVBS video output status."""

    HDMI_ON_CVBS_OFF = 0
    HDMI_OFF_CVBS_ON = 1


class RecordingState(IntEnum):
    """Recording status."""

    NOT_RECORDING = 0
    RECORDING = 1
    NO_TF_CARD = 2
    DATA_LOSS = 3


class FunctionFeedback(IntEnum):
    """Function feedback response codes."""

    PHOTO_OK = 0
    PHOTO_FAILED = 1
    HDR_ON = 2
    HDR_OFF = 3
    RECORDING_FAILED = 4
    RECORDING_STARTED = 5
    RECORDING_STOPPED = 6


class CaptureFuncType(IntEnum):
    """Capture photo / record video function types."""

    PHOTO = 0
    START_RECORD = 2
    LOCK_MODE = 3
    FOLLOW_MODE = 4
    FPV_MODE = 5
    ENABLE_HDMI = 6
    ENABLE_CVBS = 7
    DISABLE_HDMI_CVBS = 8
    TILT_DOWNWARD = 9


class VideoEncType(IntEnum):
    """Video encoding type."""

    H264 = 1
    H265 = 2


class StreamType(IntEnum):
    """Video stream type."""

    RECORDING = 0
    MAIN = 1
    SUB = 2




# =============================================================================
# Data Stream Enumerations
# =============================================================================


class FCDataType(IntEnum):
    """Flight controller data stream type."""

    ATTITUDE = 1
    RC_CHANNELS = 2


class GimbalDataType(IntEnum):
    """Gimbal data stream type."""

    ATTITUDE = 1
    MAGNETIC_ENCODER = 3
    MOTOR_VOLTAGE = 4


class DataStreamFreq(IntEnum):
    """Data stream output frequency."""

    OFF = 0
    HZ2 = 1
    HZ4 = 2
    HZ5 = 3
    HZ10 = 4
    HZ20 = 5
    HZ50 = 6
    HZ100 = 7




# =============================================================================
# File System Enumerations
# =============================================================================


class FileType(IntEnum):
    """File type for naming conventions."""

    PICTURE = 0
    RECORD_VIDEO = 2


class FileNameType(IntEnum):
    """File naming convention type."""

    RESERVE = 0
    INDEX = 1
    TIMESTAMP = 2


# Alias for backwards compatibility
PicName = FileNameType


# =============================================================================
# Firmware and Hardware Dataclasses
# =============================================================================


@dataclass(frozen=True, slots=True)
class FirmwareVersion:
    """Firmware version information.

    Attributes:
        camera: Camera firmware version as packed uint32.
        gimbal: Gimbal firmware version as packed uint32.
        zoom: Zoom module firmware version as packed uint32.

    """

    camera: int
    gimbal: int
    zoom: int

    @staticmethod
    def decode_word(word: int) -> tuple[int, int, int]:
        """Decode a firmware version word into major, minor, patch.

        The high byte is ignored per spec note. The low 3 bytes are
        (from LSB to MSB): patch, minor, major.

        Args:
            word: Raw uint32 firmware version word.

        Returns:
            Tuple of (major, minor, patch) version numbers.

        Example:
            >>> FirmwareVersion.decode_word(0x6E030203)
            (2, 2, 3)

        """
        patch = word & 0xFF
        minor = (word >> 8) & 0xFF
        major = (word >> 16) & 0xFF
        return (major, minor, patch)

    @staticmethod
    def format_word(word: int) -> str:
        """Format a firmware version word as a human-readable string.

        Args:
            word: Raw uint32 firmware version word. Zero means not present.

        Returns:
            String like 'v3.3.0', or 'n/a' if word is 0.

        """
        if word == 0:
            return "n/a"
        major, minor, patch = FirmwareVersion.decode_word(word)
        return f"v{major}.{minor}.{patch}"


@dataclass(frozen=True, slots=True)
class HardwareID:
    """Hardware identification.

    Attributes:
        raw: Raw 12-byte hardware ID string.

    """

    raw: bytes

    @property
    def product_id(self) -> ProductID:
        """Get the product identification from the first two bytes.

        The hardware ID is a 12-byte ASCII string. The first two characters
        are the product code in ASCII hex (e.g. b"73..." -> 0x73 = A8 Mini).

        Returns:
            ProductID enumeration value.

        Raises:
            ValueError: If the code is not a known product ID.

        """
        code = int(self.raw[0:2], 16)
        return ProductID(code)


# =============================================================================
# Camera System Information
# =============================================================================


@dataclass(frozen=True, slots=True)
class CameraSystemInfo:
    """Camera system information (0x0A response).

    Attributes:
        reserved_a: First reserved byte.
        hdr_sta: HDR status (0=off, 1=on).
        reserved_b: Second reserved byte.
        record_sta: Recording state.
        gimbal_motion_mode: Current gimbal motion mode.
        gimbal_mounting_dir: Gimbal mounting direction.
        video_hdmi_or_cvbs: HDMI/CVBS output status.
        zoom_linkage: Zoom linkage switch (0=off, 1=on).

    """

    reserved_a: int
    hdr_sta: int
    reserved_b: int
    record_sta: RecordingState
    gimbal_motion_mode: GimbalMotionMode
    gimbal_mounting_dir: MountingDirection
    video_hdmi_or_cvbs: HDMICVBSOutput
    zoom_linkage: int


# =============================================================================
# Attitude and Motion Dataclasses
# =============================================================================


@dataclass(frozen=True, slots=True)
class GimbalAttitude:
    """Gimbal attitude data (0x0D response).

    All angles are in degrees and rates in degrees per second.
    Raw int16 values are divided by 10.

    Attributes:
        yaw_deg: Yaw angle in degrees.
        pitch_deg: Pitch angle in degrees.
        roll_deg: Roll angle in degrees.
        yaw_rate_dps: Yaw angular velocity in degrees/second.
        pitch_rate_dps: Pitch angular velocity in degrees/second.
        roll_rate_dps: Roll angular velocity in degrees/second.

    """

    yaw_deg: float
    pitch_deg: float
    roll_deg: float
    yaw_rate_dps: float
    pitch_rate_dps: float
    roll_rate_dps: float


@dataclass(frozen=True, slots=True)
class SetAttitudeAck:
    """Set attitude acknowledgment (0x0E response).

    All angles are in degrees. Raw int16 values are divided by 10.

    Attributes:
        yaw_deg: Current yaw angle in degrees.
        pitch_deg: Current pitch angle in degrees.
        roll_deg: Current roll angle in degrees.

    """

    yaw_deg: float
    pitch_deg: float
    roll_deg: float


@dataclass(frozen=True, slots=True)
class AircraftAttitude:
    """Aircraft attitude data (0x22 send format).

    All angles are in radians and rates in radians per second.

    Attributes:
        time_boot_ms: Timestamp since system boot in milliseconds.
        roll_rad: Roll angle in radians (-pi to +pi).
        pitch_rad: Pitch angle in radians (-pi/2 to +pi/2).
        yaw_rad: Yaw angle in radians (-pi to +pi).
        rollspeed: Roll angular speed in rad/s.
        pitchspeed: Pitch angular speed in rad/s.
        yawspeed: Yaw angular speed in rad/s.

    """

    time_boot_ms: int
    roll_rad: float
    pitch_rad: float
    yaw_rad: float
    rollspeed: float
    pitchspeed: float
    yawspeed: float


@dataclass(frozen=True, slots=True)
class MagneticEncoderAngles:
    """Magnetic encoder angle data (0x26 response).

    All angles in degrees. Raw int16 values are divided by 10.

    Attributes:
        yaw: Yaw angle in degrees.
        pitch: Pitch angle in degrees.
        roll: Roll angle in degrees.

    """

    yaw: float
    pitch: float
    roll: float


@dataclass(frozen=True, slots=True)
class MotorVoltage:
    """Motor voltage data (0x2A response).

    All voltages in volts. Raw int16 values are divided by 1000.

    Attributes:
        yaw: Yaw motor voltage in volts.
        pitch: Pitch motor voltage in volts.
        roll: Roll motor voltage in volts.

    """

    yaw: float
    pitch: float
    roll: float


@dataclass(frozen=True, slots=True)
class WeakControlThreshold:
    """Weak control threshold data (0x28 response).

    All values have one decimal place precision.

    Attributes:
        limit: Weak control mode voltage limit (1.0-5.0).
        voltage: Voltage threshold (2.0-5.0).
        angular_error: Angular error threshold (3.0-30.0).

    """

    limit: float
    voltage: float
    angular_error: float


# =============================================================================
# Video Encoding
# =============================================================================


@dataclass(frozen=True, slots=True)
class EncodingParams:
    """Video encoding parameters (0x20 response).

    Attributes:
        stream_type: Stream type (recording/main/sub).
        enc_type: Encoding type (H.264/H.265).
        resolution_w: Resolution width in pixels.
        resolution_h: Resolution height in pixels.
        bitrate_kbps: Fixed bitrate in kbps.
        frame_rate: Frame rate in fps.

    """

    stream_type: StreamType
    enc_type: VideoEncType
    resolution_w: int
    resolution_h: int
    bitrate_kbps: int
    frame_rate: int






# =============================================================================
# Zoom Control
# =============================================================================


@dataclass(frozen=True, slots=True)
class ZoomRange:
    """Zoom range information (0x16 response).

    Attributes:
        max_int: Integer part of maximum zoom.
        max_float: Decimal part of maximum zoom (0-9).

    """

    max_int: int
    max_float: int

    @property
    def max_zoom(self) -> float:
        """Get the maximum zoom as a float.

        Returns:
            Maximum zoom value (e.g., 30.5 for max_int=30, max_float=5).

        """
        return self.max_int + self.max_float / 10


@dataclass(frozen=True, slots=True)
class CurrentZoom:
    """Current zoom magnification (0x18 response).

    Attributes:
        integer: Integer part of current zoom.
        decimal: Decimal part of current zoom (0-9).

    """

    integer: int
    decimal: int

    @property
    def zoom(self) -> float:
        """Get the current zoom as a float.

        Returns:
            Current zoom value (e.g., 5.3 for integer=5, decimal=3).

        """
        return self.integer + self.decimal / 10


# =============================================================================
# GPS Data
# =============================================================================


@dataclass(frozen=True, slots=True)
class RawGPS:
    """Raw GPS data (0x3E send format).

    Attributes:
        time_boot_ms: Timestamp since system boot in milliseconds.
        lat_e7: Latitude in degrees * 10^7.
        lon_e7: Longitude in degrees * 10^7.
        alt_msl_cm: Altitude MSL in centimeters.
        alt_ellipsoid_cm: Altitude above WGS84 ellipsoid in centimeters.
        vn_mmps: North velocity in mm/s * 10^3 (m E3/s).
        ve_mmps: East velocity in mm/s * 10^3 (m E3/s).
        vd_mmps: Down velocity in mm/s * 10^3 (m E3/s).

    """

    time_boot_ms: int
    lat_e7: int
    lon_e7: int
    alt_msl_cm: int
    alt_ellipsoid_cm: int
    vn_mmps: int
    ve_mmps: int
    vd_mmps: int


# =============================================================================
# System Information
# =============================================================================


@dataclass(frozen=True, slots=True)
class SystemTime:
    """System time (0x40 response).

    Attributes:
        unix_usec: UNIX epoch time in microseconds.
        boot_ms: Time since system startup in milliseconds.

    """

    unix_usec: int
    boot_ms: int


@dataclass(frozen=True, slots=True)
class GimbalSystemInfo:
    """Gimbal system information (0x31 response).

    Attributes:
        laser_state: True if laser ranging is enabled.

    """

    laser_state: bool


# =============================================================================
# IR Threshold Parameters
# =============================================================================




# =============================================================================
# Network Configuration
# =============================================================================


@dataclass(frozen=True, slots=True)
class IPConfig:
    """IP configuration (0x81/0x82 response).

    Attributes:
        ip: IP address.
        mask: Subnet mask.
        gateway: Gateway address.

    """

    ip: IPv4Address
    mask: IPv4Address
    gateway: IPv4Address


# =============================================================================
# Angle Limits
# =============================================================================


@dataclass(frozen=True, slots=True)
class AngleLimits:
    """Angle limits for a specific product.

    Attributes:
        yaw_min: Minimum yaw angle in degrees.
        yaw_max: Maximum yaw angle in degrees.
        pitch_min: Minimum pitch angle in degrees.
        pitch_max: Maximum pitch angle in degrees.

    """

    yaw_min: float
    yaw_max: float
    pitch_min: float
    pitch_max: float


# A8 Mini angle limits
ANGLE_LIMITS: Final[dict[ProductID, AngleLimits]] = {
    ProductID.A8_MINI: AngleLimits(yaw_min=-135.0, yaw_max=135.0, pitch_min=-90.0, pitch_max=25.0),
}


# =============================================================================
# Media / Web Server Models
# =============================================================================


class MediaType(IntEnum):
    """Media type for the camera web-server file API."""

    IMAGES = 0
    VIDEOS = 1


@dataclass(frozen=True, slots=True)
class MediaDirectory:
    """A directory entry returned by the camera web server.

    Attributes:
        name: Directory display name.
        path: Relative path usable in subsequent API calls.

    """

    name: str
    path: str


@dataclass(frozen=True, slots=True)
class MediaFile:
    """A media file entry returned by the camera web server.

    Attributes:
        name: File name (e.g. "IMG_0001.jpg").
        url: Full URL to download the file directly from the camera.

    """

    name: str
    url: str
