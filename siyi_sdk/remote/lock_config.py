"""Point-lock settings the camera service keeps between runs.

The values come from the loop measurement (``calibrate_loop``) and the operator.
They are stored as JSON on the companion computer, so a lock right after boot
steers with the measured delays and axis directions.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

from siyi_sdk.exceptions import ConfigurationError
from siyi_sdk.remote.protocol import ConflictError, OperationFailedError
from siyi_sdk.tracking.control import LockGains, LoopModel, TurnRateEstimator

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from siyi_sdk.firmware_tracking import FirmwareTrackingClient
    from siyi_sdk.tracking.attitude import AttitudeHistory
    from siyi_sdk.tracking.calibrate import LoopCalibration
    from siyi_sdk.tracking.firmware import FirmwarePointLock
    from siyi_sdk.tracking.gimbal import GimbalPointLock

Controller = Literal["angle", "rate", "firmware"]
Mount = Literal["normal", "inverted"]

CONTROLLERS: Final = ("angle", "rate", "firmware")
DEFAULT_PATH: Final = Path("~/.config/siyi/lock.json")

# Axis-specific values measured per mounting: flipping the camera changes how image
# motion maps to the reported attitude.
_MOUNT_FIELDS: Final = ("yaw_sign", "pitch_sign", "deg_per_unit_yaw", "deg_per_unit_pitch")


@dataclass
class LockConfig:
    """Lens, loop timing and controller settings for point lock."""

    # A8 Mini spec sheet: 81 deg horizontal. Calibrate on hardware.
    hfov_deg: float = 81.0
    # Map from image directions (right, up) to the reported attitude.
    yaw_sign: int = 1
    pitch_sign: int = 1
    # Default controller: 0x0E angle targets, the app's 0x07 rate loop, or the
    # experimental firmware controller.
    control: Controller = "angle"
    # Multiplies the gain derived from the measured delays (firmware: target offset).
    response: float = 1.0
    max_speed: int = 100
    model: Literal["local", "global"] = "local"
    # Measured by calibrate_loop.
    deg_per_unit_yaw: float = 1.0
    deg_per_unit_pitch: float = 1.0
    command_delay_ms: float = 60.0
    frame_delay_ms: float = 200.0
    motor_tau_ms: float = 0.0
    deadzone_yaw: float = 0.0
    deadzone_pitch: float = 0.0
    min_units_yaw: float = 0.0
    min_units_pitch: float = 0.0
    calibrated: bool = False
    mount_profiles: dict[str, dict[str, float]] = field(default_factory=dict)
    # How much later the ground station shows a frame than this computer receives it,
    # beyond the link latency the client measures. Used to find the frame the
    # operator clicked; measure it on the real link.
    ground_extra_video_delay_ms: float = 100.0

    # ------------------------------------------------------------------ storage

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LockConfig:
        """Build from stored or client-supplied values; unknown keys are rejected."""
        config = cls()
        config.update(data)
        return config

    def update(self, data: dict[str, Any]) -> None:
        """Change fields in place after checking names and types."""
        known = {f.name: f for f in dataclasses.fields(self)}
        unknown = sorted(set(data) - set(known))
        if unknown:
            raise ConfigurationError(f"Unknown lock settings: {', '.join(unknown)}")
        staged = dataclasses.replace(self)
        for name, value in data.items():
            current = getattr(self, name)
            if isinstance(current, bool):
                if not isinstance(value, bool):
                    raise ConfigurationError(f"{name} must be true or false")
            elif isinstance(current, (int, float)):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ConfigurationError(f"{name} must be a number")
                value = type(current)(value)
            elif isinstance(current, dict) and not isinstance(value, dict):
                raise ConfigurationError(f"{name} must be an object")
            setattr(staged, name, value)
        staged.validate()
        for name in data:
            setattr(self, name, getattr(staged, name))

    def validate(self) -> None:
        """Reject settings no lock could use."""
        if self.control not in CONTROLLERS:
            raise ConfigurationError(f"control must be one of {', '.join(CONTROLLERS)}")
        if self.model not in ("local", "global"):
            raise ConfigurationError("model must be local or global")
        if self.yaw_sign not in (-1, 1) or self.pitch_sign not in (-1, 1):
            raise ConfigurationError("yaw_sign and pitch_sign must be 1 or -1")
        if not 10.0 <= self.hfov_deg <= 170.0:
            raise ConfigurationError("hfov_deg must be between 10 and 170")
        if not 0.0 < self.response <= 5.0:
            raise ConfigurationError("response must be above 0 and at most 5")
        if not 1 <= self.max_speed <= 100:
            raise ConfigurationError("max_speed must be 1-100")
        if self.deg_per_unit_yaw <= 0 or self.deg_per_unit_pitch <= 0:
            raise ConfigurationError("deg_per_unit values must be positive")
        if min(self.command_delay_ms, self.frame_delay_ms, self.motor_tau_ms) < 0:
            raise ConfigurationError("delays must not be negative")
        if not 0.0 <= self.ground_extra_video_delay_ms <= 2000.0:
            raise ConfigurationError("ground_extra_video_delay_ms must be 0-2000")

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-able values."""
        return dataclasses.asdict(self)

    @classmethod
    def load(cls, path: Path | str | None = None) -> LockConfig:
        """Read the stored settings, or defaults if there are none yet."""
        target = Path(path or DEFAULT_PATH).expanduser()
        if not target.exists():
            return cls()
        return cls.from_dict(json.loads(target.read_text(encoding="utf-8")))

    def save(self, path: Path | str | None = None) -> None:
        """Write atomically, so a power cut never leaves half a file."""
        target = Path(path or DEFAULT_PATH).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".lock-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.to_dict(), handle, indent=2)
            os.replace(tmp, target)
        except BaseException:
            _unlink_quietly(tmp)
            raise

    # ------------------------------------------------------------------ loop

    def loop_model(self) -> LoopModel:
        """The measured loop as a :class:`LoopModel`."""
        return LoopModel(
            deg_per_unit=(self.deg_per_unit_yaw, self.deg_per_unit_pitch),
            command_delay_s=self.command_delay_ms / 1000,
            video_delay_s=self.frame_delay_ms / 1000,
            motor_tau_s=self.motor_tau_ms / 1000,
            deadzone_units=(self.deadzone_yaw, self.deadzone_pitch),
            min_units=(self.min_units_yaw, self.min_units_pitch),
        )

    def apply_mount(self, mount: Mount) -> None:
        """Use the axis directions measured in this mounting, or refuse to guess them."""
        profile = self.mount_profiles.get(mount)
        if profile:
            self.yaw_sign, self.pitch_sign = int(profile["yaw_sign"]), int(profile["pitch_sign"])
            self.deg_per_unit_yaw = float(profile["deg_per_unit_yaw"])
            self.deg_per_unit_pitch = float(profile["deg_per_unit_pitch"])
        elif self.mount_profiles:
            measured = next(iter(self.mount_profiles))
            raise ConflictError(
                f"The loop was measured with the camera {_describe(measured)}, but it is now "
                f"{_describe(mount)}. Run the loop measurement once in this orientation; "
                "both are remembered after that."
            )

    def store_mount(self, mount: Mount) -> None:
        """Remember the current axis values for this mounting."""
        self.mount_profiles[mount] = {name: getattr(self, name) for name in _MOUNT_FIELDS}

    def apply_calibration(self, result: LoopCalibration, mount: Mount) -> list[str]:
        """Store a loop measurement; returns notes for the operator.

        Raises:
            OperationFailedError: If an axis fit failed; nothing is changed then.
        """
        axes = [("yaw", result.yaw), ("pitch", result.pitch)]
        bad = [
            name
            for name, axis in axes
            if axis is not None and (axis.fit_error_deg > 1.0 or axis.command_delay_s > 1.0)
        ]
        if bad:
            raise OperationFailedError(
                f"Measurement failed on {' and '.join(bad)}; settings were not changed. Level "
                "the camera so it can tilt both ways, aim at a detailed, still scene and "
                "measure again."
            )
        model = result.model
        self.deg_per_unit_yaw, self.deg_per_unit_pitch = (round(v, 4) for v in model.deg_per_unit)
        self.command_delay_ms = round(model.command_delay_s * 1000, 1)
        self.frame_delay_ms = round(model.video_delay_s * 1000, 1)
        self.motor_tau_ms = round(model.motor_tau_s * 1000, 1)
        self.deadzone_yaw, self.deadzone_pitch = (round(v, 2) for v in model.deadzone_units)
        self.min_units_yaw, self.min_units_pitch = (float(v) for v in model.min_units)
        self.yaw_sign, self.pitch_sign = result.attitude_signs
        notes = list(result.notes)
        if 60.0 <= result.hfov_deg <= 100.0:
            self.hfov_deg = round(result.hfov_deg, 1)
        else:
            notes.append(
                f"measured field of view {result.hfov_deg:.1f} deg is implausible for the "
                f"A8 Mini (about 81); kept {self.hfov_deg}. Check the zoom reads 1x."
            )
        self.calibrated = True
        self.store_mount(mount)
        return notes

    # ------------------------------------------------------------------ locks

    def build_app_lock(
        self,
        *,
        send_rate: Callable[[int, int], Awaitable[None]],
        send_angle: Callable[[float, float], Awaitable[None]],
        attitude: AttitudeHistory,
        control: Controller,
        inverted: bool,
        turn_rate: Callable[[LoopModel], TurnRateEstimator | None],
    ) -> GimbalPointLock:
        """A :class:`GimbalPointLock` set up from these settings.

        Without attitude telemetry the delay can't be compensated, so the lock falls
        back to the slower rate loop on the raw image error.
        """
        from siyi_sdk.tracking.gimbal import GimbalPointLock

        loop = self.loop_model()
        compensated = attitude.latest() is not None
        # Rate steering with a measured motor lag predicts the commands in flight,
        # which allows a higher gain.
        smith = compensated and control == "rate" and loop.motor_tau_s > 0
        gains = LockGains.for_model(loop, compensated=compensated, smith=smith)
        gains.kp *= self.response
        gains.ki *= self.response**2
        gains.max_speed = self.max_speed
        return GimbalPointLock(
            send=send_rate,
            send_angle=send_angle,
            hfov_deg=self.hfov_deg,
            model=self.model,
            loop=loop,
            gains=gains,
            attitude=attitude if compensated else None,
            attitude_signs=(self.yaw_sign, self.pitch_sign),
            control="rate" if not compensated or control == "firmware" else control,
            trust_video_delay=self.calibrated,
            angle_pitch_offset=180.0 if inverted else 0.0,
            smith=smith,
            turn_rate=turn_rate(loop),
        )

    def build_firmware_lock(
        self,
        client: FirmwareTrackingClient,
        *,
        owns_connection: bool,
        stop: Callable[[], Awaitable[None]],
    ) -> FirmwarePointLock:
        """A :class:`FirmwarePointLock` set up from these settings."""
        from siyi_sdk.tracking.firmware import FirmwarePointLock

        return FirmwarePointLock(
            client,
            owns_connection=owns_connection,
            # The camera's own stream arrives in bursts with gaps of up to ~650 ms.
            video_timeout=1.2 if not owns_connection else 0.4,
            stop=stop,
            model=self.model,
            # No attitude-based delay correction: an unreliable pitch sign (inverted
            # mount) drove the gimbal to its limit. The response scales the offset instead.
            response=self.response,
            # Every frame of the ~25 fps link; 20 Hz aliased to every other frame.
            send_interval=0.03,
        )


def mounted_inverted(pitch_deg: float) -> bool:
    """Whether a reported pitch means the gimbal hangs upside down.

    An upright mount reports pitch within -90..25, an inverted one 90..180 or
    -180..-155, so the attitude alone tells them apart.
    """
    pitch = (pitch_deg + 180.0) % 360.0 - 180.0
    return pitch > 57.5 or pitch < -122.5


def _unlink_quietly(path: str) -> None:
    """Remove a temporary file, ignoring a file that is already gone."""
    with contextlib.suppress(OSError):
        os.unlink(path)


def _describe(mount: str) -> str:
    return "upside down" if mount == "inverted" else "upright"
