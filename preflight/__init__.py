# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Pre-Flight: prep meshes for baking.

Package layout:
    core/          mesh and scene logic and checks, no operators or UI
    operators/     bpy operators, one module per panel area
    ui/            sidebar panel and icons
    issues.py      the persistent issue list
    overlay.py, texel_overlay.py   viewport overlays

bl_info lets the folder install as a legacy add-on; Blender 4.2+ extensions read
blender_manifest.toml instead.
"""

bl_info = {
    "name": "Pre-Flight",
    "author": "Jordan Moss",
    "version": (3, 4, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Pre-Flight",
    "description": "Prep meshes for baking: hard edges, UV checks, low/high pairs",
    "category": "Mesh",
}

# F3 > Reload Scripts: reload submodules in dependency order.
if "constants" in locals():
    import importlib
    for _mod in (constants, utils,
                 core.edges, core.triangulate, core.uv_layout, core.naming,
                 core.dependencies, core.geometry, core.mapsize, core.texel, core.uv_stack,
                 core.bakers, core.id_colors, core.textures, core.rigging, core.export,
                 core.projection, core.problems, core.checks,
                 overlay, texel_overlay, issues, integrations, properties,
                 operators.shading, operators.uv_offsets, operators.issues,
                 operators.uv_tools, operators.checks, operators.pairs, operators.explode,
                 operators.id_colors, operators.textures, operators.rigging, operators,
                 ui.icons, ui.panels, ui, preferences, migrate):
        importlib.reload(_mod)

from . import (constants, core, integrations, issues, migrate, operators,  # noqa: E402,F401
               overlay, preferences, properties, texel_overlay, ui, utils)
from .core import (bakers, checks, dependencies, edges, export, geometry,  # noqa: E402,F401
                   id_colors, mapsize, naming, problems, projection, rigging, texel,
                   textures, triangulate, uv_layout, uv_stack)


def register():
    properties.register()
    preferences.register()
    operators.register()
    ui.register(preferences.sidebar_category())
    migrate.register()


def unregister():
    migrate.unregister()
    overlay.overlay_shutdown()
    texel_overlay.texel_overlay_shutdown()
    ui.unregister()
    operators.unregister()
    preferences.unregister()
    properties.unregister()
