# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Assign and clear ID colours on highs (see core/id_colors.py)."""

import bpy
from bpy.props import BoolProperty, EnumProperty

from ..core import id_colors
from ..core.checks import is_high
from ..core.naming import natural_key
from ..utils import object_mode, selected_meshes, unique_meshes

TARGETS = (
    ('HIGHS', "All Highs", "Every high in the scene: named _high (or _hi, _hp, _h) "
                           "or in the High collection"),
    ('SELECTED', "Selected", "The selected meshes"),
)


def target_meshes(context, target):
    """The meshes `target` refers to, in natural name order."""
    if target == 'SELECTED':
        objects = selected_meshes(context)
    else:
        high_coll = context.scene.preflight.high_collection
        objects = [o for o in context.scene.objects
                   if o.type == 'MESH' and is_high(o, high_coll)]
    return sorted(objects, key=lambda o: natural_key(o.name))


def show_in_viewport(context):
    """Set Solid viewports to colour by attribute so the IDs are visible."""
    for area in context.screen.areas if context.screen else ():
        if area.type == 'VIEW_3D':
            shading = area.spaces.active.shading
            if shading.type == 'SOLID':
                shading.color_type = 'VERTEX'


class PREFLIGHT_OT_assign_id_colors(bpy.types.Operator):
    """Give the highs distinct flat colours (a 'BakeID' colour attribute, exported as
vertex colours) for a Color ID / ID map bake"""
    bl_idname = "preflight.assign_id_colors"
    bl_label = "Assign ID Colors"
    bl_options = {'REGISTER', 'UNDO'}

    target: EnumProperty(name="Objects", items=TARGETS, default='HIGHS')
    mode: EnumProperty(name="Colour By", items=id_colors.MODES, default='OBJECT')
    show: BoolProperty(name="Show in Viewport", default=True,
                       description="Switch Solid shading to show colour attributes")

    def execute(self, context):
        objects = target_meshes(context, self.target)
        if not objects:
            if self.target == 'HIGHS':
                msg = "No highs found: name them _high or set the High collection"
            else:
                msg = "Select some meshes"
            self.report({'WARNING'}, msg)
            return {'CANCELLED'}
        meshes = [me for _, me in unique_meshes(objects)]
        with object_mode(context):
            if self.mode == 'OBJECT':
                for me, color in zip(meshes, id_colors.palette(len(meshes))):
                    id_colors.fill(me, color)
                count = len(meshes)
            elif self.mode == 'PART':
                count = id_colors.fill_parts(meshes)
            else:
                names = sorted({m.name if m else "" for me in meshes
                                for m in (me.materials or [None])}, key=natural_key)
                colors = dict(zip(names, id_colors.palette(len(names))))
                for me in meshes:
                    id_colors.fill_materials(me, colors)
                count = len(names)
        if self.show:
            show_in_viewport(context)
        unit = {'OBJECT': "mesh", 'PART': "part", 'MATERIAL': "material"}[self.mode]
        self.report({'INFO'}, f"{count} ID colour(s), one per {unit}, on "
                              f"{len(objects)} object(s)")
        return {'FINISHED'}


class PREFLIGHT_OT_clear_id_colors(bpy.types.Operator):
    """Remove the 'BakeID' colour attribute added by Assign ID Colors"""
    bl_idname = "preflight.clear_id_colors"
    bl_label = "Clear ID Colors"
    bl_options = {'REGISTER', 'UNDO'}

    target: EnumProperty(name="Objects", items=TARGETS, default='HIGHS')

    def execute(self, context):
        objects = target_meshes(context, self.target)
        with object_mode(context):
            removed = sum(id_colors.clear(me) for _, me in unique_meshes(objects))
        self.report({'INFO'}, f"Cleared ID colours from {removed} mesh(es)")
        return {'FINISHED'}


classes = (PREFLIGHT_OT_assign_id_colors, PREFLIGHT_OT_clear_id_colors)
