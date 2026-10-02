# Copyright (c) 2026 Mohamed Abdelkader <mohamedashraf123@gmail.com>
# All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Try OpenCV single-object trackers on a video file, camera, or RTSP stream.

Draw a box around a target and watch one or more trackers follow it. Each
tracker gets its own colored box, confidence score, and timing. A summary at
the end reports per-tracker update time and lost frames, so the same script
doubles as a Raspberry Pi benchmark (use ``--no-display``).

Trackers:
    nano  cv2.TrackerNano (NanoTrack v2, ~2 MB ONNX; needs model files)
    vit   cv2.TrackerVit  (~0.7 MB ONNX; needs a model file)
    mil   cv2.TrackerMIL  (no model; slow, baseline only)
    kcf   cv2.TrackerKCF  (needs opencv-contrib-python)
    csrt  cv2.TrackerCSRT (needs opencv-contrib-python)
    point Point lock: holds a fixed spot in a static scene while the camera moves,
          using whole-frame optical flow (no model; siyi_sdk.tracking.PointLock)

Examples:
    python examples/tracker_demo.py --download             # fetch Nano/ViT models once
    python examples/tracker_demo.py clip.mp4               # NanoTrack, pick the target
    python examples/tracker_demo.py clip.mp4 -t nano,vit,kcf
    python examples/tracker_demo.py clip.mp4 -t point,nano      # lock a spot on the ground
    python examples/tracker_demo.py rtsp://192.168.144.25:8554/main.264
    python examples/tracker_demo.py clip.mp4 --no-display --roi 600,300,80,60 --width 1280

Keys: Space pause/resume, R re-select target, N next frame while paused, Q/Esc quit.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from siyi_sdk.tracking import PointLock

MODEL_DIR = Path.home() / ".cache" / "siyi_sdk" / "trackers"
MODELS = {
    "nanotrack_backbone_sim.onnx": "https://raw.githubusercontent.com/HonglinChu/SiamTrackers/master/"
    "NanoTrack/models/nanotrackv2/nanotrack_backbone_sim.onnx",
    "nanotrack_head_sim.onnx": "https://raw.githubusercontent.com/HonglinChu/SiamTrackers/master/"
    "NanoTrack/models/nanotrackv2/nanotrack_head_sim.onnx",
    "object_tracking_vittrack_2023sep.onnx": "https://github.com/opencv/opencv_zoo/raw/main/"
    "models/object_tracking_vittrack/object_tracking_vittrack_2023sep.onnx",
}
NEEDS = {
    "nano": ("nanotrack_backbone_sim.onnx", "nanotrack_head_sim.onnx"),
    "vit": ("object_tracking_vittrack_2023sep.onnx",),
}
# BGR colors, distinguishable on most footage.
COLORS = [(0, 220, 255), (255, 160, 0), (80, 255, 80), (255, 80, 255), (80, 80, 255)]
WINDOW = "Tracker demo"
POINT_MODEL = "local"  # set by --point-model


def download_models(model_dir: Path) -> None:
    """Fetch any missing Nano/ViT model files into ``model_dir``."""
    model_dir.mkdir(parents=True, exist_ok=True)
    for name, url in MODELS.items():
        path = model_dir / name
        if path.exists() and path.stat().st_size > 0:
            print(f"have      {path}")
            continue
        print(f"download  {url}")
        tmp = path.with_suffix(".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(path)
        print(f"saved     {path} ({path.stat().st_size / 1024:.0f} KB)")


def create_tracker(kind: str, model_dir: Path, score_threshold: float) -> cv2.Tracker:
    """Build a fresh tracker, exiting with a hint if its models or module are missing."""
    missing = [name for name in NEEDS.get(kind, ()) if not (model_dir / name).exists()]
    if missing:
        sys.exit(
            f"{kind}: missing {', '.join(missing)} in {model_dir}. "
            "Run once with --download, or pass --models DIR."
        )
    if kind == "nano":
        params = cv2.TrackerNano_Params()
        params.backbone = str(model_dir / "nanotrack_backbone_sim.onnx")
        params.neckhead = str(model_dir / "nanotrack_head_sim.onnx")
        return cv2.TrackerNano_create(params)
    if kind == "vit":
        params = cv2.TrackerVit_Params()
        params.net = str(model_dir / "object_tracking_vittrack_2023sep.onnx")
        params.tracking_score_threshold = score_threshold
        return cv2.TrackerVit_create(params)
    if kind == "mil":
        return cv2.TrackerMIL_create()
    if kind == "point":
        return PointLock(POINT_MODEL)
    if kind in ("kcf", "csrt"):
        factory = getattr(cv2, f"Tracker{kind.upper()}_create", None)
        if factory is None:
            sys.exit(
                f"{kind}: not in this OpenCV build. Install opencv-contrib-python "
                "(uninstall opencv-python first; they conflict)."
            )
        return factory()
    sys.exit(f"Unknown tracker {kind!r}; choose from point, nano, vit, mil, kcf, csrt.")


@dataclass
class Run:
    """One tracker's state and timings for the session."""

    kind: str
    color: tuple[int, int, int]
    tracker: cv2.Tracker | None = None
    box: tuple[int, int, int, int] | None = None
    score: float | None = None
    found: bool = False
    update_ms: list[float] = field(default_factory=list)
    init_ms: list[float] = field(default_factory=list)
    lost: int = 0

    def start(
        self,
        frame: np.ndarray,
        roi: tuple[int, int, int, int],
        model_dir: Path,
        score_threshold: float,
    ) -> None:
        """(Re)initialize on ``roi`` in ``frame``."""
        # Recreate on re-select: some trackers keep state from a previous init.
        self.tracker = create_tracker(self.kind, model_dir, score_threshold)
        started = time.perf_counter()
        self.tracker.init(frame, roi)
        self.init_ms.append((time.perf_counter() - started) * 1000)
        self.box, self.found, self.score = roi, True, None

    def update(self, frame: np.ndarray, score_threshold: float) -> None:
        """Track into ``frame`` and record timing and loss."""
        started = time.perf_counter()
        ok, box = self.tracker.update(frame)
        self.update_ms.append((time.perf_counter() - started) * 1000)
        # OpenCV 5 gives every tracker getTrackingScore(); those without one return -1.
        get_score = getattr(self.tracker, "getTrackingScore", None)
        score = float(get_score()) if get_score else -1.0
        self.score = score if score >= 0 else None
        self.found = bool(ok) and (self.score is None or self.score >= score_threshold)
        self.box = tuple(int(v) for v in box) if ok else self.box
        self.lost += not self.found


def resize(frame: np.ndarray, width: int | None) -> np.ndarray:
    """Scale ``frame`` to ``width`` keeping its aspect ratio."""
    if not width or frame.shape[1] == width:
        return frame
    height = round(frame.shape[0] * width / frame.shape[1])
    return cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)


