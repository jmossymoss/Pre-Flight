# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Masks of problem vertices, edges and faces, for selecting them in Edit Mode."""

import bpy
import numpy as np

from . import geometry, mapsize, projection, texel, uv_stack
from .edges import PROBLEM_EDGES, edge_analysis, mirror_plane_edges, problem_edge_mask
from .uv_layout import triangle_uvs, uv_islands, zero_uv_faces

# kind -> (domain, label, description)
ELEMENT_KINDS = {
    **{k: ('EDGE', label, desc) for k, (label, desc) in PROBLEM_EDGES.items()},
    'MIRROR_HARD': ('EDGE', "Hard Edges on Mirror Line",
                    "Hard edges on a Mirror modifier's plane: keep these soft so the halves "
                    "shade as one surface"),
    'ZERO_UV': ('FACE', "Zero UV Area Faces",
                "Faces whose UVs have no area, so they get no texture pixels"),
    'OUTSIDE_UV': ('FACE', "Faces Outside 0-1",
                   "Faces whose UVs sit outside the 0-1 tile, so they are not baked"),
    'NGON': ('FACE', "N-gons", "Faces with more than four sides"),
    'NOT_TRIANGULATED': ('FACE', "Quads and N-gons",
                         "Faces that aren't triangles: the baker and the engine may split them "
                         "differently, so shading won't match the bake"),
    'SKEW': ('FACE', "Skew Risk Faces",
             "Faces whose shading normals bend strongly away from the face, which skews "
             "baked details and makes flat areas wavy"),
    'FLIPPED': ('EDGE', "Flipped Face Borders",
                "Edges between faces that point opposite ways"),
    'NON_MANIFOLD': ('EDGE', "Non-manifold Edges", "Edges shared by three or more faces"),
    'DOUBLES': ('VERT', "Doubled Vertices", "Vertices sitting on top of another vertex"),
    'ZERO_AREA': ('FACE', "Zero Area Faces", "Faces with no area"),
    'LOOSE_VERTS': ('VERT', "Loose Vertices", "Vertices not used by any edge"),
    'LOOSE_EDGES': ('EDGE', "Loose Edges", "Edges not used by any face"),
    'TEXEL': ('FACE', "Texel Density Outliers",
              "UV islands whose texel density is far off the texture set's"),
    'PROJ_MISS': ('FACE', "Missed Rays",
                  "Faces whose rays find no high poly: black or garbage pixels in the bake"),
    'PROJ_FAR': ('FACE', "Far From High",
                 "Faces far from the high poly: skewed details and waviness"),
    'STACKED': ('FACE', "Stacked Islands",
                "UV islands stacked on top of each other (mirrored or duplicated parts)"),
    'STACKED_COPIES': ('FACE', "Stacked Copies",
                       "The stacked copies Offset for Bake would move (one per stack stays)"),
    'OVERLAP': ('FACE', "Overlapping Islands",
                "UV islands that overlap others in the texture set (they'd bake into the "
                "same pixels)"),
    'TOO_CLOSE': ('FACE', "Too-Close Islands",
                  "UV islands closer to another than the padding: colours bleed between "
                  "them at lower mips"),
}

# Kinds that need data from an issue row, so they can't be selected on their own.
ISSUE_ONLY = {'TEXEL', 'PROJ_MISS', 'PROJ_FAR', 'STACKED', 'STACKED_COPIES', 'OVERLAP',
              'TOO_CLOSE'}

_CLEANUP_KINDS = ('FLIPPED', 'NON_MANIFOLD', 'DOUBLES', 'ZERO_AREA', 'LOOSE_VERTS',
                  'LOOSE_EDGES')


