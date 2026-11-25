# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Engine copies: the lows collapsed into what a game engine wants.

The bake setup is left alone. Copies get their modifiers applied (Triangulate, Mirror,
...), lose their _low suffixes, and separate parts are combined: all into one mesh, one
per texture set, or one per top-level parent. Rigged parts stay skinned. Copies go in an
"<scene> Engine" collection and are replaced each time the copy is made.
"""

import re

import bpy

from . import rigging
from .naming import LOW_TOKEN_RE

COPY_PROP = "preflight_engine_copy"  # on engine copies, so a new copy replaces them
COMBINE_ITEMS = [
    ('ALL', "All in One", "Combine every part into a single mesh"),
    ('MATERIAL', "Per Texture Set", "One mesh per material (texture set)"),
    ('PARENT', "Per Parent", "One mesh per top-level parent: separate props stay separate"),
    ('NONE', "Keep Separate", "Copy each part on its own"),
]


def engine_name(name):
    """`name` without its _low suffix (and Blender's .001 numbering)."""
    name = re.sub(r"\.\d{3,}$", "", name)
    return LOW_TOKEN_RE.sub(lambda m: m.group(1), name)


def export_collection(context, name=None):
    """The collection engine copies go in (made under the scene's collection)."""
    name = name or f"{context.scene.name} Engine"
    coll = bpy.data.collections.get(name)
    if coll is None:
        coll = bpy.data.collections.new(name)
    if coll.name not in context.scene.collection.children:
        context.scene.collection.children.link(coll)
    return coll


def clear_copies(collection):
    """Remove the engine copies a previous run left in `collection`."""
    for obj in [o for o in collection.objects if o.get(COPY_PROP)]:
        data = obj.data
        bpy.data.objects.remove(obj)
        if data is not None and data.users == 0 and isinstance(data, bpy.types.Mesh):
            bpy.data.meshes.remove(data)


def _top_parent(obj, parts):
    """The top of `obj`'s hierarchy within `parts`. Rigged parts all hang off the rig,
    so for them the bone hierarchy decides: the part on the top-level bone above."""
    while obj.parent is not None and obj.parent in parts:
        obj = obj.parent
    armature, bone = rigging.bone_target(obj)
    if armature is None:
        return obj
    by_bone = {}
    for part in parts:
        owner, name = rigging.bone_target(part)
        if owner == armature and name:
            by_bone[name] = part
    top = obj
    current = armature.data.bones.get(bone)
    while current is not None:
        if current.name in by_bone:
            top = by_bone[current.name]
        current = current.parent
    return top


def _group_key(obj, combine, parts):
    if combine == 'ALL':
        return "all"
    if combine == 'MATERIAL':
        return obj.active_material.name if obj.active_material else obj.name
    if combine == 'PARENT':
        return _top_parent(obj, parts).name
    return obj.name


def _baked_copy(obj, depsgraph, collection):
    """A copy of `obj` with its modifiers applied (but not an Armature modifier)."""
    armature_mods = [m for m in obj.modifiers if m.type == 'ARMATURE' and m.show_viewport]
    for mod in armature_mods:
        mod.show_viewport = False
    depsgraph.update()
    evaluated = obj.evaluated_get(depsgraph)
    mesh = bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=True,
                                           depsgraph=depsgraph)
    for mod in armature_mods:
        mod.show_viewport = True
    copy = bpy.data.objects.new(engine_name(obj.name), mesh)
    copy.matrix_world = obj.matrix_world.copy()
    copy[COPY_PROP] = obj.name
    collection.objects.link(copy)
    for group in obj.vertex_groups:
        if group.name not in copy.vertex_groups:
            copy.vertex_groups.new(name=group.name)
    return copy


def make_engine_copies(context, objects, combine='ALL', collection=None):
    """Engine copies of the mesh `objects`. Returns the new objects.

    Parts bound to bones become rigidly skinned copies, so combined meshes still follow
    their bones; the copies are skinned to the same armature.
    """
    parts = [o for o in objects if o.type == 'MESH']
    if not parts:
        return []
    collection = collection or export_collection(context)
    clear_copies(collection)
    depsgraph = context.evaluated_depsgraph_get()

    groups = {}
    for obj in parts:
        armature = rigging.skinning_armature(obj)
        copy = _baked_copy(obj, depsgraph, collection)
        if armature is not None:
            if rigging.follows_pose(obj, armature):
                rigging.rigid_skin(copy, armature, rigging.bone_target(obj)[1], posed=True)
            else:
                rigging.armature_modifier(copy, armature)
                copy.parent = armature
                copy.matrix_world = obj.matrix_world.copy()
        groups.setdefault(_group_key(obj, combine, set(parts)), []).append(copy)

    result = []
    view_layer = context.view_layer
    for key, copies in groups.items():
        if len(copies) > 1:
            for obj in context.selected_objects:
                obj.select_set(False)
            for obj in copies:
                obj.select_set(True)
            view_layer.objects.active = copies[0]
            with context.temp_override(active_object=copies[0], selected_objects=copies,
                                       selected_editable_objects=copies):
                bpy.ops.object.join()
        merged = copies[0]
        merged.name = engine_name(key if combine != 'NONE' else merged.name)
        if combine == 'ALL':
            merged.name = engine_name(_top_parent(parts[0], set(parts)).name)
        merged.data.name = merged.name
        merged[COPY_PROP] = merged.name
        result.append(merged)
    for obj in context.selected_objects:
        obj.select_set(False)
    for obj in result:
        obj.select_set(True)
    if result:
        view_layer.objects.active = result[0]
    return result
