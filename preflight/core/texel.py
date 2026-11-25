# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Texel density per UV island, in pixels per metre at the bake resolution."""

import numpy as np

from .geometry import fetch

# Display units: key -> (label, metres per unit). Densities are always stored in px/m.
UNITS = {
    'M': ("px/m", 1.0),
    'CM': ("px/cm", 0.01),
    'MM': ("px/mm", 0.001),
    'IN': ("px/in", 0.0254),
    'FT': ("px/ft", 0.3048),
}

# Texel density overlay colours (RGB).
LOW_COLOUR = np.array((0.20, 0.45, 1.00))
OK_COLOUR = np.array((0.25, 0.85, 0.35))
HIGH_COLOUR = np.array((1.00, 0.25, 0.20))
NONE_COLOUR = np.array((0.50, 0.50, 0.50))


# --- Units ---

def unit_items():
    """EnumProperty items for UNITS."""
    return [(k, label, f"Show texel density in pixels per {label[3:]}")
            for k, (label, _) in UNITS.items()]


def to_unit(px_per_m, unit):
    """Convert a density from px/m to `unit`."""
    return px_per_m * UNITS.get(unit, UNITS['M'])[1]


def from_unit(value, unit):
    """Convert a density from `unit` to px/m."""
    return value / UNITS.get(unit, UNITS['M'])[1]


def fmt(px_per_m, unit='M', label=True):
    """Format a px/m density in `unit`, e.g. '10.24 px/cm'."""
    value = to_unit(px_per_m, unit)
    text = f"{value:.0f}" if abs(value) >= 100 else f"{value:.2f}"
    return f"{text} {UNITS.get(unit, UNITS['M'])[0]}" if label else text


# --- Island densities ---

def island_densities(obj, mesh, tri_uv, tri_face, face_island, tex_size, scale_length=1.0):
    """Per island inside the 0-1 tile: (island ids, density in px/m, world area in m^2).

    tri_uv and tri_face come from uv_layout.triangle_uvs, face_island from uv_islands.
    Islands with zero world or UV area are left out.
    """
    if not len(tri_uv):
        return np.empty(0, np.int64), np.empty(0), np.empty(0)
    mesh.calc_loop_triangles()
    co = fetch(mesh.vertices, "co", np.float64, 3)
    mat = np.array(obj.matrix_world)
    co = co @ mat[:3, :3].T + mat[:3, 3]
    tris = fetch(mesh.loop_triangles, "vertices", np.int64, 3)
    p = co[tris]
    world = 0.5 * np.linalg.norm(np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]), axis=1)
    world *= scale_length ** 2
    d = tri_uv[:, 1:] - tri_uv[:, :1]
    uv = 0.5 * np.abs(d[:, 0, 0] * d[:, 1, 1] - d[:, 0, 1] * d[:, 1, 0])

    centres = tri_uv.mean(axis=1)
    inside = ((centres >= 0) & (centres <= 1)).all(axis=1)
    tri_island = face_island[tri_face]
    ids, inverse = np.unique(tri_island[inside], return_inverse=True)
    world_sum = np.bincount(inverse, weights=world[inside], minlength=len(ids))
    uv_sum = np.bincount(inverse, weights=uv[inside], minlength=len(ids))
    keep = (world_sum > 1e-12) & (uv_sum > 1e-12)  # zero UV area is reported separately
    density = tex_size * np.sqrt(uv_sum[keep] / world_sum[keep])
    return ids[keep], density, world_sum[keep]


def weighted_median(values, weights):
    """Median of `values` weighted by `weights`."""
    order = np.argsort(values)
    v, w = values[order], weights[order]
    cum = np.cumsum(w)
    return float(v[np.searchsorted(cum, cum[-1] / 2)])


def outlier_islands(ids, density, median, tolerance):
    """Island ids (and densities) more than `tolerance` (0.25 = 25%) off the median."""
    off = np.abs(density / median - 1) > tolerance
    return ids[off], density[off]


def face_mask(mesh, face_island, island_ids):
    """Boolean face mask of the given islands."""
    return np.isin(face_island, island_ids)


# --- Overlay ---

def triangle_densities(obj, mesh, tex_size, scale_length=1.0):
    """Return (per-triangle density in px/m, island ids, densities, world areas).

    Triangles follow mesh.loop_triangles. Triangles in islands outside the 0-1 tile or
    with zero UV area get NaN.
    """
    from .edges import edge_analysis
    from .uv_layout import triangle_uvs, uv_islands
    uv_split, _, _ = edge_analysis(mesh)
    islands = np.asarray(uv_islands(mesh, uv_split))
    tri_uv, tri_face = triangle_uvs(mesh)
    ids, density, world = island_densities(obj, mesh, tri_uv, tri_face, islands,
                                           tex_size, scale_length)
    per_tri = np.full(len(tri_face), np.nan)
    if len(ids):
        lookup = np.full(int(islands.max()) + 1, np.nan)
        lookup[ids] = density
        per_tri = lookup[islands[tri_face]]
    return per_tri, ids, density, world


def density_colours(per_tri, target, tolerance, palette=None, alpha=0.5):
    """RGBA per triangle for the texel density overlay.

    Green within `tolerance` (0.25 = 25%) of `target`, shading towards blue at half the
    target and red at double; grey where there is no density.
    """
    low, ok, high = (np.asarray(c, dtype=np.float64)[:3] for c in
                     (palette or (LOW_COLOUR, OK_COLOUR, HIGH_COLOUR)))
    out = np.empty((len(per_tri), 4))
    out[:, 3] = alpha
    out[:, :3] = NONE_COLOUR
    has = ~np.isnan(per_tri)
    ratio = per_tri[has] / target
    t = np.clip(np.log2(ratio), -1, 1)
    band = np.log2(1 + tolerance)
    k = np.clip((np.abs(t) - band) / max(1 - band, 1e-6), 0, 1)[:, None]  # distance past band
    end = np.where((t < 0)[:, None], low, high)
    rgb = ok + (end - ok) * (0.35 + 0.65 * k)
    rgb[np.abs(ratio - 1) <= tolerance] = ok
    out[has, :3] = rgb
    return out
