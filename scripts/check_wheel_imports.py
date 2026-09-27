"""Verify the built wheel keeps base control independent of video packages."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def main() -> None:
    """Extract one wheel and run each import in a fresh isolated interpreter."""
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    with tempfile.TemporaryDirectory() as temp:
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(temp)
        metadata = next(Path(temp).glob("*.dist-info/METADATA")).read_text(encoding="utf-8")
        for extra in ("stream", "stream-opencv", "stream-gst", "stream-aiortsp", "web"):
            assert any("Requires-Dist: numpy" in line and f"extra == '{extra}'" in line for line in metadata.splitlines()), extra
        checks = {
            "base": ("numpy", "from siyi_sdk import SIYIClient, connect_udp, MediaClient"),
            "opencv": ("aiortsp", "from siyi_sdk.stream.opencv_backend import OpenCVBackend"),
            "aiortsp": ("cv2", "from siyi_sdk.stream.aiortsp_backend import AiortspBackend"),
        }
        for name, (blocked, imports) in checks.items():
            code = f"""
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == {blocked!r} or fullname.startswith({blocked!r} + '.'):
            raise ModuleNotFoundError('intentionally blocked: ' + fullname)
sys.meta_path.insert(0, Block())
sys.path.insert(0, {temp!r})
{imports}
import siyi_sdk
assert siyi_sdk.__file__.startswith({temp!r}), siyi_sdk.__file__
assert {blocked!r} not in sys.modules
"""
            subprocess.run([sys.executable, "-I", "-c", code], check=True, capture_output=True)
    print(json.dumps({"wheel": str(wheel), "base_without_numpy": True,
                      "opencv_without_aiortsp": True, "aiortsp_without_opencv": True,
                      "gstreamer_runtime": "requires Linux PyGObject"}))


if __name__ == "__main__":
    main()
