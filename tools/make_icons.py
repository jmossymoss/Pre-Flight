# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Draw Pre-Flight's colour icons (PNG, 32x32) into preflight/icons/.

usage: python tools/make_icons.py

Three shapes, all anti-aliased, so headers and sub-headers differ in shape and size:
  badge_<name>   a large rounded square: panel headers
  pip_<name>     a small circle: section titles inside a panel
  dot_<name>     a medium circle: issue severities in lists and counts
  tab_<name>     a pictogram in the accent colour: the panel's tab strip
Pure Python + numpy, so it runs without Blender or PIL.
"""

import struct
import zlib
from pathlib import Path

import numpy as np

SIZE = 32
SS = 4  # supersampling for anti-aliasing

COLOURS = {
    # panel accents
    "shading": "#4C8EDA",
    "uvs": "#4FB477",
    "pairs": "#E8963A",
    "checks": "#A27BE0",
    "baker": "#D9607F",
    "rigging": "#D8B640",
    "export": "#3FB8C0",
    # severities
    "error": "#E2504C",
    "warning": "#EBB23A",
    "info": "#5B9BD5",
    "ok": "#56B865",
}


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def coverage(inside):
    """Anti-aliased coverage of a shape given inside(x, y) on supersampled pixel centres."""
    n = SIZE * SS
    c = (np.arange(n) + 0.5) / SS
    x, y = np.meshgrid(c, c)
    return inside(x, y).reshape(SIZE, SS, SIZE, SS).mean(axis=(1, 3))


def dot(x, y):
    return (x - 16) ** 2 + (y - 16) ** 2 <= 8.5 ** 2


def rounded_rect(x, y, x0, x1, y0, y1, r):
    cx = np.clip(x, x0 + r, x1 - r)
    cy = np.clip(y, y0 + r, y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r ** 2


def badge(x, y):
    return rounded_rect(x, y, 3, 29, 3, 29, 7)


def pip(x, y):
    return (x - 16) ** 2 + (y - 16) ** 2 <= 5 ** 2


def write_png(path, rgba):
    h, w, _ = rgba.shape
    raw = b"".join(b"\x00" + rgba[row].tobytes() for row in range(h))

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 9))
                     + chunk(b"IEND", b""))


def icon(shape, colour):
    alpha = coverage(shape)
    rgba = np.zeros((SIZE, SIZE, 4), dtype=np.uint8)
    rgba[..., :3] = hex_rgb(colour)
    rgba[..., 3] = np.round(alpha * 255).astype(np.uint8)
    return rgba


# ---------------------------------------------------------------------------
# Tab pictograms (Blender-style symbols, tinted with the accent colours)
# ---------------------------------------------------------------------------

INK = (32, 32, 32)       # dark detail (the warning's "!")
WHITE = (255, 255, 255)


def seg_dist(x, y, a, b):
    """Distance from (x, y) to the segment a-b."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    t = np.clip(((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy), 0, 1)
    return np.hypot(x - (ax + t * dx), y - (ay + t * dy))


def stroke(points, width, closed=False):
    pts = list(points) + ([points[0]] if closed else [])

    def inside(x, y):
        d = np.min([seg_dist(x, y, a, b) for a, b in zip(pts, pts[1:])], axis=0)
        return d <= width / 2
    return inside


def polygon(points):
    """Even-odd fill of a polygon."""
    def inside(x, y):
        res = np.zeros(x.shape, dtype=bool)
        n = len(points)
        for i in range(n):
            (x1, y1), (x2, y2) = points[i], points[(i + 1) % n]
            crosses = (y1 > y) != (y2 > y)
            with np.errstate(divide="ignore", invalid="ignore"):
                xc = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            res ^= crosses & (x < xc)
        return res
    return inside


def circle(cx, cy, r):
    return lambda x, y: (x - cx) ** 2 + (y - cy) ** 2 <= r ** 2


def ring(cx, cy, r, width):
    return lambda x, y: np.abs(np.hypot(x - cx, y - cy) - r) <= width / 2


