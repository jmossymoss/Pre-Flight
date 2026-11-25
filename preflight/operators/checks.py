# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Validate and Select Problem Elements operators."""

import bmesh
import bpy
import numpy as np
from bpy.props import EnumProperty

from ..core.problems import ELEMENT_KINDS, ISSUE_ONLY, problem_mask
from ..utils import MeshOperator, selected_meshes, unique_meshes
from .issues import report_check, run_mesh_check


def select_elements(context, objects, kind, settings, data=""):
    """Enter Edit Mode on `objects` with exactly the problem elements of `kind` selected.

    `objects` must already be selected. Returns the number of elements selected.
    """
    domain = ELEMENT_KINDS[kind][0]
    if context.object and context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    masks = {mesh: problem_mask(obj, kind, settings, data)
             for obj, mesh in unique_meshes(objects)}
    if context.view_layer.objects.active not in objects:
        context.view_layer.objects.active = objects[0]
    bpy.ops.object.mode_set(mode='EDIT')
    context.tool_settings.mesh_select_mode = (domain == 'VERT', domain == 'EDGE', domain == 'FACE')

    # Select through bmesh so edges between two selected vertices aren't selected too.
    for mesh, mask in masks.items():
        bm = bmesh.from_edit_mesh(mesh)
        bm.select_mode = {domain}
        for elem in (*bm.verts, *bm.edges, *bm.faces):
            elem.select = False
        elems = {'VERT': bm.verts, 'EDGE': bm.edges, 'FACE': bm.faces}[domain]
        elems.ensure_lookup_table()
        for i in np.nonzero(mask)[0]:
            elems[int(i)].select_set(True)
        bm.select_flush_mode()
        bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
    return int(sum(m.sum() for m in masks.values()))


class PREFLIGHT_OT_select_problem_edges(MeshOperator, bpy.types.Operator):
    """Select edges or faces that will cause bake artifacts and switch to Edit Mode"""
    bl_idname = "preflight.select_problem_edges"
    bl_label = "Select Problem Elements"

    kind: EnumProperty(items=[(k, v[1], v[2]) for k, v in ELEMENT_KINDS.items()
                              if k not in ISSUE_ONLY])

    @classmethod
    def description(cls, context, props):
        return ELEMENT_KINDS[props.kind][2]

    def execute(self, context):
        total = select_elements(context, selected_meshes(context), self.kind,
                                context.scene.preflight)
        label = ELEMENT_KINDS[self.kind][1].lower()
        self.report({'WARNING' if total else 'INFO'},
                    f"{total} {label}" if total else f"No {label} found")
        return {'FINISHED'}


class PREFLIGHT_OT_validate(MeshOperator, bpy.types.Operator):
    """Check the selected meshes for common bake problems and list them under Issues"""
    bl_idname = "preflight.validate"
    bl_label = "Validate"
    bl_options = {'REGISTER'}

    def execute(self, context):
        objects = selected_meshes(context)
        issues = run_mesh_check(context, objects)
        report_check(self, context, issues, "Pre-Flight",
                     f"All {len(objects)} object(s) look good")
        return {'FINISHED'}