def _uv_face_masks(mesh):
    """(zero_area, outside) per face for the active UV map."""
    zero = np.zeros(len(mesh.polygons), dtype=bool)
    outside = np.zeros(len(mesh.polygons), dtype=bool)
    if not mesh.uv_layers:
        return zero, outside
    tri_uv, tri_face = triangle_uvs(mesh)
    if len(tri_uv):
        zero = zero_uv_faces(tri_uv, tri_face, len(mesh.polygons))
        centres = tri_uv.mean(axis=1)
        outside[tri_face[((centres < 0) | (centres > 1)).any(axis=1)]] = True
    return zero, outside


def _texel_mask(obj, settings, data):
    """Faces of islands off the texel density `data` (or the object's own median)."""
    mesh = obj.data
    if not mesh.uv_layers:
        return np.zeros(len(mesh.polygons), bool)
    uv_split, _, _ = edge_analysis(mesh)
    islands = uv_islands(mesh, uv_split)
    tri_uv, tri_face = triangle_uvs(mesh)
    scale = settings.id_data.unit_settings.scale_length or 1.0
    size = mapsize.texel_size(mapsize.of_object(settings, obj))
    ids, density, world = texel.island_densities(obj, mesh, tri_uv, tri_face, islands,
                                                 size, scale)
    if not len(ids):
        return np.zeros(len(mesh.polygons), bool)
    median = float(data) if data else texel.weighted_median(density, world)
    off_ids, _ = texel.outlier_islands(ids, density, median, settings.texel_tolerance / 100)
    return texel.face_mask(mesh, islands, off_ids)


def _texture_set_objects(obj, data):
    """The objects named in `data` (newline-separated), always including `obj`."""
    names = [n for n in data.split("\n") if n] if data else [obj.name]
    objects = [o for o in (bpy.data.objects.get(n) for n in names) if o] or [obj]
    if obj not in objects:
        objects.append(obj)
    return objects


def problem_mask(obj, kind, settings, data=""):
    """Boolean mask over the mesh's verts, edges or faces (domain per ELEMENT_KINDS)."""
    mesh = obj.data
    if kind in PROBLEM_EDGES:
        return problem_edge_mask(obj, kind, settings)
    if kind == 'MIRROR_HARD':
        sharp = np.zeros(len(mesh.edges), dtype=bool)
        mesh.edges.foreach_get("use_edge_sharp", sharp)
        return sharp & mirror_plane_edges(obj, mesh)
    if kind == 'NGON':
        loop_totals = np.zeros(len(mesh.polygons), dtype=np.int32)
        mesh.polygons.foreach_get("loop_total", loop_totals)
        return loop_totals > 4
    if kind == 'NOT_TRIANGULATED':
        return geometry.untriangulated_mask(mesh)
    if kind == 'SKEW':
        return geometry.skew_mask(mesh, settings.skew_angle)
    if kind in _CLEANUP_KINDS:
        return geometry.cleanup_masks(mesh)[kind][1]
    if kind == 'TEXEL':
        return _texel_mask(obj, settings, data)
    if kind in ('PROJ_MISS', 'PROJ_FAR'):
        miss, far = projection.face_masks(obj, data, settings,
                                          bpy.context.evaluated_depsgraph_get(),
                                          bpy.context.scene.objects)
        return miss if kind == 'PROJ_MISS' else far
    if kind in ('STACKED', 'STACKED_COPIES'):
        stacks = uv_stack.find_stacks(_texture_set_objects(obj, data), settings)
        return uv_stack.stack_face_mask(obj, stacks, copies_only=kind == 'STACKED_COPIES')
    if kind in ('OVERLAP', 'TOO_CLOSE'):
        objects = _texture_set_objects(obj, data)
        found = uv_stack.find_layout_islands(objects, settings).get(obj)
        if found is None:
            return np.zeros(len(mesh.polygons), dtype=bool)
        face_island, overlapping, close = found
        return np.isin(face_island, overlapping if kind == 'OVERLAP' else close)
    zero, outside = _uv_face_masks(mesh)
    return zero if kind == 'ZERO_UV' else outside
