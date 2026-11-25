# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Bake map resolution (width x height) and the values derived from it.

UVs for a non-square W x H map are stretched to its ratio in UV space, so a square
island covers a square of pixels. Padding is checked and packed at the shorter side
(the stricter axis), and texel density uses sqrt(W * H) pixels per UV unit.

Helpers that take `s` accept the scene settings or the result of of() / of_object(),
which has the same attribute names.
"""

import math
from types import SimpleNamespace

# Custom properties stored on the mesh by match_uv_ratio().
RATIO_PROP = "preflight_uv_ratio"  # width / height the UVs are drawn for
SHAPE_PROP = "preflight_uv_shapes"  # island shapes right after the match
AXIS_PROP = "preflight_uv_axis"  # the STRETCH_AXES option used

SHAPE_TOLERANCE = 0.02  # median change in log(width / height) still counted as unchanged

STRETCH_AXES = (
    ('BOTH', "Both (Keep Size)", "Squash one way and stretch the other by the same amount: "
                                 "islands keep their UV area and texel density"),
    ('U', "Width Only", "Change only the islands' width (U); their height stays"),
    ('V', "Height Only", "Change only the islands' height (V); their width stays"),
)


# --- Map size ---

def of(s, mat=None):
    """Map size settings for texture set (material) `mat`.

    Uses the material's own size when it has one, else the scene's. Padding settings
    are always scene-wide.
    """
    own = getattr(mat, "preflight", None) if mat is not None else None
    if own is None or not own.use_own_size:
        return s
    return SimpleNamespace(texture_size=own.texture_size, texture_height=own.texture_height,
                           texture_square=own.texture_square, padding_auto=s.padding_auto,
                           padding=s.padding)


def of_object(s, obj):
    """Map size settings for `obj`'s texture set (its active material)."""
    return of(s, obj.active_material if obj is not None else None)


def map_size(s):
    """Return (width, height) in pixels."""
    w = int(s.texture_size)
    return w, (w if s.texture_square else int(s.texture_height))


def is_square(s):
    w, h = map_size(s)
    return w == h


def aspect(s):
    """Width / height."""
    w, h = map_size(s)
    return w / h


def label(s):
    """Display label: the width for square maps, else width and height."""
    w, h = map_size(s)
    return str(w) if w == h else f"{w}×{h}"


def padding_size(s):
    """The side padding is measured at: the shorter one, where a pixel is widest in UV."""
    return min(map_size(s))


def texel_size(s):
    """Pixels per UV unit for texel density (geometric mean of the two sides)."""
    w, h = map_size(s)
    return math.sqrt(w * h)


