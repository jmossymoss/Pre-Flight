# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Texel density overlay.

Colours each visible low (a mesh with UVs that isn't a high) by its triangles' texel
density against a target: the texture set's median, or a custom value. Objects in
Edit Mode are skipped, since their mesh data is stale until they leave it.
"""

import bpy
import gpu
import numpy as np
from bpy.app.handlers import persistent
from gpu_extras.batch import batch_for_shader

from .core import mapsize
from .utils import tag_redraw

_state = {"handle": None, "batches": None, "stats": {}}


def texel_overlay_invalidate():
    _state["batches"] = None
    tag_redraw()


def overlay_objects(context):
    """Visible lows in Object Mode that have UVs."""
    from .core.checks import is_high
    s = context.scene.preflight
    space = context.space_data
    viewport = space if isinstance(space, bpy.types.SpaceView3D) else None
    return [o for o in context.view_layer.objects
            if o.type == 'MESH' and o.mode == 'OBJECT' and o.data.uv_layers
            and o.visible_get(viewport=viewport) and not is_high(o, s.high_collection)]


def stats(obj_name):
    """(median density, target) of an object from the last build, or None."""
    return _state["stats"].get(obj_name)


def build(context):
    """Build the overlay batches and refresh the per-object stats.

    Returns (shader, batches); the shader is None in background mode.
    """
    from .core import texel
    s = context.scene.preflight
    scale = context.scene.unit_settings.scale_length or 1.0
    per_obj, sets = [], {}
    for obj in overlay_objects(context):
        size = mapsize.texel_size(mapsize.of_object(s, obj))
        per_tri, _, density, world = texel.triangle_densities(obj, obj.data, size, scale)
        if not len(per_tri):
            continue
        tex_set = obj.active_material.name if obj.active_material else obj.name
        sets.setdefault(tex_set, []).append((density, world))
        per_obj.append((obj, tex_set, per_tri, density, world))

    medians = {}
    for tex_set, entries in sets.items():
        d = np.concatenate([e[0] for e in entries])
        w = np.concatenate([e[1] for e in entries])
        medians[tex_set] = texel.weighted_median(d, w) if len(d) else None

    shader = gpu.shader.from_builtin('SMOOTH_COLOR') if not bpy.app.background else None
    from . import preferences
    prefs = preferences.get(context)
    palette = (prefs.color_texel_low, prefs.color_texel_ok, prefs.color_texel_high)
    batches, info = [], {}
    for obj, tex_set, per_tri, density, world in per_obj:
        target = s.texel_target if s.texel_target_mode == 'CUSTOM' else medians[tex_set]
        own = texel.weighted_median(density, world) if len(density) else None
        info[obj.name] = (own, target)
        if not target or shader is None:
            continue
        colours = texel.density_colours(per_tri, target, s.texel_tolerance / 100, palette)
        me = obj.data
        n = len(me.vertices)
        co = np.empty(n * 3, dtype=np.float32)
        no = np.empty(n * 3, dtype=np.float32)
        me.vertices.foreach_get("co", co)
        me.vertices.foreach_get("normal", no)
        co, no = co.reshape(-1, 3), no.reshape(-1, 3)
        tris = np.empty(len(me.loop_triangles) * 3, dtype=np.int32)
        me.loop_triangles.foreach_get("vertices", tris)
        extent = float(np.ptp(co, axis=0).max()) if n else 1.0
        pts = co[tris] + no[tris] * max(extent, 1e-4) * 0.001  # avoid z-fighting
        mat = np.array(obj.matrix_world, dtype=np.float32)
        pts = pts @ mat[:3, :3].T + mat[:3, 3]
        col = np.repeat(colours.astype(np.float32), 3, axis=0)
        batches.append(batch_for_shader(shader, 'TRIS', {"pos": pts, "color": col}))
    _state["stats"] = info
    return shader, batches


def draw_texel_overlay():
    context = bpy.context
    if context.view_layer is None or not context.window_manager.preflight.overlay_texel:
        return
    if _state["batches"] is None:
        _state["batches"] = build(context)
    shader, batches = _state["batches"]
    if shader is None:
        return
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('LESS_EQUAL')
    gpu.state.face_culling_set('BACK')
    try:
        shader.bind()
        for batch in batches:
            batch.draw(shader)
    finally:
        gpu.state.face_culling_set('NONE')
        gpu.state.depth_test_set('NONE')
        gpu.state.blend_set('NONE')


@persistent
def on_depsgraph_update(scene, depsgraph):
    if _state["batches"] is None:
        return
    for update in depsgraph.updates:
        if isinstance(update.id, bpy.types.Mesh) or (
                isinstance(update.id, bpy.types.Object)
                and (update.is_updated_geometry or update.is_updated_transform)):
            _state["batches"] = None  # a texture set's median may change: rebuild all
            return


@persistent
def on_load(*_):
    _state["batches"] = None


def texel_overlay_sync(context):
    """Install the draw handler while the overlay is on, remove it otherwise."""
    wanted = context.window_manager.preflight.overlay_texel
    handlers = bpy.app.handlers
    if wanted and _state["handle"] is None:
        _state["handle"] = bpy.types.SpaceView3D.draw_handler_add(
            draw_texel_overlay, (), 'WINDOW', 'POST_VIEW')
        handlers.depsgraph_update_post.append(on_depsgraph_update)
        handlers.load_post.append(on_load)
    elif not wanted and _state["handle"] is not None:
        texel_overlay_shutdown()
    texel_overlay_invalidate()


def texel_overlay_shutdown():
    if _state["handle"] is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_state["handle"], 'WINDOW')
        _state["handle"] = None
    for lst, fn in ((bpy.app.handlers.depsgraph_update_post, on_depsgraph_update),
                    (bpy.app.handlers.load_post, on_load)):
        if fn in lst:
            lst.remove(fn)
    _state["batches"] = None
    _state["stats"] = {}
