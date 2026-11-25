# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""UV island detection and layout checks (overlap, padding, offsets)."""

import numpy as np

from .geometry import edge_loops, fetch

ZERO_UV_AREA = 1e-10
STACKED_SHARE = 0.5  # overlap >= this share of BOTH islands means deliberately stacked copies
RASTER_BUDGET = 1 << 22  # pixels expanded per chunk, to bound memory


# --- UV islands ---

def uv_offset_ok(mod):
    """True if the modifier offsets U or V by at least one whole tile (out of 0-1)."""
    return abs(mod.offset_u) >= 1 - 1e-4 or abs(mod.offset_v) >= 1 - 1e-4


def connected_labels(n, a, b):
    """Component label per node 0..n-1 for the undirected edges a[i]-b[i].

    Vectorised hook-and-shortcut union-find. Each label is the smallest node in its
    component.
    """
    lab = np.arange(n, dtype=np.int64)
    a = np.asarray(a, dtype=np.int64)
    b = np.asarray(b, dtype=np.int64)
    while len(a):
        la, lb = lab[a], lab[b]
        diff = la != lb
        if not diff.any():
            break
        a, b, la, lb = a[diff], b[diff], la[diff], lb[diff]
        np.minimum.at(lab, np.maximum(la, lb), np.minimum(la, lb))  # hook root onto root
        while True:  # shortcut: point every node at its root
            nxt = lab[lab]
            if np.array_equal(nxt, lab):
                break
            lab = nxt
    return lab


def uv_islands(mesh, uv_split):
    """Island id per face: faces joined by edges that are not UV splits."""
    n_faces = len(mesh.polygons)
    loop_edge = fetch(mesh.loops, "edge_index", np.int64)
    loop_total = fetch(mesh.polygons, "loop_total", np.int64)
    loop_face = np.repeat(np.arange(n_faces, dtype=np.int64), loop_total)
    # Only 2-face edges can join faces; edges with 3+ faces always split.
    _, edges, loop_a, loop_b = edge_loops(loop_edge, len(mesh.edges))
    joined = ~np.asarray(uv_split, dtype=bool)[edges]
    return connected_labels(n_faces, loop_face[loop_a[joined]], loop_face[loop_b[joined]])


def zero_uv_faces(tri_uv, tri_face, n_faces):
    """Faces whose UV area, summed over their triangles, is zero.

    Measured per face rather than per triangle: an n-gon with three collinear UV points
    has a zero-area sliver triangle but still covers pixels.
    """
    d = tri_uv[:, 1:] - tri_uv[:, :1]
    area = np.abs(d[:, 0, 0] * d[:, 1, 1] - d[:, 0, 1] * d[:, 1, 0])
    face_area = np.bincount(tri_face, weights=area, minlength=n_faces)
    has_tris = np.bincount(tri_face, minlength=n_faces) > 0
    return has_tris & (face_area < ZERO_UV_AREA)


def triangle_uvs(mesh):
    """Return (tri_uv[n, 3, 2], tri_face[n]) for the active UV map."""
    mesh.calc_loop_triangles()
    n_tris = len(mesh.loop_triangles)
    loops = np.empty(n_tris * 3, dtype=np.int32)
    faces = np.empty(n_tris, dtype=np.int32)
    mesh.loop_triangles.foreach_get("loops", loops)
    mesh.loop_triangles.foreach_get("polygon_index", faces)
    uv = fetch(mesh.uv_layers.active.data, "uv", np.float64, 2)
    return uv[loops].reshape(-1, 3, 2), faces


# --- Rasterisation ---

def _sorted_unique(keys):
    keys = np.sort(keys)
    if len(keys):
        keys = keys[np.concatenate(([True], keys[1:] != keys[:-1]))]
    return keys


def _raster(tri_uv, tri_gid, res, n_ids):
    """Sorted unique keys `pixel * n_ids + gid` for every pixel centre inside a triangle.

    Scanline approach: for each triangle and pixel row, the run of covered pixel
    centres comes from the three barycentric half-planes, so only covered pixels are
    ever expanded.
    """
    p = tri_uv * res
    a, b, c = p[:, 0], p[:, 1], p[:, 2]
    area = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    lo = np.maximum(np.floor(p.min(axis=1)).astype(np.int64), 0)
    hi = np.minimum(np.ceil(p.max(axis=1)).astype(np.int64), res - 1)
    rows = hi[:, 1] - lo[:, 1] + 1
    keep = (np.abs(area) >= 1e-12) & (rows > 0) & (hi[:, 0] >= lo[:, 0])
    t = np.repeat(np.nonzero(keep)[0], rows[keep])
    if not len(t):
        return np.zeros(0, np.int64)
    py = lo[t, 1] + (np.arange(len(t)) - np.repeat(np.cumsum(rows[keep]) - rows[keep], rows[keep]))
    y = py + 0.5
    A, B, C, ar = a[t], b[t], c[t], area[t]
    # Barycentric weight w_i(x) = c_i + d_i * x along this row; inside where all are >= 0.
    d0 = (B[:, 1] - C[:, 1]) / ar
    c0 = (B[:, 0] * (C[:, 1] - y) - C[:, 0] * (B[:, 1] - y)) / ar
    d1 = (C[:, 1] - A[:, 1]) / ar
    c1 = (C[:, 0] * (A[:, 1] - y) - A[:, 0] * (C[:, 1] - y)) / ar
    d2, c2 = -(d0 + d1), 1.0 - c0 - c1
    x_min = np.full(len(t), -np.inf)
    x_max = np.full(len(t), np.inf)
    empty = np.zeros(len(t), dtype=bool)
    with np.errstate(divide='ignore', invalid='ignore'):
        for d, c in ((d0, c0), (d1, c1), (d2, c2)):
            bound = -c / d
            x_min = np.where(d > 0, np.maximum(x_min, bound), x_min)
            x_max = np.where(d < 0, np.minimum(x_max, bound), x_max)
            empty |= (d == 0) & (c < 0)
    # Pixel centres px + 0.5 within [x_min, x_max] and the triangle's box. The small
    # tolerance keeps centres lying exactly on an edge.
    first = np.maximum(np.ceil(x_min - 0.5 - 1e-7), lo[t, 0])
    last = np.minimum(np.floor(x_max - 0.5 + 1e-7), hi[t, 0])
    ok = ~empty & (last >= first)
    first, last = first[ok].astype(np.int64), last[ok].astype(np.int64)
    base = py[ok] * res * n_ids + tri_gid[t[ok]]
    runs = last - first + 1
    out = []
    ends = np.cumsum(runs)
    start = 0
    while start < len(runs):
        offset = ends[start - 1] if start else 0
        stop = max(int(np.searchsorted(ends, offset + RASTER_BUDGET, side='right')), start + 1)
        n = runs[start:stop]
        px = np.repeat(first[start:stop], n) + (
            np.arange(int(n.sum())) - np.repeat(np.cumsum(n) - n, n))
        out.append(_sorted_unique(np.repeat(base[start:stop], n) + px * n_ids))
        start = stop
    return _sorted_unique(np.concatenate(out)) if out else np.zeros(0, np.int64)


