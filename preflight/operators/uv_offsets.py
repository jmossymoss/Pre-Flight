# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""UV offset operator for Mirror and Array modifiers."""

import bpy
from bpy.props import BoolProperty, EnumProperty

from ..constants import UV_OFFSET_KINDS
from ..core.uv_layout import uv_offset_ok
from ..overlay import overlay_invalidate
from ..utils import selected_meshes


class PREFLIGHT_OT_set_uv_offset(bpy.types.Operator):
    """Offset the copies made by these modifiers one UV tile to the right"""
    bl_idname = "preflight.set_uv_offset"
    bl_label = "Set +1U"
    bl_options = {'REGISTER', 'UNDO'}

    kind: EnumProperty(items=[(k, v, "") for k, v in UV_OFFSET_KINDS.items()])
    only_unset: BoolProperty(
        name="Only Unset",
        description="Leave modifiers that already have a U or V offset alone",
        default=True,
    )

    @classmethod
    def description(cls, context, props):
        return (f"Set Offset U = 1 on {UV_OFFSET_KINDS[props.kind]} modifiers "
                "of the selected objects")

    def execute(self, context):
        count = 0
        for obj in selected_meshes(context):
            for mod in obj.modifiers:
                if mod.type == self.kind and not (self.only_unset and uv_offset_ok(mod)):
                    mod.offset_u = 1.0
                    count += 1
        overlay_invalidate()
        self.report({'INFO'}, f"Set +1U on {count} {UV_OFFSET_KINDS[self.kind]} modifier(s)")
        return {'FINISHED'}
