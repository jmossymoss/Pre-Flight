# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Stacked UV islands: mirrored or duplicated parts sharing UV space.

Bakers write both copies into the same pixels, so all but one copy are moved +1 tile in
U for the bake. The offset is stored per corner in a float attribute, so restore() puts
back exactly those UVs even after other edits.
"""

import numpy as np

from . import mapsize
from .edges import edge_analysis
from .geometry import fetch
from .uv_layout import check_uv_layout, triangle_uvs, uv_islands

OFFSET_ATTR = "preflight_u_offset"  # FLOAT on corners (a FLOAT2 would become a UV map)


# --- Finding stacks ---

def island_flipped(tri_uv, tri_face, face_island):
    """Map island id -> True when its UVs are mirrored (negative winding overall)."""
    d = tri_uv[:, 1:] - tri_uv[:, :1]
    signed = d[:, 0, 0] * d[:, 1, 1] - d[:, 0, 1] * d[:, 1, 0]
    tri_island = face_island[tri_face]
    ids, inverse = np.unique(tri_island, return_inverse=True)
    total = np.bincount(inverse, weights=signed, minlength=len(ids))
    return {int(i): bool(t < 0) for i, t in zip(ids, total)}


def choose_moves(stacked_pairs, flipped):
    """Pick the islands to move from stacked ((label, island), (label, island)) pairs.

    Each group of stacked islands keeps one in place - preferring an unmirrored one,
    then the first by name - and moves the rest. flipped maps (label, island) -> bool.
    """
    parent = {}

    def find(k):
        parent.setdefault(k, k)
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for a, b in stacked_pairs:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_b] = root_a
    groups = {}
    for k in list(parent):
        groups.setdefault(find(k), []).append(k)
    moves = set()
    for members in groups.values():
        keep = min(members, key=lambda k: (flipped.get(k, False), k))
        moves.update(m for m in members if m != keep)
    return moves


def _layouts(objects, settings):
    """Gather per-texture-set layout data for the meshes in `objects` that have UVs.

    Returns (by_name, sets, set_size, flips, face_islands): `sets` maps a texture set to
    the entries check_uv_layout takes, `set_size` to its map size settings.
    """
    by_name, sets, flips, face_islands = {}, {}, {}, {}
    set_size = {}
    for obj in objects:
        if obj.type != 'MESH' or not obj.data.uv_layers:
            continue
        me = obj.data
        uv_split, _, _ = edge_analysis(me)
        islands = uv_islands(me, uv_split)
        tri_uv, tri_face = triangle_uvs(me)
        if not len(tri_uv):
            continue
        by_name[obj.name] = obj
        face_islands[obj] = islands
        tex_set = obj.active_material.name if obj.active_material else obj.name
        sets.setdefault(tex_set, []).append((obj.name, tri_uv, islands[tri_face]))
        set_size.setdefault(tex_set, mapsize.of_object(settings, obj))
        for isl, mirrored in island_flipped(tri_uv, tri_face, islands).items():
            flips[(obj.name, isl)] = mirrored
    return by_name, sets, set_size, flips, face_islands


def find_layout_islands(objects, settings):
    """Overlapping and too-close islands among `objects`, per texture set.

    Returns {obj: (face_island, overlapping ids, too-close ids)}.
    """
    by_name, sets, set_size, _, face_islands = _layouts(objects, settings)
    found = {}
    for tex_set, entries in sets.items():
        size = set_size[tex_set]
        overlaps, close, _ = check_uv_layout(entries, mapsize.padding_size(size),
                                             mapsize.padding(size), details=True, keyed=True)
        for slot, pairs in ((0, overlaps), (1, close)):
            for name, isl in {k for pair in pairs for k in pair}:
                found.setdefault(by_name[name], (set(), set()))[slot].add(isl)
    return {obj: (face_islands[obj], sorted(o), sorted(c)) for obj, (o, c) in found.items()}


def find_stacks(objects, settings):
    """Stacked islands among `objects`, grouped per texture set (active material).

    Returns {obj: (face_island, member island ids, copy island ids)}. Members are every
    island in a stack; copies are the ones Offset for Bake moves (one per stack stays).
    Only islands in the 0-1 tile count.
    """
    by_name, sets, set_size, flips, face_islands = _layouts(objects, settings)
    members, copies = {}, {}
    for tex_set, entries in sets.items():
        size = set_size[tex_set]
        _, _, stacked = check_uv_layout(entries, mapsize.padding_size(size),
                                        mapsize.padding(size), details=True)
        for name, isl in {k for pair in stacked for k in pair}:
            members.setdefault(by_name[name], set()).add(isl)
        for name, isl in choose_moves(stacked, flips):
            copies.setdefault(by_name[name], set()).add(isl)
    return {obj: (face_islands[obj], sorted(isls), sorted(copies.get(obj, ())))
            for obj, isls in members.items()}


def stack_face_mask(obj, stacks, copies_only=False):
    """Boolean face mask of obj's stacked islands (or only the copies)."""
    n_faces = len(obj.data.polygons)
    if obj not in stacks:
        return np.zeros(n_faces, dtype=bool)
    face_island, members, copies = stacks[obj]
    return np.isin(face_island, copies if copies_only else members)


# --- Offsetting ---

def offset_islands(mesh, face_island, island_ids, du=1.0):
    """Move the given islands by `du` in U and record the offset. Returns faces moved."""
    faces = np.nonzero(np.isin(face_island, list(island_ids)))[0]
    if not len(faces):
        return 0
    totals = fetch(mesh.polygons, "loop_total", np.int64)
    loop_face = np.repeat(np.arange(len(mesh.polygons)), totals)
    loops = np.nonzero(np.isin(loop_face, faces))[0]

    uv_layer = mesh.uv_layers.active
    uv = fetch(uv_layer.data, "uv", np.float64, 2)
    uv[loops, 0] += du
    uv_layer.data.foreach_set("uv", uv.astype(np.float32).ravel())

    attr = mesh.attributes.get(OFFSET_ATTR) or mesh.attributes.new(OFFSET_ATTR, 'FLOAT', 'CORNER')
    off = fetch(attr.data, "value", np.float64)
    off[loops] += du
    attr.data.foreach_set("value", off.astype(np.float32))
    mesh.update()
    return len(faces)


def has_offset(mesh):
    """True if the mesh has offsets recorded by offset_islands."""
    return mesh.attributes.get(OFFSET_ATTR) is not None


def restore(mesh):
    """Undo every offset made by offset_islands. Returns True if anything was restored."""
    attr = mesh.attributes.get(OFFSET_ATTR)
    if attr is None:
        return False
    off = fetch(attr.data, "value", np.float64)
    if mesh.uv_layers.active:
        uv_layer = mesh.uv_layers.active
        uv = fetch(uv_layer.data, "uv", np.float64, 2)
        uv[:, 0] -= off
        uv_layer.data.foreach_set("uv", uv.astype(np.float32).ravel())
    mesh.attributes.remove(attr)
    mesh.update()
    return True
