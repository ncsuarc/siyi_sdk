"""Draw the point-lock marker onto video frames before they are sent to the browser.

Drawing on the server keeps the marker in step with the picture; a browser
overlay fed by the 10 Hz telemetry socket would lag behind the video.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

from siyi_sdk.tracking import LockState, LockStatus

# BGR
LOCKED = (80, 220, 80)
SEARCHING = (0, 200, 255)


def draw_lock(image: np.ndarray, status: LockStatus) -> None:
    """Draw a reticle on the locked spot, or an edge arrow toward it when off-screen."""
    if status.state is LockState.IDLE:
        return
    height, width = image.shape[:2]
    color = LOCKED if status.state is LockState.LOCKED else SEARCHING
    thick = max(2, width // 640)
    radius = max(14, width // 50)
    font = 0.5 * thick
    label = ("LOCKED" if status.state is LockState.LOCKED else "SEARCHING") + f" {status.score:.2f}"
    x, y = status.x, status.y
    if status.on_screen:
        cx, cy = round(x), round(y)
        cv2.circle(image, (cx, cy), radius, color, thick, cv2.LINE_AA)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            cv2.line(
                image,
                (cx + dx * radius // 2, cy + dy * radius // 2),
                (cx + dx * radius * 3 // 2, cy + dy * radius * 3 // 2),
                color,
                thick,
                cv2.LINE_AA,
            )
        origin = (min(cx + radius + 6, width - 160 * thick), max(cy - radius, 20 * thick))
    else:
        # Arrow from the image centre toward the spot, clipped to a margin inside the frame.
        mx, my = width / 2, height / 2
        dx, dy = x - mx, y - my
        margin = 40 * thick
        scale = min(
            (mx - margin) / abs(dx) if dx else math.inf, (my - margin) / abs(dy) if dy else math.inf
        )
        tip = (round(mx + dx * scale), round(my + dy * scale))
        cv2.arrowedLine(
            image, (round(mx), round(my)), tip, color, thick + 1, cv2.LINE_AA, tipLength=0.08
        )
        label += " off-screen"
        origin = (
            min(max(tip[0] - 80 * thick, 4), width - 200 * thick),
            min(max(tip[1] + 24 * thick, 20 * thick), height - 8),
        )
    cv2.putText(
        image, label, origin, cv2.FONT_HERSHEY_SIMPLEX, font, (0, 0, 0), thick + 2, cv2.LINE_AA
    )
    cv2.putText(image, label, origin, cv2.FONT_HERSHEY_SIMPLEX, font, color, thick, cv2.LINE_AA)


CENTRE = (255, 255, 0)  # cyan: where the camera points
COMMAND = (255, 0, 255)  # magenta: the target last sent to the firmware
STALE = (160, 160, 160)
WARN = (60, 60, 255)


def _text(image: np.ndarray, text: str, origin: tuple[int, int], color, thick: int) -> None:
    font = 0.5 * thick
    cv2.putText(image, text, origin, cv2.FONT_HERSHEY_SIMPLEX, font, (0, 0, 0), thick + 2, cv2.LINE_AA)
    cv2.putText(image, text, origin, cv2.FONT_HERSHEY_SIMPLEX, font, color, thick, cv2.LINE_AA)


def draw_firmware_command(
    image: np.ndarray,
    status: LockStatus,
    target: tuple[int, int, int, int] | None,
    target_age_s: float,
    gimbal_rate: tuple[float, float] | None,
) -> None:
    """Show what the firmware lock asks for and whether the gimbal moves.

    ``target`` is the last command in the firmware's 1280x720 space; the arrow
    from the image centre to it is the correction requested. ``gimbal_rate`` is
    the measured (yaw, pitch) turn rate in deg/s from the attitude stream.
    """
    height, width = image.shape[:2]
    thick = max(1, width // 1280 + 1)
    cx, cy = width // 2, height // 2
    arm = max(20, width // 40)
    cv2.line(image, (cx - arm, cy), (cx + arm, cy), CENTRE, thick, cv2.LINE_AA)
    cv2.line(image, (cx, cy - arm), (cx, cy + arm), CENTRE, thick, cv2.LINE_AA)
    cv2.circle(image, (cx, cy), arm // 4, CENTRE, thick, cv2.LINE_AA)
    lines: list[tuple[str, tuple[int, int, int]]] = []
    if target is not None:
        stale = target_age_s > 0.5
        color = STALE if stale else COMMAND
        tx, ty = round(target[0] * width / 1280), round(target[1] * height / 720)
        bw, bh = round(target[2] * width / 1280), round(target[3] * height / 720)
        cv2.rectangle(image, (tx - bw // 2, ty - bh // 2), (tx + bw // 2, ty + bh // 2), color, thick,
                      cv2.LINE_AA)
        if (tx - cx) ** 2 + (ty - cy) ** 2 > 16:
            cv2.arrowedLine(image, (cx, cy), (tx, ty), color, thick + 1, cv2.LINE_AA, tipLength=0.06)
        lines.append((f"CMD   dx {tx - cx:+5d}  dy {ty - cy:+5d} px   age {target_age_s * 1000:4.0f} ms"
                      + ("  STALE" if stale else ""), color))
    if status.state is LockState.LOCKED and status.on_screen:
        lines.append((f"POINT dx {round(status.x) - cx:+5d}  dy {round(status.y) - cy:+5d} px "
                      "from centre", LOCKED))
    if gimbal_rate is None:
        lines.append(("GIMBAL  no attitude data", STALE))
    else:
        yaw_rate, pitch_rate = gimbal_rate
        lines.append((f"GIMBAL yaw {yaw_rate:+6.1f}  pitch {pitch_rate:+6.1f} deg/s", CENTRE))
        commanded = target is not None and target_age_s <= 0.5 and (
            abs(target[0] - 640) > 25 or abs(target[1] - 360) > 25
        )
        if commanded and max(abs(yaw_rate), abs(pitch_rate)) < 0.5:
            lines.append(("NOT ROTATING despite an off-centre command", WARN))
    step = 22 * thick
    y = height - 12 - step * (len(lines) - 1)
    for text, color in lines:
        _text(image, text, (12, y), color, thick)
        y += step


# BGR, by feature kind (see PointLock._debug_record)
_KIND_COLORS = ((150, 150, 150), (60, 60, 255), (80, 220, 80), (255, 140, 30))
_KIND_LABELS = ("dropped (flow disagreed with its return)", "rejected as moving",
                "agrees with camera motion", "fits the point's local motion")
_FLOW_GAIN = 3  # flow lines are drawn this many times longer, since frame-to-frame motion is tiny


def draw_tracking_debug(
    image: np.ndarray, info: dict | None, score: float, min_score: float
) -> None:
    """Show how the point lock is working: every tracked feature, why it counts, and the score.

    ``info`` is ``PointLock.debug_info``; its coordinates are in the tracker's downscaled
    frame. Features the lock ignores (moving objects) are red; those steering the point
    are blue (``local`` model) or green.
    """
    if info is None:
        return
    height, width = image.shape[:2]
    thick = max(1, width // 1280 + 1)
    before = info["before"] / info["scale"]
    after = info["after"] / info["scale"]
    kind = info["kind"]
    # Draw the unused kinds first so the features that matter stay on top.
    for k in (0, 1, 2, 3):
        color = _KIND_COLORS[k]
        for (bx, by), (ax, ay) in zip(before[kind == k], after[kind == k], strict=True):
            if k:
                gain = _FLOW_GAIN - 1
                tip = (round(ax + (ax - bx) * gain), round(ay + (ay - by) * gain))
                cv2.line(image, (round(bx), round(by)), tip, color, thick, cv2.LINE_AA)
            cv2.circle(image, (round(ax), round(ay)), 2 * thick + (k > 1), color, -1, cv2.LINE_AA)
    # The legend sits bottom-right: the dashboard's own readouts cover the top-left, and the
    # firmware command text uses the bottom-left. Text scales with the frame, not its pixels.
    font = max(0.4, 0.45 * width / 1280)
    line = max(1, round(width / 1280))
    counts = np.bincount(kind, minlength=4)
    rows = [(f"TRACKING  {counts[2] + counts[3]} of {len(kind)} features agree",
             (255, 255, 255), None)]
    rows += [(f"{counts[k]:3d} {_KIND_LABELS[k]}", _KIND_COLORS[k], k) for k in (3, 2, 1, 0)]
    rows += [("", (255, 255, 255), "bar"),
             (f"score {score:.2f}  (released below {min_score:.2f})", (255, 255, 255), None)]
    sizes = [cv2.getTextSize(text or "x", cv2.FONT_HERSHEY_SIMPLEX, font, line)[0]
             for text, _, _ in rows]
    step = round(max(h for _, h in sizes) * 1.7)
    text_w = max(w for w, _ in sizes)
    x = width - 12 - text_w
    y = height - 12 - step * (len(rows) - 1)
    for (text, color, k), (_, h) in zip(rows, sizes, strict=True):
        if isinstance(k, int):
            cv2.circle(image, (x - step // 2, y - h // 2), max(3, h // 3), color, -1, cv2.LINE_AA)
        elif k == "bar":
            top, bottom = y - h, y
            cv2.rectangle(image, (x, top), (x + text_w, bottom), (255, 255, 255), line)
            fill = round(text_w * min(max(score, 0.0), 1.0))
            cv2.rectangle(image, (x, top), (x + fill, bottom),
                          LOCKED if score >= min_score else WARN, -1)
            mark = x + round(text_w * min_score)
            cv2.line(image, (mark, top - 4), (mark, bottom + 4), WARN, line + 1)
        if text:
            _outlined(image, text, (x, y), color, font, line)
        y += step


def _outlined(image: np.ndarray, text: str, origin: tuple[int, int],
              color: tuple[int, int, int], font: float, line: int) -> None:
    cv2.putText(image, text, origin, cv2.FONT_HERSHEY_SIMPLEX, font, (0, 0, 0), line + 2,
                cv2.LINE_AA)
    cv2.putText(image, text, origin, cv2.FONT_HERSHEY_SIMPLEX, font, color, line, cv2.LINE_AA)
