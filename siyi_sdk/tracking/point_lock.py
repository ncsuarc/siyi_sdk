# Copyright (c) 2026 Mohamed Abdelkader <mohamedashraf123@gmail.com>
# All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Hold a fixed point in a static scene while the camera moves."""

from __future__ import annotations

from typing import Any, Literal

import cv2
import numpy as np
from numpy.typing import NDArray

PointModel = Literal["local", "global"]
Image = NDArray[Any]  # BGR uint8 frame; OpenCV's stubs accept any numeric array
Box = tuple[int, int, int, int]


class PointLock:
    """Track a fixed spot in the scene through camera motion.

    Rather than following the selected patch itself, this tracks a few hundred
    features across the whole frame with pyramidal Lucas-Kanade optical flow and
    rejects moving objects with a RANSAC homography fit. The point can be
    featureless, briefly covered, or off-screen. Two motion models:

    ``local`` (default) moves the point with a similarity transform fitted to
    the ``LOCAL_FEATURES`` agreeing features nearest to it. Nearby features
    usually sit at a similar depth, so buildings and other 3D structure cause
    little drift. Best for ground targets seen from above or at an angle.

    ``global`` moves the point with the whole-frame homography. Better when the
    point is far away (near the horizon) with foreground objects in front of it,
    since its on-screen neighbours are then at a very different depth.

    The score is the fraction of tracked features that agree with the fitted
    motion; 0 means the motion could not be estimated and the point is frozen.
    Frame-to-frame chaining drifts slowly, so keep the point near the image
    centre (which a gimbal loop does) for the best accuracy.

    The interface matches ``cv2.Tracker`` (``init``, ``update``,
    ``getTrackingScore``) so it can be swapped in for OpenCV trackers.
    """

    WORK_WIDTH = 640  # flow runs on a downscaled copy; plenty for a Raspberry Pi
    MIN_INLIERS = 25
    TARGET_FEATURES = 300
    LOCAL_FEATURES = 40

    def __init__(self, model: PointModel = "local") -> None:
        """Create an unlocked tracker using motion ``model`` (``local`` or ``global``)."""
        if model not in ("local", "global"):
            raise ValueError(f"point model must be local or global, got {model!r}")
        self.model = model
        self.score = 0.0
        self.inliers = 0

    def init(self, frame: Image, roi: Box) -> None:
        """Lock onto the centre of ``roi`` (x, y, w, h); its size only sets the box."""
        self.scale = min(1.0, self.WORK_WIDTH / frame.shape[1])
        x, y, w, h = roi
        self.size = (w, h)
        # Centre first, then the box corners, in full-resolution pixels.
        corners = [[x + w / 2, y + h / 2], [x, y], [x + w, y], [x + w, y + h], [x, y + h]]
        self.shape: Image = np.array(corners, dtype=np.float32).reshape(-1, 1, 2)
        self.gray = self._gray(frame)
        self.points = self._features(self.gray)
        self.score = 1.0
        self.inliers = 0

    def _gray(self, frame: Image) -> Image:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if self.scale == 1.0:
            return gray
        size = (round(gray.shape[1] * self.scale), round(gray.shape[0] * self.scale))
        return cv2.resize(gray, size, interpolation=cv2.INTER_AREA)

    def _features(self, gray: Image) -> Image | None:
        features: Image | None = cv2.goodFeaturesToTrack(
            gray, self.TARGET_FEATURES, qualityLevel=0.01, minDistance=8, blockSize=7
        )
        return features

    def update(self, frame: Image) -> tuple[bool, Box]:
        """Move the point by this frame's scene motion; returns (ok, box)."""
        gray = self._gray(frame)
        homography, keep, local = None, None, None
        if self.points is not None and len(self.points) >= 8:
            # OpenCV accepts None for the output points; its type stubs don't.
            moved, status, _ = cv2.calcOpticalFlowPyrLK(  # type: ignore[call-overload]
                self.gray, gray, self.points, None, winSize=(21, 21), maxLevel=3
            )
            back, status_back, _ = cv2.calcOpticalFlowPyrLK(  # type: ignore[call-overload]
                gray, self.gray, moved, None, winSize=(21, 21), maxLevel=3
            )
            # Forward-backward check drops features that slid along edges or got occluded.
            error = np.linalg.norm(self.points - back, axis=2).ravel()
            good = (status.ravel() == 1) & (status_back.ravel() == 1) & (error < 1.0)
            if good.sum() >= 8:
                homography, mask = cv2.findHomography(
                    self.points[good], moved[good], cv2.RANSAC, 3.0
                )
                if homography is not None:
                    inlier = mask.ravel() == 1
                    keep = moved[good][inlier]
                    if self.model == "local":
                        local = self._local_motion(self.points[good][inlier], keep)
        self.inliers = 0 if keep is None else len(keep)
        to_work = np.diag([self.scale, self.scale, 1.0])
        if self.inliers < self.MIN_INLIERS or (self.model == "local" and local is None):
            self.score = 0.0
        else:
            self.score = self.inliers / max(1, 0 if self.points is None else len(self.points))
            motion = local if self.model == "local" else homography
            full = np.linalg.inv(to_work) @ motion @ to_work
            self.shape = cv2.perspectiveTransform(self.shape, full)
        self.gray = gray
        # Keep agreeing features; re-detect across the frame when too few remain.
        if keep is not None and len(keep) >= self.TARGET_FEATURES * 2 // 3:
            self.points = keep.reshape(-1, 1, 2)
        else:
            self.points = self._features(gray)
        return self.score > 0, self.box()

    def _local_motion(self, before: Image, after: Image) -> Image | None:
        """Similarity transform (3x3, work pixels) fitted to the features nearest the point."""
        point = np.array(self.center()) * self.scale
        distance = np.linalg.norm(before.reshape(-1, 2) - point, axis=1)
        nearest = np.argsort(distance)[: self.LOCAL_FEATURES]
        if len(nearest) < 6:
            return None
        affine, _ = cv2.estimateAffinePartial2D(before[nearest], after[nearest], method=cv2.LMEDS)
        return None if affine is None else np.vstack([affine, [0.0, 0.0, 1.0]])

    def center(self) -> tuple[float, float]:
        """Current point position in full-resolution pixels (may be off-screen)."""
        return float(self.shape[0, 0, 0]), float(self.shape[0, 0, 1])

    def box(self) -> Box:
        """Box of the original size's current extent, centred on the point."""
        _, _, w, h = cv2.boundingRect(self.shape[1:].astype(np.float32))
        # A whole-frame homography can stretch the box far away from the point
        # even when the point itself is right; keep the drawing readable.
        w0, h0 = self.size
        w, h = min(max(w, w0 // 4), w0 * 4), min(max(h, h0 // 4), h0 * 4)
        cx, cy = self.center()
        return round(cx - w / 2), round(cy - h / 2), w, h

    def getTrackingScore(self) -> float:  # noqa: N802 - matches cv2.Tracker
        """Fraction of features agreeing with the fitted motion (0 = lost)."""
        return self.score
