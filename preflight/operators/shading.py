# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Hard edges, custom normals, triangulation and Full Prep."""

import bpy

from ..constants import TRIANGULATE_MOD
from ..core.edges import clear_custom_normals, run_hard_edges
from ..core.triangulate import add_triangulate, remove_triangulate
from ..utils import MeshOperator, object_mode, selected_meshes, unique_meshes


class PREFLIGHT_OT_hard_edges(MeshOperator, bpy.types.Operator):
    """Smooth shade everything and mark sharp edges along UV seams or island borders"""
    bl_idname = "preflight.hard_edges"
    bl_label = "Hard Edges from UVs"

    def execute(self, context):
        self.report({'INFO'}, run_hard_edges(context))
        return {'FINISHED'}


class PREFLIGHT_OT_clear_custom_normals(MeshOperator, bpy.types.Operator):
    """Remove custom split normals from the selected meshes"""
    bl_idname = "preflight.clear_custom_normals"
    bl_label = "Clear Custom Normals"

    def execute(self, context):
        with object_mode(context):
            count = sum(clear_custom_normals(o) for o, _ in unique_meshes(selected_meshes(context)))
        self.report({'INFO'}, f"Cleared custom normals on {count} mesh(es)")
        return {'FINISHED'}


class PREFLIGHT_OT_triangulate(MeshOperator, bpy.types.Operator):
    """Add or update a Triangulate modifier at the end of the stack"""
    bl_idname = "preflight.triangulate"
    bl_label = "Triangulate"

    def execute(self, context):
        objects = selected_meshes(context)
        for obj in objects:
            add_triangulate(obj, context.scene.preflight)
        self.report({'INFO'}, f"Triangulate set on {len(objects)} object(s)")
        return {'FINISHED'}


class PREFLIGHT_OT_untriangulate(bpy.types.Operator):
    """Remove the Pre-Flight Triangulate modifier"""
    bl_idname = "preflight.untriangulate"
    bl_label = "Remove"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(TRIANGULATE_MOD in o.modifiers for o in selected_meshes(context))

    def execute(self, context):
        count = sum(remove_triangulate(o) for o in selected_meshes(context))
        self.report({'INFO'}, f"Removed triangulation from {count} object(s)")
        return {'FINISHED'}


class PREFLIGHT_OT_full_prep(MeshOperator, bpy.types.Operator):
    """Run hard edges and (optionally) triangulation in one go, using the panel settings"""
    bl_idname = "preflight.full_prep"
    bl_label = "Full Prep"

    def execute(self, context):
        msg = run_hard_edges(context)
        settings = context.scene.preflight
        if settings.triangulate_in_full:
            for obj in selected_meshes(context):
                add_triangulate(obj, settings)
            msg += ", triangulated"
        self.report({'INFO'}, msg)
        return {'FINISHED'}
