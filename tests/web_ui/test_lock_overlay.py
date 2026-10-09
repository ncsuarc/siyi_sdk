"""The tracking debug overlay draws from PointLock.debug_info."""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("cv2")

from web_ui.lock_overlay import draw_tracking_debug  # noqa: E402


def test_tracking_debug_overlay_draws_and_tolerates_no_data() -> None:
    image = np.zeros((360, 640, 3), np.uint8)
    draw_tracking_debug(image, None, 0.0, 0.2)
    assert not image.any()
    info = {
        "before": np.array([[10.0, 10.0], [50.0, 40.0], [80.0, 60.0], [90.0, 30.0]]),
        "after": np.array([[11.0, 10.0], [52.0, 41.0], [81.0, 60.0], [92.0, 31.0]]),
        "kind": np.array([0, 1, 2, 3], np.uint8),
        "scale": 0.5,
    }
    draw_tracking_debug(image, info, 0.75, 0.2)
    assert image.any()
