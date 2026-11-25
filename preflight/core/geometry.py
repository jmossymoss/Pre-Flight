# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Geometry checks: mesh cleanup, shading skew and triangulation.

Everything is numpy-only so the checks stay fast on dense high-poly meshes. All masks
index the original (unevaluated) mesh.
"""

from contextlib import contextmanager

import numpy as np

DOUBLES_DISTANCE = 1e-5  # local units; Blender's Merge by Distance default is 1e-4
INWARD_RATIO = -0.02  # outward highs measure 0.03..0.9; a flipped copy scores the negative

# foreach_get is only fast when the buffer matches Blender's own storage type.
_NATIVE = {'i': np.int32, 'u': np.int32, 'f': np.float32, 'b': bool}

# Element properties that Blender 4+ also stores as attributes. Reading the attribute
# is several times faster than going through the elements.
_ATTRIBUTES = {
    ("MeshLoops", "edge_index"): (".corner_edge", "value"),
    ("MeshLoops", "vertex_index"): (".corner_vert", "value"),
    ("MeshEdges", "vertices"): (".edge_verts", "value"),
    ("MeshVertices", "co"): ("position", "vector"),
}

# Collections whose reads may be cached inside shared_reads().
_SHAREABLE = {"MeshLoops", "MeshPolygons", "MeshEdges", "MeshVertices", "MeshLoopTriangles"}
_memo = None  # (mesh pointer, collection, attr, width, dtype) -> array, inside shared_reads


# --- Mesh array reads ---

@contextmanager
def shared_reads():
    """Cache mesh array reads for the duration of the block.

    Several checks read the same arrays; inside this block each is read from Blender
    once. Callers still get their own copy, so in-place edits don't leak between
    checks. Only use it while the meshes are not being modified.
    """
    global _memo
    outer = _memo
    _memo = {} if outer is None else outer
    try:
        yield
    finally:
        _memo = outer


def fetch(coll, attr, dtype=np.float32, width=1):
    """Return coll.foreach_get(attr) as a numpy array of `dtype`, shaped (n, width).

    Reads into Blender's native type and converts afterwards: any other buffer type
    makes foreach_get fall back to a slow per-item copy.
    """
    native = _NATIVE[np.dtype(dtype).kind]
    rna = getattr(coll, "bl_rna", None)
    identifier = rna.identifier if rna else ""
    key = None
    if _memo is not None and identifier in _SHAREABLE:
        key = (coll.id_data.as_pointer(), identifier, attr, width, native)
        cached = _memo.get(key)
        if cached is not None:
            arr = cached.astype(dtype, copy=True)
            return arr.reshape(-1, width) if width > 1 else arr
    arr = np.empty(len(coll) * width, dtype=native)
    if len(arr):
        alias = _ATTRIBUTES.get((identifier, attr))
        layer = coll.id_data.attributes.get(alias[0]) if alias else None
        if layer is not None and len(layer.data) * width == len(arr):
            layer.data.foreach_get(alias[1], arr)
        else:
            coll.foreach_get(attr, arr)
    if key is not None:
        _memo[key] = arr
        arr = arr.astype(dtype, copy=True)
    else:
        arr = arr.astype(dtype, copy=False)
    return arr.reshape(-1, width) if width > 1 else arr


_get = fetch  # short alias used by other modules


# --- Topology ---

def edge_loops(loop_edge, n_edges):
    """Return (counts, edges, loop_a, loop_b) without sorting.

    counts is the number of loops on every edge; edges lists the edges with exactly two
    loops, and loop_a / loop_b are those two loops.
    """
    counts = np.bincount(loop_edge, minlength=n_edges)
    idx = np.arange(len(loop_edge), dtype=np.int64)
    first = np.zeros(n_edges, dtype=np.int64)
    last = np.zeros(n_edges, dtype=np.int64)
    first[loop_edge[::-1]] = idx[::-1]  # with repeated indices the last write wins
    last[loop_edge] = idx
    edges = np.nonzero(counts == 2)[0]
    return counts, edges, first[edges], last[edges]


def loop_faces(mesh):
    """Face index of every loop."""
    totals = fetch(mesh.polygons, "loop_total", np.int32)
    return np.repeat(np.arange(len(mesh.polygons), dtype=np.int32), totals)


def edge_face_counts(mesh, loop_edge=None):
    """Number of faces using each edge."""
    if loop_edge is None:
        loop_edge = fetch(mesh.loops, "edge_index", np.int64)
    return np.bincount(loop_edge, minlength=len(mesh.edges))


def inconsistent_edges(mesh, loop_edge=None):
    """Edges whose two faces wind the same way along them, i.e. one face is flipped."""
    n_loops = len(mesh.loops)
    mask = np.zeros(len(mesh.edges), dtype=bool)
    if not n_loops:
        return mask
    loop_vert = fetch(mesh.loops, "vertex_index", np.int64)
    if loop_edge is None:
        loop_edge = fetch(mesh.loops, "edge_index", np.int64)
    # A loop runs from its vertex along its edge. Consistently wound faces traverse a
    # shared edge in opposite directions, so their loops start at different ends.
    # Edges with 3+ faces are non-manifold rather than flipped, so only 2-face edges count.
    _, edges, loop_a, loop_b = edge_loops(loop_edge, len(mesh.edges))
    mask[edges[loop_vert[loop_a] == loop_vert[loop_b]]] = True
    return mask


# --- Cleanup checks ---

def is_inside_out(mesh, face_counts=None, flipped=None):
    """True for a closed, consistently wound mesh whose faces all point inwards."""
    face_counts = edge_face_counts(mesh) if face_counts is None else face_counts
    if not len(face_counts) or (face_counts != 2).any():
        return False  # open or non-manifold: the signed volume is meaningless
    if flipped is not None and flipped.any():
        return False
    mesh.calc_loop_triangles()
    co = fetch(mesh.vertices, "co", np.float64, 3)
    tris = fetch(mesh.loop_triangles, "vertices", np.int64, 3)
    if not len(tris):
        return False
    v0, v1, v2 = co[tris[:, 0]], co[tris[:, 1]], co[tris[:, 2]]
    return float(np.einsum("ij,ij->i", v0, np.cross(v1, v2)).sum()) < 0


def normals_point_inwards(mesh):
    """True when the vertex normals mostly point towards the mesh's centroid.

    A cheap test for high-poly meshes: it reads only vertex positions and normals,
    avoiding the much slower face and corner arrays. Each vertex votes with how far its
    normal points away from the centroid; outward meshes, open or closed, score
    positive and an inverted copy the exact negative.
    """
    if not len(mesh.vertices) or not len(mesh.polygons):
        return False
    co = fetch(mesh.vertices, "co", np.float64, 3)
    co -= co.mean(axis=0)
    normals = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertex_normals.foreach_get("vector", normals)
    votes = np.einsum("ij,ij->i", co, normals.reshape(-1, 3))
    total = float(np.abs(votes).sum())
    return total > 0 and float(votes.sum()) < INWARD_RATIO * total


def double_verts(mesh, distance=DOUBLES_DISTANCE):
    """Vertices sharing a position with another vertex (within `distance`)."""
    co = fetch(mesh.vertices, "co", np.float64, 3)
    mask = np.zeros(len(co), dtype=bool)
    if len(co) < 2:
        return mask
    cells = np.round(co / distance).astype(np.int64)
    cells -= cells.min(axis=0)
    span = cells.max(axis=0) + 1
    if float(span[0]) * float(span[1]) * float(span[2]) < 2.0 ** 62:
        # One int64 key per grid cell: a 1-D sort is far faster than unique(axis=0).
        key = cells[:, 0] + span[0] * (cells[:, 1] + span[1] * cells[:, 2])
        order = np.argsort(key)
        same = key[order[1:]] == key[order[:-1]]
        mask[order[1:][same]] = True
        mask[order[:-1][same]] = True
        return mask
    _, inverse, counts = np.unique(cells, axis=0, return_inverse=True, return_counts=True)
    mask[counts[inverse.ravel()] > 1] = True
    return mask


def loose_verts(mesh, loose_edges):
    """Vertices with no edges.

    Only vertices outside every face can be loose, minus those on a loose edge; the
    edge list is read only when `loose_edges` has any set.
    """
    used = np.zeros(len(mesh.vertices), dtype=bool)
    used[fetch(mesh.loops, "vertex_index", np.int32)] = True
    if loose_edges.any():
        edge_verts = fetch(mesh.edges, "vertices", np.int32, 2)
        used[edge_verts[loose_edges].ravel()] = True
    return ~used


def cleanup_masks(mesh):
    """Masks of problem elements as {kind: (domain, mask)}, plus 'INSIDE_OUT': bool."""
    loop_edge = fetch(mesh.loops, "edge_index", np.int64)
    face_counts = edge_face_counts(mesh, loop_edge)
    flipped = inconsistent_edges(mesh, loop_edge)
    loose_edges = face_counts == 0
    return {
        'FLIPPED': ('EDGE', flipped),
        'NON_MANIFOLD': ('EDGE', face_counts > 2),
        'LOOSE_EDGES': ('EDGE', loose_edges),
        'LOOSE_VERTS': ('VERT', loose_verts(mesh, loose_edges)),
        'DOUBLES': ('VERT', double_verts(mesh)),
        # Blender's own face area is faster than computing it in numpy.
        'ZERO_AREA': ('FACE', fetch(mesh.polygons, "area", np.float32) < 1e-12),
        'INSIDE_OUT': is_inside_out(mesh, face_counts, flipped),
    }


# --- Shading and triangulation ---

def skew_mask(mesh, max_angle):
    """Faces where a corner's shading normal bends more than `max_angle` from the face.

    Bent corner normals cast the baker's rays at an angle, skewing details and making
    flat areas wavy. Hard edges don't count, since corner normals already follow them.
    """
    n_faces = len(mesh.polygons)
    if not n_faces or not hasattr(mesh, "corner_normals"):
        return np.zeros(n_faces, dtype=bool)
    corner = fetch(mesh.corner_normals, "vector", np.float64, 3)
    face_normals = fetch(mesh.polygons, "normal", np.float64, 3)
    face = loop_faces(mesh)
    cos = np.clip(np.einsum("ij,ij->i", corner, face_normals[face]), -1, 1)
    worst = np.full(n_faces, 1.0)
    np.minimum.at(worst, face, cos)
    return worst < np.cos(max_angle)


def untriangulated_mask(mesh):
    """Faces with more than three corners."""
    return fetch(mesh.polygons, "loop_total", np.int32) > 3