# --- Layout check ---

def check_uv_layout(entries, tex_size, padding_px, details=False, keyed=False):
    """Rasterise the islands of several meshes that share a texture.

    entries: list of (label, tri_uv, island_per_tri). Triangles outside the 0-1 tile
    are skipped.

    Returns (overlapping_pairs, too_close_pairs) as lists of (label_a, label_b).
    Overlap is tested at pixel centres; padding is checked at a resolution where the
    padding is 2 pixels wide.

    details=True adds a third item: stacked pairs, islands that each cover most of the
    other (mirrored or duplicated UVs), as ((label, island), (label, island)). Those
    pairs are then left out of the overlapping pairs.

    keyed=True names the overlapping and too-close pairs by (label, island) as well.
    """
    res = int(np.clip(tex_size * 2 / max(padding_px, 1), 64, 2048))
    labels = {}  # gid -> label
    keys = {}  # gid -> (label, island id)
    tris, gids = [], []
    next_id = 0
    for label, tri_uv, tri_island in entries:
        uniq, inv = np.unique(np.asarray(tri_island), return_inverse=True)
        for k, isl in enumerate(uniq.tolist()):
            labels[next_id + k] = label
            keys[next_id + k] = (label, int(isl))
        inside = ((tri_uv >= 0) & (tri_uv <= 1)).all(axis=(1, 2))
        tris.append(np.asarray(tri_uv, dtype=np.float64)[inside])
        gids.append(inv.reshape(-1)[inside] + next_id)
        next_id += len(uniq)
    n_ids = max(next_id, 1)

    # Every (pixel, island) covered by some triangle, deduplicated and sorted by pixel.
    hits = _raster(np.concatenate(tris) if tris else np.zeros((0, 3, 2)),
                   np.concatenate(gids) if gids else np.zeros(0, np.int64), res, n_ids)
    pix, gid = hits // n_ids, hits % n_ids
    area_px = np.bincount(gid, minlength=n_ids)
    owner = np.full(res * res, -1, dtype=np.int64)
    owner[pix] = gid
    owner = owner.reshape(res, res)

    # Islands sharing a pixel overlap. Each island pairs only with the next one up on
    # that pixel, so a stack of n copies gives n-1 pairs rather than every combination.
    idx = np.nonzero(pix[1:] == pix[:-1])[0]
    pair_keys = [gid[idx] * n_ids + gid[idx + 1]] if len(idx) else []
    overlaps = set()
    pair_px = {}
    if pair_keys:
        uniq, counts = np.unique(np.concatenate(pair_keys), return_counts=True)
        for key, count in zip(uniq.tolist(), counts.tolist()):
            pair = (key // n_ids, key % n_ids)
            overlaps.add(pair)
            pair_px[pair] = count

    # Different islands within 2 pixels of each other are too close.
    close = set()
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            if (dx, dy) <= (0, 0) or dx * dx + dy * dy > 4:
                continue
            a = owner[max(dy, 0):res + min(dy, 0), max(dx, 0):res + min(dx, 0)]
            b = owner[max(-dy, 0):res + min(-dy, 0), max(-dx, 0):res + min(-dx, 0)]
            m = (a >= 0) & (b >= 0) & (a != b)
            if m.any():
                pairs = np.unique(np.stack([np.minimum(a[m], b[m]), np.maximum(a[m], b[m])], 1),
                                  axis=0)
                close.update(map(tuple, pairs.tolist()))
    close -= overlaps

    if keyed:
        def named(pairs):
            return [(keys[x], keys[y]) for x, y in sorted(pairs)]
    else:
        def named(pairs):
            return [(labels[x], labels[y]) for x, y in pairs]

    if not details:
        return named(overlaps), named(close)
    # Measured against the larger island, so a big island over small ones is only an
    # overlap, not a stack.
    stacked = {p for p in overlaps
               if pair_px[p] >= STACKED_SHARE * max(int(area_px[p[0]]), int(area_px[p[1]]))}
    return (named(overlaps - stacked), named(close),
            [(keys[a], keys[b]) for a, b in sorted(stacked)])
