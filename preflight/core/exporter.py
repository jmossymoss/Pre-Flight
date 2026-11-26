# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Writing files: the low and high for the baker, and the engine mesh and rig for the
game engine. Also the cage meshes and ray distances that come from the Projection check,
and collision meshes for the engine copies.
"""

import os
import re

import bmesh
import bpy
import numpy as np
from mathutils import Vector

from . import export, projection, rigging
from .naming import HIGH_TOKEN_RE, LOW_TOKEN_RE

CAGE_PROP = "preflight_cage"            # on a cage mesh: the low it was made from
COLLISION_PROP = "preflight_collision"  # on a collision mesh: the engine mesh it wraps

# How each baker takes its meshes: 'SPLIT' wants the low and the high as two files,
# 'SINGLE' reads both from one, None bakes inside Blender.
BAKER_FILES = {
    'PAINTER': 'SPLIT', 'DESIGNER': 'SPLIT', 'XNORMAL': 'SPLIT',
    'MARMOSET': 'SINGLE', 'FYR': None, 'BLENDER': None,
}
FILES_ITEMS = [
    ('AUTO', "As the Baker Wants", "Two files for Painter, Designer and xNormal; one for "
                                   "Marmoset Toolbag"),
    ('SPLIT', "Low and High Files", "<name>_low.fbx and <name>_high.fbx"),
    ('SINGLE', "One File", "<name>_bake.fbx holding both"),
]
ENGINE_ITEMS = [
    ('UNREAL', "Unreal Engine", "FBX with Unreal's scale, no leaf bones, UCX_ collision names"),
    ('UNITY', "Unity", "FBX with scale applied, no leaf bones, _collider collision names"),
    ('GODOT', "Godot (glTF)", "A .glb file, -colonly collision names"),
]
COLLISION_ITEMS = [
    ('NONE', "None", "No collision meshes"),
    ('BOX', "Boxes", "A box round each engine mesh"),
    ('HULL', "Convex Hulls", "A convex hull round each engine mesh"),
]
NORMAL_ITEMS = [
    ('OPENGL', "OpenGL (Y+)", "Blender, Unity, Godot: maps load as they are"),
    ('DIRECTX', "DirectX (Y-)", "Unreal: Load Textures flips the green channel so the "
                                "maps look right in Blender"),
]


def asset_name(context, name=""):
    """File name stem for exports: the name given, else the .blend's, else the scene's."""
    name = name.strip()
    if not name and bpy.data.filepath:
        name = os.path.splitext(os.path.basename(bpy.data.filepath))[0]
    name = name or context.scene.name
    return re.sub(r'[<>:"/\\|?*]+', "_", name).strip() or "asset"


def export_folder(context, folder=""):
    """Absolute export folder: the one given, else an "export" folder beside the .blend.
    Raises ValueError when there is nowhere to write."""
    if folder.strip():
        path = bpy.path.abspath(folder)
    elif bpy.data.filepath:
        path = os.path.join(os.path.dirname(bpy.data.filepath), "export")
    else:
        raise ValueError("Pick an export folder (or save the .blend first)")
    os.makedirs(path, exist_ok=True)
    return path


def baker_files(baker, choice='AUTO'):
    """'SPLIT' or 'SINGLE' for this baker and Files setting; None if it needs no files."""
    if choice != 'AUTO':
        return choice
    if baker in BAKER_FILES:
        return BAKER_FILES[baker]
    return 'SPLIT'


# --- Writing ---

def _write_fbx(context, path, objects, **options):
    objects = [o for o in objects if o is not None]
    settings = dict(use_selection=True, use_mesh_modifiers=True, add_leaf_bones=False,
                    use_custom_props=False, bake_anim=False, path_mode='AUTO')
    settings.update(options)
    with context.temp_override(selected_objects=objects, active_object=objects[0],
                               object=objects[0]):
        bpy.ops.export_scene.fbx(filepath=path, **settings)
    return path


def _write_glb(context, path, objects, animation):
    with context.temp_override(selected_objects=objects, active_object=objects[0],
                               object=objects[0]):
        bpy.ops.export_scene.gltf(filepath=path, export_format='GLB', use_selection=True,
                                  export_apply=False, export_animations=animation,
                                  export_yup=True)
    return path


def export_bake(context, lows, highs, folder, name, files='SPLIT', cages=()):
    """Write the bake meshes as FBX, modifiers applied (so Triangulate is baked in) and
    normals written as they shade. Returns the paths written.

    'SPLIT': <name>_low.fbx and <name>_high.fbx. 'SINGLE': <name>_bake.fbx with both.
    Cage meshes go in <name>_cage.fbx (their own file either way: bakers load them apart).
    """
    options = dict(object_types={'MESH'}, mesh_smooth_type='OFF',
                   apply_scale_options='FBX_SCALE_ALL')
    written = []
    if files == 'SINGLE':
        both = list(lows) + [h for h in highs if h not in lows]
        if both:
            written.append(_write_fbx(context, os.path.join(folder, f"{name}_bake.fbx"),
                                      both, **options))
    else:
        for part, objects in (("low", lows), ("high", highs)):
            if objects:
                written.append(_write_fbx(context, os.path.join(folder, f"{name}_{part}.fbx"),
                                          list(objects), **options))
    if cages:
        written.append(_write_fbx(context, os.path.join(folder, f"{name}_cage.fbx"),
                                  list(cages), **options))
    return written


def export_engine(context, meshes, folder, name, target='UNREAL', animation=True):
    """Write the engine meshes, the rigs they are skinned to and any collision meshes
    as one file set up for `target`. Returns the path written."""
    rigs = []
    for obj in meshes:
        rig = rigging.skinning_armature(obj)
        if rig is not None and rig not in rigs:
            rigs.append(rig)
    objects = list(meshes) + rigs
    animation = animation and bool(rigs)
    if target == 'GODOT':
        return _write_glb(context, os.path.join(folder, f"{name}.glb"), objects, animation)
    options = dict(object_types={'MESH', 'ARMATURE'}, mesh_smooth_type='FACE',
                   use_armature_deform_only=True, bake_anim=animation,
                   bake_anim_use_nla_strips=False, bake_anim_use_all_actions=False,
                   bake_anim_force_startend_keying=True)
    if target == 'UNITY':
        options["apply_scale_options"] = 'FBX_SCALE_ALL'
        options["bake_space_transform"] = not rigs  # it breaks skinned meshes
    else:
        options["apply_scale_options"] = 'FBX_SCALE_NONE'
    return _write_fbx(context, os.path.join(folder, f"{name}.fbx"), objects, **options)


# --- Ray distance and cages ---

def ray_distances(context, objects, margin=0.1):
    """(front, rear, pairs) the Projection check measures over every low/high pair in
    `objects`, each with `margin` added: how far out a cage must sit and how far in
    rays must reach. pairs is [(low, highs, front, rear)], without the margin."""
    depsgraph = context.evaluated_depsgraph_get()
    pairs = []
    for _base, low, highs, res, _tol, _search in projection.analyse_pairs(
            objects, context.scene.preflight, depsgraph):
        if res["hits"]:
            pairs.append((low, highs, float(res["front"]), float(res["rear"])))
    front = max((p[2] for p in pairs), default=0.0) * (1.0 + margin)
    rear = max((p[3] for p in pairs), default=0.0) * (1.0 + margin)
    return front, rear, pairs


def apply_ray_distance(context, front, rear):
    """Set Blender's own bake to the measured distances: Extrusion pushes the rays' start
    out past the high, Max Ray Distance lets them reach back in as far as it sinks."""
    bake = context.scene.render.bake
    bake.use_selected_to_active = True
    bake.use_cage = False
    bake.cage_extrusion = front
    bake.max_ray_distance = front + rear


def cage_name(low_name):
    """<base>_cage for a <base>_low."""
    if LOW_TOKEN_RE.search(low_name):
        return LOW_TOKEN_RE.sub(lambda m: "_cage" + m.group(1), low_name)
    return low_name + "_cage"


def cage_collection(context):
    name = f"{context.scene.name} Cages"
    coll = bpy.data.collections.get(name)
    if coll is None:
        coll = bpy.data.collections.new(name)
    if coll.name not in context.scene.collection.children:
        context.scene.collection.children.link(coll)
    return coll


def make_cages(context, objects, margin=0.1):
    """A cage for every low that has highs among `objects`: the low as it bakes (modifiers
    applied), pushed out along its averaged normals just past the furthest the high
    stands proud of it. Cages from an earlier run are replaced. Returns the cages."""
    _front, _rear, pairs = ray_distances(context, objects, margin)
    coll = cage_collection(context)
    for old in [o for o in coll.objects if o.get(CAGE_PROP)]:
        data = old.data
        bpy.data.objects.remove(old)
        if data is not None and data.users == 0:
            bpy.data.meshes.remove(data)
    depsgraph = context.evaluated_depsgraph_get()
    made = []
    for low, _highs, front, _rear_d in pairs:
        distance = max(front * (1.0 + margin), 1e-5)
        mesh = bpy.data.meshes.new_from_object(low.evaluated_get(depsgraph),
                                               preserve_all_data_layers=True,
                                               depsgraph=depsgraph)
        linear = np.array(low.matrix_world)[:3, :3]
        inverse = np.linalg.inv(linear)
        count = len(mesh.vertices)
        co = np.empty(count * 3)
        mesh.vertices.foreach_get("co", co)
        normals = np.empty(count * 3)
        mesh.vertices.foreach_get("normal", normals)
        co, normals = co.reshape(-1, 3), normals.reshape(-1, 3)
        world_n = normals @ inverse  # inverse-transpose, as row vectors
        world_n /= np.maximum(np.linalg.norm(world_n, axis=1, keepdims=True), 1e-12)
        local_push = (world_n * distance) @ inverse.T
        mesh.vertices.foreach_set("co", (co + local_push).ravel())
        mesh.update()
        cage = bpy.data.objects.new(cage_name(low.name), mesh)
        mesh.name = cage.name
        cage.matrix_world = low.matrix_world.copy()
        cage.display_type = 'WIRE'
        cage.hide_render = True
        cage[CAGE_PROP] = low.name
        coll.objects.link(cage)
        made.append(cage)
    return made


def cages(context):
    """The cage meshes made by make_cages that are still in the scene."""
    return [o for o in context.scene.objects if o.get(CAGE_PROP)]


# --- Collision ---

def collision_name(name, index, target='UNREAL'):
    if target == 'GODOT':
        return f"{name}-colonly" if index == 0 else f"{name}_{index:02d}-colonly"
    if target == 'UNITY':
        return f"{name}_collider" if index == 0 else f"{name}_collider_{index:02d}"
    return f"UCX_{name}_{index:02d}"


def make_collision(context, meshes, kind='HULL', target='UNREAL'):
    """A collision mesh for each engine mesh in `meshes` that isn't skinned (a rig's
    collision is set up in the engine): its bounding box or its convex hull, named the
    way `target` picks collision up on import. Returns the new objects."""
    made = []
    if kind == 'NONE':
        return made
    for obj in meshes:
        if obj.type != 'MESH' or rigging.skinning_armature(obj) is not None:
            continue
        if not len(obj.data.vertices):
            continue
        bm = bmesh.new()
        if kind == 'BOX':
            verts = [bm.verts.new(Vector(c)) for c in obj.bound_box]
            for face in ((0, 1, 2, 3), (7, 6, 5, 4), (0, 4, 5, 1), (1, 5, 6, 2),
                         (2, 6, 7, 3), (3, 7, 4, 0)):
                bm.faces.new([verts[i] for i in face])
        else:
            for v in obj.data.vertices:
                bm.verts.new(v.co)
            hull = bmesh.ops.convex_hull(bm, input=bm.verts)
            spare = {e for key in ("geom_interior", "geom_unused") for e in hull[key]
                     if isinstance(e, bmesh.types.BMVert)}
            bmesh.ops.delete(bm, geom=list(spare), context='VERTS')
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
        mesh = bpy.data.meshes.new(collision_name(obj.name, 0, target))
        bm.to_mesh(mesh)
        bm.free()
        shell = bpy.data.objects.new(mesh.name, mesh)
        shell.matrix_world = obj.matrix_world.copy()
        shell.display_type = 'WIRE'
        shell.hide_render = True
        shell[COLLISION_PROP] = obj.name
        shell[export.COPY_PROP] = obj.name  # replaced with the engine copies next time
        for coll in obj.users_collection:
            coll.objects.link(shell)
        made.append(shell)
    return made


def bake_sets(context, selected):
    """(lows, highs) to export for the baker: the Low and High collections' meshes, else
    the selected meshes sorted by their _low / _high names."""
    from .dependencies import collection_meshes
    s = context.scene.preflight
    if s.low_collection and s.high_collection:
        return collection_meshes(s.low_collection), collection_meshes(s.high_collection)
    lows = [o for o in selected if LOW_TOKEN_RE.search(o.name)]
    highs = [o for o in selected if HIGH_TOKEN_RE.search(o.name)]
    return lows, highs