def suggested_padding(size):
    """size / 128, at least 4: 4 px at 512 and below, 8 at 1024, 16 at 2048, ..."""
    return max(4, int(size) // 128)


def padding(s):
    """Padding in pixels: derived from the map size (Auto) or set by hand."""
    return suggested_padding(padding_size(s)) if s.padding_auto else s.padding


def uv_margin(s):
    """Padding as a UV distance, at the shorter side so both axes get at least that."""
    return padding(s) / padding_size(s)


# --- UVs drawn for a map ratio ---

def recorded_ratio(mesh):
    """The ratio Match UVs to Map Ratio last stretched the UVs to (1 if never)."""
    return float(mesh.get(RATIO_PROP, 1.0))


def uv_ratio(mesh):
    """The map ratio (width / height) the mesh's UVs are drawn for.

    The recorded ratio, or 1 when the UVs have been unwrapped again since (see redrawn).
    """
    ratio = recorded_ratio(mesh)
    if abs(ratio - 1.0) < 1e-6 or redrawn(mesh):
        return 1.0
    return ratio


def ratio_matches(mesh, s):
    return abs(uv_ratio(mesh) - aspect(s)) < 1e-4


def uv_axis(mesh):
    return str(mesh.get(AXIS_PROP, 'BOTH'))


def _islands(mesh):
    """Return (loop island ids 0..n-1, loop UVs, island bbox min, bbox max).

    Reads the active UV map. In Edit Mode it reads a temporary copy of the mesh, since
    the UVs can't be read there directly; sync it first with
    Object.update_from_editmode().
    """
    if mesh.is_editmode:
        import bpy
        copy = mesh.copy()
        try:
            return _islands(copy)
        finally:
            bpy.data.meshes.remove(copy)
    import numpy as np

    from .edges import edge_analysis
    from .geometry import fetch
    from .uv_layout import uv_islands
    uv_split, _, _ = edge_analysis(mesh)
    islands = uv_islands(mesh, uv_split)
    totals = fetch(mesh.polygons, "loop_total", np.int64)
    _, islands = np.unique(np.asarray(islands), return_inverse=True)
    loop_island = np.repeat(islands.ravel(), totals)
    uv = fetch(mesh.uv_layers.active.data, "uv", np.float64, 2)
    n_islands = int(loop_island.max()) + 1 if len(loop_island) else 0
    lo = np.full((n_islands, 2), np.inf)
    hi = np.full((n_islands, 2), -np.inf)
    np.minimum.at(lo, loop_island, uv)
    np.maximum.at(hi, loop_island, uv)
    return loop_island, uv, lo, hi


def _shapes(lo, hi):
    """Sorted |log(width / height)| of the island boxes.

    Unchanged by packing (moves, uniform scaling, 90 degree turns) but changed by
    unwrapping again.
    """
    import numpy as np
    size = hi - lo
    ok = (size > 1e-9).all(axis=1)
    return np.sort(np.abs(np.log(size[ok, 0] / size[ok, 1])))


def redrawn(mesh, boxes=None):
    """True when the UVs were unwrapped again since Match UVs to Map Ratio stretched them.

    Detected by the island shapes no longer matching those recorded at the time.
    `boxes` is the islands' (bbox min, bbox max) when already known.
    """
    import numpy as np
    stored = mesh.get(SHAPE_PROP)
    if stored is None or RATIO_PROP not in mesh or not mesh.uv_layers or not mesh.polygons:
        return False
    stored = np.asarray(stored, dtype=np.float64)
    if not mesh.is_editmode and not mesh.uv_layers.active.data:
        return False
    lo, hi = boxes if boxes is not None else _islands(mesh)[2:]
    now = _shapes(lo, hi)
    if len(now) != len(stored):
        return True
    return bool(len(now)) and float(np.median(np.abs(now - stored))) > SHAPE_TOLERANCE


def _axis_scale(change, axis):
    """(U, V) factors that change an island's U:V proportion by `change`."""
    if axis == 'U':
        return change, 1.0
    if axis == 'V':
        return 1.0, 1.0 / change
    return math.sqrt(change), 1.0 / math.sqrt(change)


def match_uv_ratio(mesh, ratio, axis='BOTH', drawn_square=False):
    """Re-stretch every UV island from the ratio its UVs are drawn for to `ratio`.

    Each island is scaled about its own centre so texels stay square on the map.
    `axis` is a STRETCH_AXES key: 'BOTH' scales U and V by reciprocal square roots of
    the change, keeping UV area and texel density; 'U' or 'V' changes only that axis.
    The previous stretch is undone first, so matches don't accumulate. UVs unwrapped
    again since the last match (see redrawn), or with `drawn_square`, are treated as
    square.

    Works in Object Mode, or in Edit Mode without leaving it (the mesh data must be
    current: Object.update_from_editmode()). Islands may overlap or leave 0-1
    afterwards, so pack after. Returns False when there was nothing to change.
    """
    import numpy as np
    if not mesh.uv_layers or not mesh.polygons:
        return False
    loop_island, uv, lo, hi = _islands(mesh)
    if not len(uv):
        return False
    old, old_axis = recorded_ratio(mesh), uv_axis(mesh)
    if drawn_square or abs(old - 1.0) < 1e-6 or redrawn(mesh, (lo, hi)):
        old, old_axis = 1.0, axis  # square UVs: nothing to undo
    if abs(old - ratio) < 1e-6 and old_axis == axis:
        if drawn_square or SHAPE_PROP not in mesh:  # already drawn for this ratio: record it
            _remember(mesh, ratio, axis, _shapes(lo, hi))
        return False
    centre = ((lo + hi) / 2)[loop_island]
    # Undo the earlier stretch back to square, then stretch to `ratio`. U relative to V
    # shrinks as the map gets wider.
    undo, do = _axis_scale(old, old_axis), _axis_scale(1.0 / ratio, axis)
    scale = np.array([undo[0] * do[0], undo[1] * do[1]])
    _write_uvs(mesh, centre + (uv - centre) * scale)
    _remember(mesh, ratio, axis, _shapes(lo * scale, hi * scale))
    return True


def _write_uvs(mesh, uv):
    """Set the active UV map's loop UVs (mesh loop order), in Object or Edit Mode."""
    import numpy as np
    if not mesh.is_editmode:
        mesh.uv_layers.active.data.foreach_set("uv", uv.astype(np.float32).ravel())
        mesh.update()
        return
    import bmesh
    bm = bmesh.from_edit_mesh(mesh)
    layer = bm.loops.layers.uv.get(mesh.uv_layers.active.name) or bm.loops.layers.uv.active
    values = uv.tolist()
    i = 0
    for face in bm.faces:  # the edit mesh keeps the mesh's face and loop order
        for loop in face.loops:
            loop[layer].uv = values[i]
            i += 1
    bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)


def _remember(mesh, ratio, axis, shapes):
    """Record the ratio and axis the UVs are drawn for, and the island shapes."""
    if abs(ratio - 1.0) < 1e-6:
        for prop in (RATIO_PROP, AXIS_PROP, SHAPE_PROP):
            mesh.pop(prop, None)
    else:
        mesh[RATIO_PROP] = ratio
        mesh[AXIS_PROP] = axis
        mesh[SHAPE_PROP] = [float(v) for v in shapes]
