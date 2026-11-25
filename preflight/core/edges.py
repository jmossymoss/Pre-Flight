# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Edge analysis, and hard edges derived from UV islands or seams."""

import bpy
import numpy as np

from ..utils import is_auto_smooth_mod, object_mode, selected_meshes, unique_meshes
from .geometry import edge_loops, fetch

UV_SPLIT_EPSILON = 1e-5  # UV distance at which the two sides of an edge count as split

PROBLEM_EDGES = {
    'HARD_NO_SPLIT': ("Hard Edges Without UV Split",
                      "Hard edges must have a UV split, or the bake shows a visible seam"),
    'CORNER_NO_SPLIT': ("Sharp Corners Without UV Split",
                        "Corners sharper than the threshold with no UV split: the normal map "
                        "has to bend a lot here, so cut UVs and harden"),
}


# --- Analysis ---

def edges_from_seams(mesh):
    """Boolean mask of the edges marked as UV seams."""
    seams = np.zeros(len(mesh.edges), dtype=bool)
    mesh.edges.foreach_get("use_seam", seams)
    return seams


def loop_topology(mesh):
    """Return (loop_edge, loop_vert, loop_next, loop_face) index arrays for every loop."""
    n_loops, n_faces = len(mesh.loops), len(mesh.polygons)
    loop_edge = fetch(mesh.loops, "edge_index", np.int64)
    loop_vert = fetch(mesh.loops, "vertex_index", np.int64)
    start = fetch(mesh.polygons, "loop_start", np.int64)
    total = fetch(mesh.polygons, "loop_total", np.int64)
    loop_next = np.arange(1, n_loops + 1, dtype=np.int64)
    loop_next[start + total - 1] = start
    loop_face = np.repeat(np.arange(n_faces, dtype=np.int64), total)
    return loop_edge, loop_vert, loop_next, loop_face


def edge_analysis(mesh):
    """Per-edge arrays (uv_split, face_angle, manifold).

    uv_split   - the UVs differ on either side (a UV island border); also set for
                 non-manifold edges
    face_angle - angle between the two face normals in radians (0 = flat)
    manifold   - the edge has exactly two faces
    """
    n_edges = len(mesh.edges)
    uv_split = np.zeros(n_edges, dtype=bool)
    face_angle = np.zeros(n_edges, dtype=np.float32)
    manifold = np.zeros(n_edges, dtype=bool)
    if not n_edges or not len(mesh.loops):
        return uv_split, face_angle, manifold
    loop_edge, loop_vert, loop_next, loop_face = loop_topology(mesh)
    counts, edge, loop_a, loop_b = edge_loops(loop_edge, n_edges)
    uv_split[counts > 2] = True
    manifold[edge] = True

    normals = fetch(mesh.polygons, "normal", np.float64, 3)
    dots = np.einsum('ij,ij->i', normals[loop_face[loop_a]], normals[loop_face[loop_b]])
    face_angle[edge] = np.arccos(np.clip(dots, -1.0, 1.0))

    layer = mesh.uv_layers.active
    if layer is not None:
        uv = fetch(layer.data, "uv", np.float64, 2)
        # Loop a runs from vertex a0 to a1; loop b usually runs the other way.
        same = loop_vert[loop_a] == loop_vert[loop_b]
        b0 = np.where(same, loop_b, loop_next[loop_b])
        b1 = np.where(same, loop_next[loop_b], loop_b)
        d0 = np.linalg.norm(uv[loop_a] - uv[b0], axis=1)
        d1 = np.linalg.norm(uv[loop_next[loop_a]] - uv[b1], axis=1)
        uv_split[edge] = (d0 > UV_SPLIT_EPSILON) | (d1 > UV_SPLIT_EPSILON)
    return uv_split, face_angle, manifold


def mirror_plane_edges(obj, mesh):
    """Edges lying on the plane of an enabled Mirror modifier (the mirror seam)."""
    mask = np.zeros(len(mesh.edges), dtype=bool)
    mods = [m for m in obj.modifiers if m.type == 'MIRROR' and m.show_viewport]
    if not mods or not len(mesh.edges):
        return mask
    co = fetch(mesh.vertices, "co", np.float64, 3)
    edge_verts = fetch(mesh.edges, "vertices", np.int32, 2)
    for mod in mods:
        pts = co
        if mod.mirror_object:
            mat = np.array(mod.mirror_object.matrix_world.inverted() @ obj.matrix_world)
            pts = co @ mat[:3, :3].T + mat[:3, 3]
        tol = max(mod.merge_threshold, 1e-4)
        for axis in range(3):
            if mod.use_axis[axis]:
                on_plane = np.abs(pts[:, axis]) <= tol
                mask |= on_plane[edge_verts].all(axis=1)
    return mask


def problem_edge_mask(obj, kind, settings):
    """Edges of `obj` matching a PROBLEM_EDGES kind, ignoring mirror-plane edges."""
    mesh = obj.data
    uv_split, face_angle, manifold = edge_analysis(mesh)
    if kind == 'HARD_NO_SPLIT':
        sharp = np.zeros(len(mesh.edges), dtype=bool)
        mesh.edges.foreach_get("use_edge_sharp", sharp)
        mask = sharp & manifold & ~uv_split
    else:
        mask = manifold & ~uv_split & (face_angle > settings.corner_angle)
    return mask & ~mirror_plane_edges(obj, mesh)


# --- Hard edges ---

def clear_custom_normals(obj):
    """Clear the mesh's custom split normals. Returns True if it had any."""
    if not obj.data.has_custom_normals:
        return False
    with bpy.context.temp_override(object=obj, active_object=obj):
        bpy.ops.mesh.customdata_custom_splitnormals_clear()
    return True


def apply_hard_edges(obj, mesh, settings):
    """Smooth all faces, then mark sharp edges from the chosen UV source.

    Returns the number of sharp edges.
    """
    uv_split, face_angle, manifold = edge_analysis(mesh)
    if settings.edge_source == 'ISLANDS':
        sharp = uv_split.copy()
        if settings.sync_seams:
            mesh.edges.foreach_set("use_seam", uv_split)
    else:
        sharp = edges_from_seams(mesh)

    if settings.min_angle > 0:
        # Shallow seams (e.g. a cylinder's side seam) can stay soft.
        sharp &= (face_angle >= settings.min_angle) | ~manifold
    if settings.soft_mirror_edges:
        sharp &= ~mirror_plane_edges(obj, mesh)
    mesh.edges.foreach_set("use_edge_sharp", sharp)

    face_attr = mesh.attributes.get("sharp_face")  # no attribute means every face is smooth
    if face_attr:
        mesh.attributes.remove(face_attr)

    if settings.clear_custom_normals:
        clear_custom_normals(obj)
    mesh.update()
    return int(sharp.sum())


def strip_auto_smooth(obj):
    """Remove the object's auto-smooth modifiers. Returns how many were removed."""
    removed = 0
    for mod in [m for m in obj.modifiers if is_auto_smooth_mod(m)]:
        obj.modifiers.remove(mod)
        removed += 1
    return removed


def run_hard_edges(context):
    """Apply hard edges to the selected meshes and return a report message."""
    settings = context.scene.preflight
    objects = selected_meshes(context)
    with object_mode(context):
        sharp = sum(apply_hard_edges(o, m, settings) for o, m in unique_meshes(objects))
        stripped = sum(strip_auto_smooth(o) for o in objects) if settings.strip_auto_smooth else 0
    msg = f"{len(objects)} object(s): {sharp} sharp edge(s)"
    if stripped:
        msg += f", removed {stripped} auto-smooth modifier(s)"
    return msg
