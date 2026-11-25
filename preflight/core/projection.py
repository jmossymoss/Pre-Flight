# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Low/high projection: how far the high sits from the low along the low's normals.

Rays start on the low (with its modifiers, as it will bake) and travel outwards
(front) and inwards (rear) until they hit the high, as a baker's do. This only
measures; it builds no cage.
"""

import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree

from .geometry import _get, fetch, loop_faces
from .naming import pair_groups, pair_role

FACE_ID = "preflight_face_id"  # temporary attribute: original face index + 1


# --- Pairs and bounds ---

def split_pair(objs):
    """(lows, highs) among `objs`, by name."""
    lows = [o for o in objs if pair_role(o.name) == "low"]
    highs = [o for o in objs if pair_role(o.name) == "high"]
    return lows, highs


def partners(base, objects):
    """(lows, highs) named <base>_low / <base>_high[_n] among `objects`."""
    objs = pair_groups([o for o in objects if o.type == 'MESH']).get(base, [])
    return split_pair(objs)


def world_bounds_diag(objs):
    """Diagonal of the world-space bounding box around `objs` (0 when empty)."""
    pts = np.array([o.matrix_world @ Vector(c) for o in objs for c in o.bound_box])
    return float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0))) if len(pts) else 0.0


def limits(low, highs, settings):
    """(tolerance, search): the gap above which a high counts as far, and the ray length.

    Rays search across the whole pair, so a large gap reports as far rather than
    as a miss; only rays that leave the pair entirely miss.
    """
    tolerance = world_bounds_diag([low]) * settings.projection_tolerance / 100
    return tolerance, max(world_bounds_diag([low, *highs]), tolerance * 4)


# --- Ray casting ---

def high_bvh(highs, depsgraph):
    """One world-space BVH over the evaluated highs, or None if they have no faces."""
    all_co, all_tris, offset = [], [], 0
    for hi in highs:
        ev = hi.evaluated_get(depsgraph)
        me = ev.to_mesh()
        try:
            me.calc_loop_triangles()
            co = _get(me.vertices, "co", np.float64, 3)
            tris = _get(me.loop_triangles, "vertices", np.int64, 3)
            mat = np.array(ev.matrix_world)
            all_co.append(co @ mat[:3, :3].T + mat[:3, 3])
            all_tris.append(tris + offset)
            offset += len(co)
        finally:
            ev.to_mesh_clear()
    if not offset or not sum(len(t) for t in all_tris):
        return None
    return BVHTree.FromPolygons(np.concatenate(all_co).tolist(),
                                np.concatenate(all_tris).tolist(), all_triangles=True)


def _cast(mesh, matrix_world, bvh, tolerance, search, average):
    """Cast from every corner and face centre of `mesh`, both ways along its normals.

    Returns per-face 'miss' / 'far' masks (a face counts when any of its samples
    does), 'front' / 'rear' (largest hit distances) and 'hits'.
    """
    n_faces = len(mesh.polygons)
    out = {"miss": np.zeros(n_faces, bool), "far": np.zeros(n_faces, bool),
           "front": 0.0, "rear": 0.0, "hits": 0}
    if not len(mesh.loops) or not hasattr(mesh, "corner_normals"):
        return out
    mat = np.array(matrix_world)
    co = _get(mesh.vertices, "co", np.float64, 3) @ mat[:3, :3].T + mat[:3, 3]
    loop_vert = _get(mesh.loops, "vertex_index", np.int64)
    if average:
        normals = _get(mesh.vertices, "normal", np.float64, 3)[loop_vert]
    else:
        normals = _get(mesh.corner_normals, "vector", np.float64, 3)
    normals = normals @ np.linalg.inv(mat[:3, :3])  # inverse-transpose, row vectors
    normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-12)

    # Corners sharing a vertex and normal cast the same rays: cast once per key.
    key = np.concatenate([loop_vert[:, None], np.round(normals * 1e4).astype(np.int64)], axis=1)
    _, first, inverse = np.unique(key, axis=0, return_index=True, return_inverse=True)
    inverse = inverse.ravel()
    face = loop_faces(mesh)

    # Face centres too, along the averaged corner normal: large faces bake mostly
    # from their interior.
    centres = _get(mesh.polygons, "center", np.float64, 3) @ mat[:3, :3].T + mat[:3, 3]
    centre_n = np.zeros((n_faces, 3))
    np.add.at(centre_n, face, normals)
    centre_n /= np.maximum(np.linalg.norm(centre_n, axis=1, keepdims=True), 1e-12)

    points = np.concatenate([co[loop_vert[first]], centres])
    dirs = np.concatenate([normals[first], centre_n])
    miss = np.zeros(len(points), bool)
    far = np.zeros(len(points), bool)
    front_max = rear_max = 0.0
    hits = 0
    for k in range(len(points)):
        p, n = Vector(points[k]), Vector(dirs[k])
        hit_f = bvh.ray_cast(p, n, search)[3]
        hit_r = bvh.ray_cast(p, -n, search)[3]
        if hit_f is None and hit_r is None:
            miss[k] = True
            continue
        hits += 1
        if hit_r is None or (hit_f is not None and hit_f <= hit_r):
            front_max = max(front_max, hit_f)
        else:
            rear_max = max(rear_max, hit_r)
        # Far is the gap between the surfaces, not the ray length: a diagonal ray
        # from a hard corner is long even where the high sits evenly over the low.
        far[k] = bvh.find_nearest(p)[3] > tolerance

    n_corner = len(first)
    np.logical_or.at(out["miss"], face, miss[:n_corner][inverse])
    np.logical_or.at(out["far"], face, far[:n_corner][inverse])
    out["miss"] |= miss[n_corner:]
    out["far"] |= far[n_corner:]
    out["far"] &= ~out["miss"]
    out["front"], out["rear"], out["hits"] = front_max, rear_max, hits
    return out


def analyse_low(low, bvh, tolerance, search, average=True, depsgraph=None):
    """Cast from the low as it will bake (with modifiers), reported on its own faces.

    A temporary face attribute (FACE_ID) carries each face's index through the
    modifier stack, so mirrored or arrayed copies report on their source face.
    Faces a modifier creates (e.g. boolean walls) can't be selected on the original
    mesh and are counted in 'extra_miss' / 'extra_far' instead.

    average: cast along averaged vertex normals, like a baker's default cage;
        False casts along the split shading normals.
    tolerance: world-space gap above which the high counts as far.
    search: ray length before a ray counts as a miss.
    """
    mesh = low.data
    n = len(mesh.polygons)
    if depsgraph is None or not any(m.show_viewport for m in low.modifiers):
        res = _cast(mesh, low.matrix_world, bvh, tolerance, search, average)
        res["extra_miss"] = res["extra_far"] = 0
        return res

    attr = mesh.attributes.get(FACE_ID) or mesh.attributes.new(FACE_ID, 'INT', 'FACE')
    attr.data.foreach_set("value", np.arange(1, n + 1, dtype=np.int32))
    mesh.update()
    depsgraph.update()
    try:
        ev = low.evaluated_get(depsgraph)
        em = ev.to_mesh()
        try:
            res = _cast(em, ev.matrix_world, bvh, tolerance, search, average)
            ids = em.attributes.get(FACE_ID)
            if ids is not None and ids.domain == 'FACE':
                orig = fetch(ids.data, "value", np.int64)
                orig -= 1
            elif len(em.polygons) == n:
                orig = np.arange(n)
            else:
                orig = np.full(len(em.polygons), -1)
        finally:
            ev.to_mesh_clear()
    finally:
        mesh.attributes.remove(mesh.attributes[FACE_ID])
        mesh.update()

    mapped = (orig >= 0) & (orig < n)
    out = dict(res, miss=np.zeros(n, bool), far=np.zeros(n, bool))
    out["miss"][orig[mapped & res["miss"]]] = True
    out["far"][orig[mapped & res["far"]]] = True
    out["far"] &= ~out["miss"]
    out["extra_miss"] = int((res["miss"] & ~mapped).sum())
    out["extra_far"] = int((res["far"] & ~mapped).sum())
    return out


# --- Entry points ---

def analyse_pairs(objects, settings, depsgraph):
    """Run analyse_low for every <base>_low that has highs among `objects`.

    Yields (base, low, highs, result, tolerance, search).
    """
    for base, objs in sorted(pair_groups([o for o in objects if o.type == 'MESH']).items()):
        lows, highs = split_pair(objs)
        if not lows or not highs:
            continue
        bvh = high_bvh(highs, depsgraph)
        if bvh is None:
            continue
        for low in lows:
            tolerance, search = limits(low, highs, settings)
            res = analyse_low(low, bvh, tolerance, search, settings.projection_average,
                              depsgraph)
            yield base, low, highs, res, tolerance, search


def face_masks(low, base, settings, depsgraph, objects):
    """Per-face (miss, far) masks for one low against its partner highs."""
    _, highs = partners(base, objects)
    n = len(low.data.polygons)
    bvh = high_bvh(highs, depsgraph) if highs else None
    if bvh is None:
        return np.zeros(n, bool), np.zeros(n, bool)
    tolerance, search = limits(low, highs, settings)
    res = analyse_low(low, bvh, tolerance, search, settings.projection_average, depsgraph)
    return res["miss"], res["far"]