def compose(layers):
    """Paint (shape, rgb, opacity) layers in order. rgb may be an (H, W, 3) array."""
    rgb = np.zeros((SIZE, SIZE, 3))
    alpha = np.zeros((SIZE, SIZE))
    for shape, colour, opacity in layers:
        a = coverage(shape) * opacity
        c = np.broadcast_to(np.asarray(colour, dtype=float), (SIZE, SIZE, 3))
        out_a = a + alpha * (1 - a)
        with np.errstate(divide="ignore", invalid="ignore"):
            rgb = np.where(out_a[..., None] > 0,
                           (c * a[..., None] + rgb * alpha[..., None] * (1 - a[..., None]))
                           / out_a[..., None], 0)
        alpha = out_a
    rgba = np.zeros((SIZE, SIZE, 4), dtype=np.uint8)
    rgba[..., :3] = np.round(np.clip(rgb, 0, 255)).astype(np.uint8)
    rgba[..., 3] = np.round(alpha * 255).astype(np.uint8)
    return rgba


def ngon(cx, cy, r, n, start=0.0):
    return [(cx + r * np.cos(a), cy + r * np.sin(a))
            for a in np.radians(start + np.arange(n) * 360 / n)]


def shade(colour, k):
    """The colour darkened (k < 1) or lightened toward white (k > 1)."""
    c = np.array(hex_rgb(colour), dtype=float)
    return tuple(c * k if k <= 1 else c + (255 - c) * (k - 1))


def tab_shading(col):    # a flat-shaded sphere: disc, crescent shadow, highlight
    disc = circle(16, 16, 12.5)
    shadow = lambda x, y: disc(x, y) & ~circle(12.5, 12.5, 12.5)(x, y)
    return compose([(disc, hex_rgb(col), 1),
                    (shadow, shade(col, 0.6), 1),
                    (circle(11.5, 11.5, 3), shade(col, 1.6), 1)])


def tab_uvs(col):        # a UV checker
    def cells(on):
        def inside(x, y):
            box = (x >= 4) & (x < 28) & (y >= 4) & (y < 28)
            check = ((np.floor((x - 4) / 8) + np.floor((y - 4) / 8)) % 2 == 0)
            return box & (check == on)
        return inside
    return compose([(cells(True), hex_rgb(col), 1), (cells(False), hex_rgb(col), 0.35)])


def tab_pairs(col):      # a faceted low-poly shape over a smooth, paler high
    return compose([(circle(19.5, 18.5, 10), hex_rgb(col), 0.45),
                    (polygon(ngon(12.5, 13.5, 9.5, 6, 30)), hex_rgb(col), 1)])


def croissant(cx, cy, radius, a0, a1, thick, thin):
    """inside(x, y) for a croissant: an arched band, thick in the middle and tapering
    to rounded tips, from angle a0 to a1 (degrees, y down)."""
    def taper(x, y):
        ang = np.degrees(np.arctan2(y - cy, x - cx)) % 360
        t = (ang - a0) / (a1 - a0)
        w = thin + (thick - thin) * np.sin(np.pi * np.clip(t, 0, 1))
        return ang, t, np.abs(np.hypot(x - cx, y - cy) - radius) <= w / 2

    def inside(x, y):
        _, t, band = taper(x, y)
        tips = np.zeros(x.shape, dtype=bool)
        for a in (a0, a1):
            r = np.radians(a)
            tips |= circle(cx + radius * np.cos(r), cy + radius * np.sin(r), thin / 2)(x, y)
        return (band & (t >= 0) & (t <= 1)) | tips

    def ridges(x, y):
        ang, t, band = taper(x, y)
        d = np.hypot(x - cx, y - cy)
        near = np.zeros(x.shape, dtype=bool)
        for k in (0.22, 0.5, 0.78):
            a = a0 + (a1 - a0) * k
            near |= np.abs(np.radians(ang - a) * d) <= 0.75
        return band & near & (t >= 0) & (t <= 1)
    return inside, ridges


def tab_baker(col):      # a croissant (it's a baker)
    body, ridges = croissant(16, 28, 13, 197, 343, 14, 4.5)
    return compose([(body, hex_rgb(col), 1), (ridges, shade(col, 0.6), 1)])