def draw(frame: np.ndarray, runs: list[Run], hud: list[str]) -> None:
    """Draw each tracker's box, center, label, and the status lines."""
    for index, run in enumerate(runs):
        if run.box is None:
            continue
        x, y, w, h = run.box
        thickness = 2 if run.found else 1
        cv2.rectangle(frame, (x, y), (x + w, y + h), run.color, thickness)
        cx, cy = x + w // 2, y + h // 2
        cv2.drawMarker(frame, (cx, cy), run.color, cv2.MARKER_CROSS, 12, thickness)
        label = run.kind + (f" {run.score:.2f}" if run.score is not None else "")
        label += "" if run.found else " LOST"
        height, width = frame.shape[:2]
        label_x, label_y = x, max(14 + 16 * index, y - 6 - 16 * index)
        if not (0 <= cx < width and 0 <= cy < height):
            # Off-screen: point an arrow from the image center toward it, as the gimbal would turn.
            scale = min(
                (width / 2 - 30) / abs(cx - width / 2) if cx != width / 2 else np.inf,
                (height / 2 - 30) / abs(cy - height / 2) if cy != height / 2 else np.inf,
            )
            tip = (
                round(width / 2 + (cx - width / 2) * scale),
                round(height / 2 + (cy - height / 2) * scale),
            )
            cv2.arrowedLine(
                frame, (width // 2, height // 2), tip, run.color, 2, cv2.LINE_AA, tipLength=0.08
            )
            label += " off-screen"
            label_x = min(max(tip[0] - 60, 4), width - 200)
            label_y = min(max(tip[1] + 24 + 16 * index, 14), height - 6)
        cv2.putText(
            frame,
            label,
            # Stack labels so overlapping boxes keep readable names.
            (label_x, label_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            run.color,
            1,
            cv2.LINE_AA,
        )
    for i, line in enumerate(hud):
        org = (10, 22 + 20 * i)
        cv2.putText(frame, line, org, cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(
            frame, line, org, cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA
        )


def select_roi(frame: np.ndarray) -> tuple[int, int, int, int] | None:
    """Let the user drag a box; Enter/Space confirms, C cancels."""
    roi = cv2.selectROI(WINDOW, frame, showCrosshair=True, fromCenter=False)
    return tuple(int(v) for v in roi) if roi[2] > 0 and roi[3] > 0 else None


def summarize(runs: list[Run], frames: int, decode_ms: list[float]) -> None:
    """Print per-tracker timing and loss statistics."""

    def pct(values: list[float], q: float) -> float:
        return sorted(values)[min(len(values) - 1, int(q * len(values)))]

    print(
        f"\n{frames} frames; decode+resize median {statistics.median(decode_ms):.1f} ms"
        if decode_ms
        else f"\n{frames} frames"
    )
    print(
        f"{'tracker':8} {'updates':>8} {'median ms':>10} {'p95 ms':>8} {'max fps':>8} "
        f"{'lost':>6} {'init ms':>8}"
    )
    for run in runs:
        if not run.update_ms:
            print(f"{run.kind:8} {'0':>8}")
            continue
        median = statistics.median(run.update_ms)
        print(
            f"{run.kind:8} {len(run.update_ms):8d} {median:10.1f} {pct(run.update_ms, 0.95):8.1f} "
            f"{1000 / median if median else float('inf'):8.0f} {run.lost:6d} "
            f"{statistics.median(run.init_ms):8.1f}"
        )


def main() -> None:
    """Parse arguments and run the tracking loop."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("source", nargs="?", help="video file, camera index (0), or rtsp:// URL")
    parser.add_argument(
        "-t", "--trackers", default="nano", help="comma list: point,nano,vit,mil,kcf,csrt"
    )
    parser.add_argument(
        "--models", type=Path, default=MODEL_DIR, help=f"model directory (default {MODEL_DIR})"
    )
    parser.add_argument("--download", action="store_true", help="download missing Nano/ViT models")
    parser.add_argument(
        "--width", type=int, help="resize frames to this width first, e.g. 1280 for 720p"
    )
    parser.add_argument(
        "--roi", help="initial box x,y,w,h in resized pixels (skips mouse selection)"
    )
    parser.add_argument(
        "--point-model",
        choices=("local", "global"),
        default="local",
        help="point lock motion model: local (ground targets) or global (distant/horizon)",
    )
    parser.add_argument("--score", type=float, default=0.3, help="score below this counts as lost")
    parser.add_argument("--threads", type=int, help="limit OpenCV threads (Pi 5 has 4 cores)")
    parser.add_argument("--no-display", action="store_true", help="headless benchmark; needs --roi")
    parser.add_argument("--output", type=Path, help="write the annotated video here (.mp4)")
    parser.add_argument(
        "--fast", action="store_true", help="don't pace playback to the video frame rate"
    )
    parser.add_argument("--max-frames", type=int, help="stop after this many frames")
    args = parser.parse_args()

    if args.download:
        download_models(args.models)
        if not args.source:
            return
    if not args.source:
        parser.error("give a video source, or only --download")
    if args.no_display and not args.roi:
        parser.error("--no-display needs --roi x,y,w,h")
    global POINT_MODEL
    POINT_MODEL = args.point_model
    if args.threads:
        cv2.setNumThreads(args.threads)

    source = int(args.source) if args.source.isdigit() else args.source
    capture = cv2.VideoCapture(source)
    if not capture.isOpened():
        sys.exit(f"Could not open {args.source}")
    video_fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    is_file = isinstance(source, str) and not source.lower().startswith(("rtsp", "http"))

    kinds = [k.strip().lower() for k in args.trackers.split(",") if k.strip()]
    runs = [Run(kind, COLORS[i % len(COLORS)]) for i, kind in enumerate(kinds)]
    for kind in kinds:  # fail early on missing models or contrib
        create_tracker(kind, args.models, args.score)

    ok, frame = capture.read()
    if not ok:
        sys.exit("No frames in source")
    frame = resize(frame, args.width)
    print(
        f"{args.source}: {frame.shape[1]}x{frame.shape[0]} at {video_fps:.1f} fps, "
        f"OpenCV {cv2.__version__}, {cv2.getNumThreads()} threads"
    )

    if not args.no_display:
        cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
    roi = tuple(int(v) for v in args.roi.split(",")) if args.roi else select_roi(frame)
    if roi is None:
        sys.exit("No target selected")
    for run in runs:
        run.start(frame, roi, args.models, args.score)

    writer = None
    if args.output:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(
            str(args.output), fourcc, video_fps, (frame.shape[1], frame.shape[0])
        )

    frames, decode_ms, paused, step = 1, [], False, False
    work_ms = 0.0
    while True:
        tick = time.perf_counter()
        if not paused or step:
            started = time.perf_counter()
            ok, frame = capture.read()
            if not ok:
                break
            frame = resize(frame, args.width)
            decode_ms.append((time.perf_counter() - started) * 1000)
            frames += 1
            for run in runs:
                run.update(frame, args.score)
            step = False
            # Decode, resize, and every tracker update; excludes drawing and playback pacing.
            work_ms = (time.perf_counter() - tick) * 1000
        view = frame.copy()
        hud = [f"frame {frames}  work {work_ms:.0f} ms" + ("  PAUSED" if paused else "")]
        hud += [
            f"{run.kind}: {run.update_ms[-1]:.1f} ms" if run.update_ms else run.kind for run in runs
        ]
        draw(view, runs, hud)
        if writer and not paused:
            writer.write(view)
        if args.max_frames and frames >= args.max_frames:
            break
        if args.no_display:
            continue
        cv2.imshow(WINDOW, view)
        wait = 1
        if is_file and not args.fast and not paused:
            wait = max(1, int(1000 / video_fps - (time.perf_counter() - tick) * 1000))
        key = cv2.waitKey(0 if paused and not step else wait) & 0xFF
        if key in (ord("q"), 27) or cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
            break
        if key == ord(" "):
            paused = not paused
        elif key == ord("n") and paused:
            step = True
        elif key == ord("r"):
            new_roi = select_roi(frame)
            if new_roi:
                for run in runs:
                    run.start(frame, new_roi, args.models, args.score)

    capture.release()
    if writer:
        writer.release()
        print(f"wrote {args.output}")
    cv2.destroyAllWindows()
    summarize(runs, frames, decode_ms)


if __name__ == "__main__":
    main()
