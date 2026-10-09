"""Slide figures for PointLock: real OpenCV calls on a synthetic aerial scene."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
rng = np.random.default_rng(7)

W, H = 1920, 1080          # frame size
WORLD = (2600, 1700)       # world image (w, h)
SCALE = 640 / W            # PointLock.WORK_WIDTH

# ---------------------------------------------------------------- world scene
def make_world() -> np.ndarray:
    ww, wh = WORLD
    img = np.zeros((wh, ww, 3), np.uint8)
    palette = [(70, 120, 80), (60, 105, 70), (95, 140, 110), (80, 130, 150), (70, 110, 125), (100, 150, 140)]
    # fields
    x = 0
    while x < ww:
        fw = int(rng.integers(220, 420))
        y = 0
        while y < wh:
            fh = int(rng.integers(200, 380))
            color = palette[int(rng.integers(len(palette)))]
            cv2.rectangle(img, (x, y), (x + fw, y + fh), color, -1)
            # crop rows
            if rng.random() < 0.6:
                dark = tuple(max(0, c - 18) for c in color)
                step = int(rng.integers(10, 18))
                for r in range(y, y + fh, step):
                    cv2.line(img, (x, r), (x + fw, r), dark, 2)
            y += fh
        x += fw
    noise = rng.normal(0, 9, img.shape).astype(np.float32)
    img = np.clip(img.astype(np.float32) + cv2.GaussianBlur(noise, (0, 0), 1.2), 0, 255).astype(np.uint8)
    # roads
    for pts in ([(0, 900), (900, 860), (1700, 980), (2600, 940)], [(1250, 0), (1300, 700), (1220, 1700)]):
        pts = np.array(pts, np.int32)
        cv2.polylines(img, [pts], False, (95, 95, 95), 46, cv2.LINE_AA)
        cv2.polylines(img, [pts], False, (210, 210, 210), 2, cv2.LINE_AA)
    # buildings with shadows
    for _ in range(70):
        bx, by = int(rng.integers(0, ww - 120)), int(rng.integers(0, wh - 120))
        if abs(bx - 1500) < 260 and abs(by - 620) < 200:
            continue  # keep the target field clear
        bw, bh = int(rng.integers(40, 120)), int(rng.integers(40, 110))
        cv2.rectangle(img, (bx + 10, by + 12), (bx + bw + 10, by + bh + 12), (35, 45, 40), -1)
        roof = [(70, 70, 160), (150, 150, 155), (60, 90, 140), (180, 180, 185)][int(rng.integers(4))]
        cv2.rectangle(img, (bx, by), (bx + bw, by + bh), roof, -1)
        cv2.line(img, (bx, by + bh // 2), (bx + bw, by + bh // 2), tuple(c - 40 for c in roof), 2)
    # trees
    for _ in range(260):
        tx, ty = int(rng.integers(0, ww)), int(rng.integers(0, wh))
        if abs(tx - 1500) < 260 and abs(ty - 620) < 200:
            continue
        r = int(rng.integers(8, 18))
        cv2.circle(img, (tx + 4, ty + 5), r, (25, 40, 30), -1, cv2.LINE_AA)
        cv2.circle(img, (tx, ty), r, (40, int(rng.integers(80, 110)), 50), -1, cv2.LINE_AA)
    # the target: a plain, featureless field
    cv2.rectangle(img, (1260, 440), (1740, 800), (85, 150, 120), -1)
    img[440:800, 1260:1740] = cv2.GaussianBlur(img[440:800, 1260:1740], (0, 0), 6)
    return img


def draw_truck(img: np.ndarray, cx: float, cy: float) -> None:
    """A big textured vehicle so RANSAC has something to reject."""
    x, y = int(cx), int(cy)
    cv2.rectangle(img, (x - 150 + 8, y - 48 + 10), (x + 150 + 8, y + 48 + 10), (30, 30, 30), -1)
    cv2.rectangle(img, (x - 150, y - 48), (x + 90, y + 48), (40, 170, 230), -1)
    # irregular cargo so features are distinct (repeating stripes fool the flow)
    cargo = np.random.default_rng(3)
    for _ in range(14):
        bx, by = x - 145 + int(cargo.integers(0, 200)), y - 44 + int(cargo.integers(0, 60))
        bw, bh = int(cargo.integers(14, 40)), int(cargo.integers(12, 30))
        shade = [(20, 110, 170), (230, 230, 240), (30, 60, 200), (60, 200, 255)][int(cargo.integers(4))]
        cv2.rectangle(img, (bx, by), (min(bx + bw, x + 88), min(by + bh, y + 46)), shade, -1)
    cv2.rectangle(img, (x + 95, y - 44), (x + 150, y + 44), (200, 200, 205), -1)
    cv2.rectangle(img, (x + 120, y - 36), (x + 145, y + 36), (60, 50, 40), -1)
    for dx in (-110, -40, 40, 120):
        for dy in (-52, 52):
            cv2.circle(img, (x + dx, y + dy), 9, (20, 20, 20), -1)


world = make_world()
target_world = np.array([1500.0, 620.0])

# Frame 1 looks at an offset; frame 2 has panned, rotated and zoomed a bit.
def view(offset: tuple[float, float], angle_deg: float, zoom: float) -> np.ndarray:
    """2x3 map from frame pixel to world pixel."""
    a = math.radians(angle_deg)
    c, s = math.cos(a) / zoom, math.sin(a) / zoom
    cx, cy = W / 2, H / 2
    # rotate/scale about the frame centre, then place the centre in the world
    return np.array([[c, -s, offset[0] - c * cx + s * cy], [s, c, offset[1] - s * cx - c * cy]])

M1 = view((1260, 760), 0.0, 1.0)
M2 = view((1310, 740), 2.0, 1.04)
truck1, truck2 = (960.0, 870.0), (900.0, 870.0)


def render(M: np.ndarray, truck: tuple[float, float]) -> np.ndarray:
    scene = world.copy()
    draw_truck(scene, *truck)
    return cv2.warpAffine(scene, M, (W, H), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP)


def to_frame(M: np.ndarray, p: np.ndarray) -> np.ndarray:
    full = np.vstack([M, [0, 0, 1]])
    return (np.linalg.inv(full) @ np.array([p[0], p[1], 1.0]))[:2]


f1, f2 = render(M1, truck1), render(M2, truck2)
click = to_frame(M1, target_world)
truth = to_frame(M2, target_world)

# ---------------------------------------------------------------- PointLock's steps
def gray(frame: np.ndarray) -> np.ndarray:
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return cv2.resize(g, (640, round(H * SCALE)), interpolation=cv2.INTER_AREA)

g1, g2 = gray(f1), gray(f2)
p0 = cv2.goodFeaturesToTrack(g1, 300, qualityLevel=0.01, minDistance=8, blockSize=7)
p1, st, _ = cv2.calcOpticalFlowPyrLK(g1, g2, p0, None, winSize=(21, 21), maxLevel=3)
back, st_b, _ = cv2.calcOpticalFlowPyrLK(g2, g1, p1, None, winSize=(21, 21), maxLevel=3)
fb_err = np.linalg.norm(p0 - back, axis=2).ravel()
good = (st.ravel() == 1) & (st_b.ravel() == 1) & (fb_err < 1.0)
Hm, mask = cv2.findHomography(p0[good], p1[good], cv2.RANSAC, 3.0)
inl = mask.ravel() == 1
before, after = p0[good][inl], p1[good][inl]
pt_work = click * SCALE
dist = np.linalg.norm(before.reshape(-1, 2) - pt_work, axis=1)
nearest = np.argsort(dist)[:40]
A, _ = cv2.estimateAffinePartial2D(before[nearest], after[nearest], method=cv2.LMEDS)
S = np.diag([SCALE, SCALE, 1.0])
full = np.linalg.inv(S) @ np.vstack([A, [0, 0, 1]]) @ S
tracked = (full @ np.array([click[0], click[1], 1.0]))[:2]

n_feat, n_fb, n_in = len(p0), int((~good).sum()), int(inl.sum())
n_out = int(good.sum()) - n_in
err_px = float(np.linalg.norm(tracked - truth))
print(f"features={n_feat} fb_rejected={n_fb} inliers={n_in} outliers={n_out} "
      f"score={n_in / n_feat:.2f} tracked_err={err_px:.1f}px moved={np.linalg.norm(truth - click):.0f}px")

full_res = lambda p: p.reshape(-1, 2) / SCALE  # noqa: E731
P0, P1 = full_res(p0), full_res(p1)
G0, G1 = P0[good], P1[good]

# ---------------------------------------------------------------- drawing
def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "arialbd.ttf" if bold else "arial.ttf"
    try:
        return ImageFont.truetype(f"C:/Windows/Fonts/{name}", size)
    except OSError:
        return ImageFont.load_default()

CYAN, YELLOW, GREEN, RED, GREY, WHITE, MAGENTA = (
    (255, 220, 60), (60, 220, 255), (90, 230, 90), (70, 70, 255), (150, 150, 150), (255, 255, 255), (230, 120, 255),
)  # BGR


def dim(frame: np.ndarray, k: float = 0.45) -> np.ndarray:
    return (frame.astype(np.float32) * k).astype(np.uint8)


def ip(p) -> tuple[int, int]:
    return int(round(p[0])), int(round(p[1]))


def reticle(img, p, color, r=34, t=4):
    c = ip(p)
    cv2.circle(img, c, r, color, t, cv2.LINE_AA)
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        cv2.line(img, (c[0] + dx * r // 2, c[1] + dy * r // 2), (c[0] + dx * r * 3 // 2, c[1] + dy * r * 3 // 2),
                 color, t, cv2.LINE_AA)


def arrow(img, a, b, color, t=3):
    cv2.arrowedLine(img, ip(a), ip(b), color, t, cv2.LINE_AA, tipLength=0.3)


def caption(img, title, sub, legend=()):
    """Title bar plus a legend, drawn with PIL for nicer type."""
    pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    d = ImageDraw.Draw(pil, "RGBA")
    d.rectangle((0, 0, W, 150), fill=(0, 0, 0, 175))
    d.text((48, 26), title, font=font(54, True), fill=(255, 255, 255))
    d.text((50, 96), sub, font=font(30), fill=(210, 210, 210))
    if legend:
        f = font(28)
        rows = len(legend)
        y0 = H - 40 - rows * 46
        width = 60 + max(d.textlength(t, font=f) for _, t in legend) + 40
        d.rounded_rectangle((32, y0 - 20, 32 + width, H - 32), 14, fill=(0, 0, 0, 175))
        for i, (bgr, text) in enumerate(legend):
            y = y0 + i * 46
            rgb = bgr[::-1]
            d.ellipse((56, y + 6, 78, y + 28), fill=rgb)
            d.text((96, y), text, font=f, fill=(240, 240, 240))
    return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)


def spread(points: np.ndarray, cell: int) -> np.ndarray:
    """Indices of at most one point per cell, so a sample covers the frame evenly."""
    seen, keep = set(), []
    for i, (x, y) in enumerate(points.reshape(-1, 2)):
        key = (int(x // cell), int(y // cell))
        if key not in seen and 0 <= x < W and 160 <= y < H:  # skip under the title bar
            seen.add(key)
            keep.append(i)
    return np.array(keep, dtype=int)


panels = []

# 1. corners
img = dim(f1, 0.4)
for i in spread(P0, 130):
    cv2.circle(img, ip(P0[i]), 8, CYAN, -1, cv2.LINE_AA)
reticle(img, click, MAGENTA)
panels.append(caption(img, "1. Find corners across the whole frame",
                      "Corners are easy to follow. The clicked spot is plain grass: it has none.",
                      [(CYAN, "Feature to track"), (MAGENTA, "Clicked spot")]))

# 2. flow
img = dim(f2, 0.4)
for i in spread(G1, 220):
    arrow(img, G0[i], G1[i], YELLOW, 4)
bad = P1[~good]
for i in spread(bad, 400)[:4]:
    c = ip(bad[i])
    cv2.line(img, (c[0] - 9, c[1] - 9), (c[0] + 9, c[1] + 9), GREY, 4, cv2.LINE_AA)
    cv2.line(img, (c[0] - 9, c[1] + 9), (c[0] + 9, c[1] - 9), GREY, 4, cv2.LINE_AA)
panels.append(caption(img, "2. Follow each corner into the next frame",
                      "Lucas-Kanade optical flow. Matches that don't track back are dropped.",
                      [(YELLOW, "How a feature moved"), (GREY, "Dropped: unreliable match")]))

# 3. RANSAC
img = dim(f2, 0.4)
I0, I1 = G0[inl], G1[inl]
O0, O1 = G0[~inl], G1[~inl]
for i in spread(I1, 220):
    arrow(img, I0[i], I1[i], GREEN, 4)
for i in spread(O1, 70):
    arrow(img, O0[i], O1[i], RED, 5)
panels.append(caption(img, "3. RANSAC: which features move with the scene?",
                      "One fitted motion explains the camera. The truck doesn't fit it, so it's ignored.",
                      [(GREEN, "Moves with the scene"), (RED, "Moving object: rejected")]))

# 4. move the point
img = dim(f2, 0.4)
nb0, nb1 = before.reshape(-1, 2)[nearest] / SCALE, after.reshape(-1, 2)[nearest] / SCALE
radius = int(np.linalg.norm(nb1 - tracked, axis=1).max()) + 30
overlay = img.copy()
cv2.circle(overlay, ip(tracked), radius, GREEN, -1, cv2.LINE_AA)
img = cv2.addWeighted(overlay, 0.12, img, 0.88, 0)
cv2.circle(img, ip(tracked), radius, GREEN, 2, cv2.LINE_AA)
for i in spread(nb1, 150):
    arrow(img, nb0[i], nb1[i], GREEN, 4)
cv2.circle(img, ip(click), 34, MAGENTA, 3, cv2.LINE_AA)
arrow(img, click, tracked, MAGENTA, 5)
reticle(img, tracked, MAGENTA)
panels.append(caption(img, "4. Move the spot the way its neighbours moved",
                      f"Fit to the 40 nearest scene features. Error vs. ground truth: {err_px:.1f} px.",
                      [(GREEN, "Nearby features used"), (MAGENTA, "Spot: before → after")]))

for i, p in enumerate(panels, 1):
    cv2.imwrite(str(OUT / f"optical-flow-step{i}.png"), p)

# 2x2 overview
small = [cv2.resize(p, (W // 2, H // 2), interpolation=cv2.INTER_AREA) for p in panels]
grid = np.vstack([np.hstack(small[:2]), np.hstack(small[2:])])
cv2.line(grid, (W // 2, 0), (W // 2, H), (20, 20, 20), 4)
cv2.line(grid, (0, H // 2), (W, H // 2), (20, 20, 20), 4)
cv2.imwrite(str(OUT / "optical-flow-overview.png"), grid)

# single-slide summary of the whole idea
img = dim(f2, 0.4)
clear = lambda q: not (q[0] < 760 and q[1] > 820)  # noqa: E731 - keep the legend corner empty
for i in spread(I1, 200):
    if clear(I1[i]) and clear(I0[i]):
        arrow(img, I0[i], I1[i], GREEN, 4)
for i in spread(O1, 70):
    arrow(img, O0[i], O1[i], RED, 5)
cv2.circle(img, ip(click), 12, MAGENTA, 3, cv2.LINE_AA)
reticle(img, tracked, MAGENTA, r=26)
concept = caption(img, "Point lock: track the scene, not the spot",
                  "Follow corners between frames, ignore what moves differently, move the spot with its neighbours.",
                  [(GREEN, "Background: shows how the camera moved"), (RED, "Moving truck: ignored"),
                   (MAGENTA, "Locked spot: carried along with the scene")])
cv2.imwrite(str(OUT / "optical-flow-concept.png"), concept)

# raw frames, for reference
cv2.imwrite(str(OUT / "frame-before.png"), f1)
cv2.imwrite(str(OUT / "frame-after.png"), f2)