def tab_rigging(col):    # a bone: two knuckles at each end of a shaft
    ends = [(8.5, 23.5), (23.5, 8.5)]
    knobs = [circle(5.6, 22.4, 3.3), circle(9.6, 26.4, 3.3),
             circle(22.4, 5.6, 3.3), circle(26.4, 9.6, 3.3)]
    bone = lambda x, y: (stroke(ends, 5)(x, y) | knobs[0](x, y) | knobs[1](x, y)
                         | knobs[2](x, y) | knobs[3](x, y))
    return compose([(bone, hex_rgb(col), 1),
                    (stroke([(11, 21), (21, 11)], 1.4), shade(col, 1.5), 0.8)])


def tab_export(col):     # a box with an arrow leaving it
    box = stroke([(5, 12), (5, 27), (20, 27), (20, 12)], 3, closed=True)
    arrow = stroke([(12, 19), (25, 6)], 3)
    head = polygon([(27.5, 3.5), (27.5, 13), (18, 3.5)])
    return compose([(box, hex_rgb(col), 1), (arrow, hex_rgb(col), 1), (head, hex_rgb(col), 1)])


def tab_checks(col):     # a tick in a disc
    return compose([(circle(16, 16, 12.5), hex_rgb(col), 1),
                    (stroke([(10, 16.5), (14.5, 21), (22.5, 11.5)], 3.2), WHITE, 1)])


def tab_issues(col):     # a warning triangle with "!"
    tri = stroke([(16, 5), (28, 26), (4, 26)], 4, closed=True)
    fill = polygon([(16, 5), (28, 26), (4, 26)])
    return compose([(lambda x, y: tri(x, y) | fill(x, y), hex_rgb(col), 1),
                    (stroke([(16, 12), (16, 19)], 3), INK, 1),
                    (circle(16, 23, 1.7), INK, 1)])


OUTLINE_WIDTH = 2.0      # icon pixels (1px at the 16px Blender draws icons at)
OUTLINE_COLOUR = (0, 0, 0)


def outlined(rgba, width=OUTLINE_WIDTH, colour=OUTLINE_COLOUR):
    """Put a dark outline round an icon: its alpha grown by `width`, drawn underneath."""
    alpha = rgba[..., 3] / 255
    grown = np.zeros_like(alpha)
    r = int(np.ceil(width))
    padded = np.pad(alpha, r)
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            # soft edge: full inside the width, fading over the last pixel
            w = np.clip(width + 0.5 - np.hypot(dx, dy), 0, 1)
            if w > 0:
                shifted = padded[r + dy:r + dy + SIZE, r + dx:r + dx + SIZE]
                grown = np.maximum(grown, shifted * w)
    a = alpha[..., None]
    out_a = a + grown[..., None] * (1 - a)
    with np.errstate(divide="ignore", invalid="ignore"):
        rgb = np.where(out_a > 0, (rgba[..., :3] * a + np.array(colour) * grown[..., None]
                                   * (1 - a)) / out_a, 0)
    res = np.zeros_like(rgba)
    res[..., :3] = np.round(rgb).astype(np.uint8)
    res[..., 3] = np.round(out_a[..., 0] * 255).astype(np.uint8)
    return res


TABS = {
    "tab_shading": (tab_shading, "shading"),
    "tab_uvs": (tab_uvs, "uvs"),
    "tab_pairs": (tab_pairs, "pairs"),
    "tab_baker": (tab_baker, "baker"),
    "tab_checks": (tab_checks, "checks"),
    "tab_rigging": (tab_rigging, "rigging"),
    "tab_export": (tab_export, "export"),
    "tab_issues_error": (tab_issues, "error"),
    "tab_issues_warning": (tab_issues, "warning"),
    "tab_issues_ok": (tab_issues, "ok"),
    "tab_issues_info": (tab_issues, "info"),
}


def main():
    out = Path(__file__).resolve().parent.parent / "preflight" / "icons"
    out.mkdir(exist_ok=True)
    for old in out.glob("*.png"):
        old.unlink()
    shapes = {"badge": badge, "pip": pip, "dot": dot}
    for name, colour in COLOURS.items():
        for prefix, shape in shapes.items():
            write_png(out / f"{prefix}_{name}.png", icon(shape, colour))
    for name, (draw, colour) in TABS.items():
        write_png(out / f"{name}.png", outlined(draw(COLOURS[colour])))
    print(f"wrote {len(shapes) * len(COLOURS) + len(TABS)} icons to {out}")


if __name__ == "__main__":
    main()
