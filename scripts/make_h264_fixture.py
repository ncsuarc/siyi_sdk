"""Record a deterministic synthetic H.264 fixture using real libx264/PyAV."""

from pathlib import Path

import av
import numpy as np


def main() -> None:
    """Write one 64x48 red IDR image; this is not a camera recording."""
    target = Path(__file__).resolve().parents[1] / "tests/fixtures/synthetic_red.h264"
    target.parent.mkdir(parents=True, exist_ok=True)
    codec = av.CodecContext.create("libx264", "w")
    codec.width, codec.height = 64, 48
    codec.pix_fmt = "yuv420p"
    from fractions import Fraction
    codec.time_base = Fraction(1, 30)
    codec.options = {"preset": "ultrafast", "tune": "zerolatency", "x264-params": "keyint=1:repeat-headers=1"}
    image = np.zeros((48, 64, 3), dtype=np.uint8)
    image[:, :, 2] = 220
    frame = av.VideoFrame.from_ndarray(image, format="bgr24")
    frame.pts = 0
    target.write_bytes(b"".join(bytes(packet) for packet in [*codec.encode(frame), *codec.encode(None)]))


if __name__ == "__main__":
    main()
