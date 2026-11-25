# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""The Pre-Flight Triangulate modifier."""

import bpy

from ..constants import TRIANGULATE_MOD


def add_triangulate(obj, settings):
    """Add or update the Triangulate modifier and move it to the end of the stack.

    It goes above any trailing Weighted Normal modifiers so they still apply last.
    """
    mod = obj.modifiers.get(TRIANGULATE_MOD) or obj.modifiers.new(TRIANGULATE_MOD, 'TRIANGULATE')
    mod.quad_method = settings.quad_method
    mod.ngon_method = settings.ngon_method
    mod.min_vertices = 4
    if hasattr(mod, "keep_custom_normals"):
        mod.keep_custom_normals = settings.keep_normals

    mods = obj.modifiers
    others = [m for m in mods if m != mod]
    target = len(others)
    while target > 0 and others[target - 1].type == 'WEIGHTED_NORMAL':
        target -= 1
    index = mods.find(mod.name)
    if index != target:
        if hasattr(mods, "move"):
            mods.move(index, target)
        else:
            with bpy.context.temp_override(object=obj):
                bpy.ops.object.modifier_move_to_index(modifier=mod.name, index=target)


def remove_triangulate(obj):
    """Remove the Triangulate modifier. Returns True if there was one."""
    mod = obj.modifiers.get(TRIANGULATE_MOD)
    if mod:
        obj.modifiers.remove(mod)
        return True
    return False
