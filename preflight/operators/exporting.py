# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Export operators: bake files, the engine file, cages and Blender's bake distances."""

import os

import bpy

from ..core import bakers, export, exporter
from ..core.dependencies import collection_meshes
from ..utils import object_mode, selected_meshes
from .issues import names_check_objects


def _folder_and_name(operator, context):
    s = context.scene.preflight
    try:
        folder = exporter.export_folder(context, s.export_folder)
    except (ValueError, OSError) as ex:
        operator.report({'ERROR'}, str(ex))
        return None, None
    return folder, exporter.asset_name(context, s.export_name)


def _shown(paths):
    return ", ".join(os.path.basename(p) for p in paths)


class PREFLIGHT_OT_export_bake(bpy.types.Operator):
    """Write the low and high meshes for your baker: one file or two, as it wants them,
with modifiers applied and the cages if you made any. Uses the Low and High collections,
or the selected _low / _high meshes"""
    bl_idname = "preflight.export_bake"
    bl_label = "Export for Baker"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        s = context.scene.preflight
        if exporter.baker_files(s.baker, s.bake_files) is None:
            cls.poll_message_set(f"{bakers.BAKERS[s.baker]['name']} bakes inside Blender: "
                                 "nothing to export (set Files to export anyway)")
            return False
        lows, highs = exporter.bake_sets(context, selected_meshes(context))
        if not lows and not highs:
            cls.poll_message_set("Set the Low and High collections, or select meshes "
                                 "named _low / _high")
            return False
        return True

    def execute(self, context):
        s = context.scene.preflight
        folder, name = _folder_and_name(self, context)
        if folder is None:
            return {'CANCELLED'}
        lows, highs = exporter.bake_sets(context, selected_meshes(context))
        files = exporter.baker_files(s.baker, s.bake_files)
        cages = exporter.cages(context) if s.bake_export_cages else []
        with object_mode(context):
            try:
                written = exporter.export_bake(context, lows, highs, folder, name, files, cages)
            except RuntimeError as ex:
                self.report({'ERROR'}, f"Export failed: {ex}")
                return {'CANCELLED'}
        missing = [n for n, objs in (("lows", lows), ("highs", highs)) if not objs]
        note = f" (no {' or '.join(missing)} found)" if missing else ""
        self.report({'WARNING'} if missing else {'INFO'},
                    f"Wrote {_shown(written)} to {folder}{note}")
        return {'FINISHED'}


def _engine_parts(context):
    """The selected meshes, else the Low collection's."""
    parts = [o for o in context.selected_objects if o.type == 'MESH'
             and not o.get(export.COPY_PROP)]
    low = context.scene.preflight.low_collection
    if not parts and low is not None:
        parts = collection_meshes(low)
    return parts


class PREFLIGHT_OT_export_engine(bpy.types.Operator):
    """Make the engine mesh (as Make Engine Mesh does) and write it for your game engine,
with its rig, its animation and collision meshes if you asked for them"""
    bl_idname = "preflight.export_engine"
    bl_label = "Export to Engine"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not _engine_parts(context):
            cls.poll_message_set("Select the lows, or set the Low collection")
            return False
        return True

    def execute(self, context):
        s = context.scene.preflight
        folder, name = _folder_and_name(self, context)
        if folder is None:
            return {'CANCELLED'}
        parts = _engine_parts(context)
        with object_mode(context):
            copies = export.make_engine_copies(context, parts, s.export_combine)
            shells = exporter.make_collision(context, copies, s.export_collision,
                                             s.engine_target)
            try:
                path = exporter.export_engine(context, copies + shells, folder, name,
                                              s.engine_target, s.engine_animation)
            except RuntimeError as ex:
                self.report({'ERROR'}, f"Export failed: {ex}")
                return {'CANCELLED'}
        extra = f", {len(shells)} collision mesh(es)" if shells else ""
        self.report({'INFO'}, f"Wrote {os.path.basename(path)} to {folder}: "
                              f"{len(copies)} mesh(es){extra}")
        return {'FINISHED'}


def _pair_objects(context):
    return names_check_objects(context)[0]


class PREFLIGHT_OT_make_cages(bpy.types.Operator):
    """Make a cage for each low from what the Projection check measures: the low pushed
out just past its high. Export for Baker writes them to their own file"""
    bl_idname = "preflight.make_cages"
    bl_label = "Make Cages"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return PREFLIGHT_OT_set_ray_distance.poll(context)

    def execute(self, context):
        s = context.scene.preflight
        with object_mode(context):
            cages = exporter.make_cages(context, _pair_objects(context),
                                        s.cage_margin / 100.0)
        if not cages:
            self.report({'WARNING'}, "No low / high pairs where rays reach the high: "
                                     "run Match & Rename first")
            return {'CANCELLED'}
        self.report({'INFO'}, f"{len(cages)} cage(s) in '{cages[0].users_collection[0].name}'")
        return {'FINISHED'}


class PREFLIGHT_OT_set_ray_distance(bpy.types.Operator):
    """Set Blender's own bake (Selected to Active) to the distances the Projection check
measures: Extrusion and Max Ray Distance, with the margin added"""
    bl_idname = "preflight.set_ray_distance"
    bl_label = "Set Bake Distances"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        s = context.scene.preflight
        if not ((s.low_collection and s.high_collection) or selected_meshes(context)):
            cls.poll_message_set("Set the Low and High collections, or select the pairs")
            return False
        return True

    def execute(self, context):
        s = context.scene.preflight
        with object_mode(context):
            front, rear, pairs = exporter.ray_distances(context, _pair_objects(context),
                                                        s.cage_margin / 100.0)
        if not pairs:
            self.report({'WARNING'}, "No low / high pairs where rays reach the high: "
                                     "run Match & Rename first")
            return {'CANCELLED'}
        exporter.apply_ray_distance(context, front, rear)
        from ..core.checks import fmt_length
        self.report({'INFO'}, f"Bake: Extrusion {fmt_length(front)}, Max Ray Distance "
                              f"{fmt_length(front + rear)} (from {len(pairs)} pair(s))")
        return {'FINISHED'}


classes = (
    PREFLIGHT_OT_export_bake,
    PREFLIGHT_OT_export_engine,
    PREFLIGHT_OT_make_cages,
    PREFLIGHT_OT_set_ray_distance,
)
