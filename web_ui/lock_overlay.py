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
