# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Move data saved by Bake Prep (Pre-Flight's old name) to Pre-Flight's names on load.

Covers the "bake_prep" scene and material settings groups, "bake_prep_*" custom
properties and mesh attributes, and the "Triangulate_BakePrep" modifier.
"""

import bpy
from bpy.app.handlers import persistent

from .constants import TRIANGULATE_MOD

OLD_GROUP = "bake_prep"
NEW_GROUP = "preflight"
OLD_PREFIX = "bake_prep_"
NEW_PREFIX = "preflight_"
OLD_TRIANGULATE = "Triangulate_BakePrep"


def _stores(id_block):
    """Where an unregistered add-on's settings can be stored: Blender 5's system
    properties and the custom properties (used by Blender 4.x)."""
    stores = []
    if hasattr(id_block, "bl_system_properties_get"):
        system = id_block.bl_system_properties_get()
        if system is not None:
            stores.append(system)
    stores.append(id_block)
    return stores


def _apply(target, data):
    """Write saved values (enums as numbers, collections as lists) through RNA."""
    for key, value in data.items():
        prop = target.bl_rna.properties.get(key)
        if prop is None or (prop.is_readonly and prop.type != 'COLLECTION'):
            continue
        try:
            if prop.type == 'COLLECTION':
                items = getattr(target, key)
                items.clear()
                for entry in value:
                    _apply(items.add(), entry)
            elif prop.type == 'POINTER':
                if isinstance(value, dict):
                    _apply(getattr(target, key), value)
                else:
                    setattr(target, key, value)
            elif prop.type == 'ENUM' and isinstance(value, int):
                name = next((i.identifier for i in prop.enum_items if i.value == value), None)
                if name is not None:
                    setattr(target, key, name)
            elif prop.type == 'BOOLEAN' and not getattr(prop, "is_array", False):
                setattr(target, key, bool(value))
            else:
                setattr(target, key, value)
        except (TypeError, ValueError, AttributeError):
            pass  # a setting that no longer exists or changed type: keep the default


def _move_group(id_block):
    """Carry the old settings group over to Pre-Flight's, then drop it."""
    moved = False
    for store in _stores(id_block):
        old = store.get(OLD_GROUP)
        if old is None:
            continue
        if hasattr(old, "to_dict"):
            _apply(getattr(id_block, NEW_GROUP), old.to_dict())
        del store[OLD_GROUP]
        moved = True
    return moved


def _move_props(owner):
    """Rename "bake_prep_*" custom properties on any ID or node."""
    moved = 0
    for key in [k for k in owner.keys() if k.startswith(OLD_PREFIX)]:
        new_key = NEW_PREFIX + key[len(OLD_PREFIX):]
        if new_key not in owner:
            owner[new_key] = owner[key]
        del owner[key]
        moved += 1
    return moved


def migrate():
    """Move every Bake Prep setting in the open file to its Pre-Flight name.

    Returns the number of items moved.
    """
    count = 0
    for scene in bpy.data.scenes:
        if not scene.library:
            count += _move_group(scene)
            count += _move_props(scene)
    for mat in bpy.data.materials:
        if mat.library:
            continue
        count += _move_group(mat)
        count += _move_props(mat)
        if mat.node_tree is not None:
            for node in mat.node_tree.nodes:
                count += _move_props(node)
    for mesh in bpy.data.meshes:
        if mesh.library or mesh.is_editmode:
            continue
        count += _move_props(mesh)
        for attr in list(mesh.attributes):
            if attr.name.startswith(OLD_PREFIX):
                new_name = NEW_PREFIX + attr.name[len(OLD_PREFIX):]
                if mesh.attributes.get(new_name) is None:
                    attr.name = new_name
                    count += 1
    for obj in bpy.data.objects:
        if obj.library:
            continue
        count += _move_props(obj)
        mod = obj.modifiers.get(OLD_TRIANGULATE)
        if mod is not None and obj.modifiers.get(TRIANGULATE_MOD) is None:
            mod.name = TRIANGULATE_MOD
            count += 1
    return count


def _update_file():
    from . import preferences
    migrate()
    preferences.apply_new_file_defaults()


@persistent
def on_load_post(*_args):
    _update_file()


def _migrate_open_file():
    try:
        _update_file()
    except (AttributeError, RuntimeError):
        pass  # no file data reachable yet (e.g. while Blender is starting)
    return None


def register():
    bpy.app.handlers.load_post.append(on_load_post)
    # Also migrate a file that is already open when the add-on is enabled.
    bpy.app.timers.register(_migrate_open_file, first_interval=0.1)


def unregister():
    if on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(on_load_post)
