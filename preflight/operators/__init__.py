# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Registration for all Pre-Flight operators."""

import bpy

from . import (checks, explode, exporting, id_colors, issues, mechanisms, pairs, rigging,
               shading, textures, uv_offsets, uv_tools)

classes = (
    shading.PREFLIGHT_OT_hard_edges,
    shading.PREFLIGHT_OT_clear_custom_normals,
    shading.PREFLIGHT_OT_triangulate,
    shading.PREFLIGHT_OT_untriangulate,
    shading.PREFLIGHT_OT_full_prep,
    uv_offsets.PREFLIGHT_OT_set_uv_offset,
    checks.PREFLIGHT_OT_validate,
    checks.PREFLIGHT_OT_select_problem_edges,
    pairs.PREFLIGHT_OT_match_names,
    pairs.PREFLIGHT_OT_duplicate_pair,
    pairs.PREFLIGHT_OT_separate_cutters,
    pairs.PREFLIGHT_OT_pair_selected,
    pairs.PREFLIGHT_OT_sync_data_names,
    pairs.PREFLIGHT_OT_check_names,
    explode.PREFLIGHT_OT_explode,
    explode.PREFLIGHT_OT_unexplode,
    *issues.classes,
    *uv_tools.classes,
    *id_colors.classes,
    *textures.classes,
    *rigging.classes,
    *mechanisms.classes,
    *exporting.classes,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.app.timers.register(textures.resume_auto_reload, first_interval=0.5)


def unregister():
    textures.unregister_timer()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
