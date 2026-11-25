# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""ID colours for highs: a flat colour per object, loose part or material.

Written as a colour attribute so it exports with the mesh (FBX vertex colours) and
works with any baker's Color ID / ID map bake.
"""

import numpy as np

from .geometry import fetch
from .uv_layout import connected_labels

ATTRIBUTE = "BakeID"

MODES = (
    ('OBJECT', "Per Object", "One colour per high (objects sharing a mesh share it)"),
    ('PART', "Per Loose Part", "One colour per connected piece of each high"),
    ('MATERIAL', "Per Material", "One colour per material: the same material gets the "
                                 "same colour on every high"),
)

NEIGHBOUR_GAP = 0.25  # sRGB distance (about 64/255) wanted between consecutive IDs

_palette_cache = {}  # lattice levels -> farthest-point sequence


# --- Palette ---

def _srgb_sequence(n):
    """n sRGB colours, each as far as possible from all those before it.

    Greedy farthest-point sampling on an RGB lattice, so any prefix is well spread.
    Repeats once the lattice runs out of distinct colours.
    """
    levels = int(np.clip(np.ceil((4 * n) ** (1 / 3)), 6, 32))
    if levels in _palette_cache and len(_palette_cache[levels]) >= n:
        return _palette_cache[levels][:n]
    axis = np.linspace(0.0, 1.0, levels)
    grid = np.stack(np.meshgrid(axis, axis, axis, indexing='ij'), -1).reshape(-1, 3)
    grid = grid[grid.max(axis=1) - grid.min(axis=1) > 0.15]  # skip greys: easily confused
    first = np.argmin(np.linalg.norm(grid - (0.9, 0.1, 0.1), axis=1))  # start at red
    picked = [first]
    dist = np.linalg.norm(grid - grid[first], axis=1)
    for _ in range(min(n, len(grid)) - 1):
        k = int(np.argmax(dist))
        picked.append(k)
        dist = np.minimum(dist, np.linalg.norm(grid - grid[k], axis=1))
    seq = grid[picked]
    if n > len(seq):
        seq = np.resize(seq, (n, 3))
    _palette_cache[levels] = seq
    return seq[:n]


def _spread(rgb, window=64):
    """Reorder so consecutive colours differ by at least NEIGHBOUR_GAP where possible.

    A colour too close to the previous one swaps with the next one within `window`
    that is far enough away.
    """
    rgb = rgb.copy()
    for i in range(1, len(rgb)):
        if np.linalg.norm(rgb[i] - rgb[i - 1]) >= NEIGHBOUR_GAP:
            continue
        ahead = rgb[i + 1:i + 1 + window]
        ok = np.nonzero(np.linalg.norm(ahead - rgb[i - 1], axis=1) >= NEIGHBOUR_GAP)[0]
        if len(ok):
            j = i + 1 + int(ok[0])
            rgb[[i, j]] = rgb[[j, i]]
    return rgb


def palette(n):
    """n distinct RGBA colours (linear), in a fixed shuffled order.

    Late picks in the farthest-point sequence tend to look alike, while neighbouring
    IDs (parts of one object, objects named in a row) should differ most, so the
    sequence is shuffled with a fixed seed and then spread.
    """
    if n <= 0:
        return np.zeros((0, 4), dtype=np.float32)
    rgb = _spread(_srgb_sequence(n)[np.random.default_rng(7).permutation(n)])
    out = np.ones((n, 4), dtype=np.float32)
    out[:, :3] = rgb
    return _to_linear(out)


def _to_linear(rgba):
    """Convert the colour channels from sRGB to linear in place.

    Colour attributes store linear values; the baker shows the sRGB colour picked here.
    """
    c = rgba[:, :3]
    rgba[:, :3] = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return rgba


# --- Colour attribute ---

def _layer(mesh, domain):
    """The ID colour attribute in `domain`, set as the active and render colour."""
    attr = mesh.color_attributes.get(ATTRIBUTE)
    if attr is not None and (attr.domain != domain or attr.data_type != 'BYTE_COLOR'):
        mesh.color_attributes.remove(attr)
        attr = None
    if attr is None:
        attr = mesh.color_attributes.new(ATTRIBUTE, 'BYTE_COLOR', domain)
    mesh.color_attributes.active_color = attr
    mesh.color_attributes.render_color_index = mesh.color_attributes.find(ATTRIBUTE)
    return attr


def fill(mesh, color):
    """Paint the whole mesh one colour."""
    attr = _layer(mesh, 'POINT')
    data = np.tile(np.asarray(color, dtype=np.float32), len(mesh.vertices))
    attr.data.foreach_set("color", data)
    mesh.update()


def part_labels(mesh):
    """Loose-part index (0..n-1) per vertex."""
    n = len(mesh.vertices)
    edges = fetch(mesh.edges, "vertices", np.int64, 2)
    labels = connected_labels(n, edges[:, 0], edges[:, 1]) if len(edges) else np.arange(n)
    _, inverse = np.unique(labels, return_inverse=True)
    return inverse.ravel()


def fill_parts(meshes):
    """A colour per loose part, unique across all `meshes`. Returns the number of parts."""
    labels = [part_labels(me) if len(me.vertices) else None for me in meshes]
    total = sum(int(p.max()) + 1 for p in labels if p is not None)
    colors = palette(total)
    start = 0
    for me, part in zip(meshes, labels):
        if part is None:
            continue
        attr = _layer(me, 'POINT')
        attr.data.foreach_set("color", colors[start + part].ravel())
        me.update()
        start += int(part.max()) + 1
    return total


def fill_materials(mesh, material_colors):
    """A colour per material slot, from {material name: colour} shared across meshes.

    Uses the corner domain so faces on either side of a material border keep their
    own colour.
    """
    if not len(mesh.polygons):
        return
    slot_colors = np.array([material_colors[m.name if m else ""] for m in mesh.materials]
                           or [material_colors[""]], dtype=np.float32)
    mat_index = fetch(mesh.polygons, "material_index", np.int32)
    mat_index = np.clip(mat_index, 0, len(slot_colors) - 1)
    totals = fetch(mesh.polygons, "loop_total", np.int64)
    corner_slot = np.repeat(mat_index, totals)
    attr = _layer(mesh, 'CORNER')
    attr.data.foreach_set("color", slot_colors[corner_slot].ravel())
    mesh.update()


def clear(mesh):
    """Remove the ID colour attribute. Returns True if there was one."""
    attr = mesh.color_attributes.get(ATTRIBUTE)
    if attr is None:
        return False
    mesh.color_attributes.remove(attr)
    mesh.update()
    return True
