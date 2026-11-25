# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""UV offset overlay.

Tints the evaluated result of objects with an enabled Mirror/Array modifier:
green when the copies' UVs are offset, red when not. Batches are cached per object
and rebuilt when the depsgraph reports a change.
"""

import bpy
import gpu
import numpy as np
from bpy.app.handlers import persistent
from gpu_extras.batch import batch_for_shader

from . import preferences
from .core.uv_layout import uv_offset_ok
from .utils import tag_redraw

_overlay = {"handle": None, "cache": {}}


def overlay_invalidate():
    _overlay["cache"].clear()
    tag_redraw()


def overlay_kinds(wm):
    """The modifier types whose overlay is switched on."""
    props = wm.preflight
    return {k for k, on in (('MIRROR', props.overlay_mirror), ('ARRAY', props.overlay_array)) if on}


def overlay_status(obj, kinds):
    """'OK' or 'BAD' for an object with matching modifiers, None without any."""
    mods = [m for m in obj.modifiers if m.type in kinds and m.show_viewport]
    if not mods:
        return None
    return 'OK' if all(uv_offset_ok(m) for m in mods) else 'BAD'


def build_overlay_batch(obj, depsgraph, shader):
    obj_eval = obj.evaluated_get(depsgraph)
    me = obj_eval.to_mesh()
    try:
        me.calc_loop_triangles()
        tri_count = len(me.loop_triangles)
        if not tri_count:
            return None

        co = np.empty(len(me.vertices) * 3, dtype=np.float32)
        no = np.empty_like(co)
        me.vertices.foreach_get("co", co)
        me.vertices.foreach_get("normal", no)
        co, no = co.reshape(-1, 3), no.reshape(-1, 3)

        tri_verts = np.empty(tri_count * 3, dtype=np.int32)
        me.loop_triangles.foreach_get("vertices", tri_verts)

        # Push the surface out a little along the normals to avoid z-fighting.
        size = float(np.ptp(co, axis=0).max()) if len(co) else 1.0
        pts = co[tri_verts] + no[tri_verts] * max(size, 1e-4) * 0.001
        mat = np.array(obj_eval.matrix_world, dtype=np.float32)
        pts = pts @ mat[:3, :3].T + mat[:3, 3]
        return batch_for_shader(shader, 'TRIS', {"pos": pts})
    finally:
        obj_eval.to_mesh_clear()


def draw_overlay():
    context = bpy.context
    kinds = overlay_kinds(context.window_manager)
    if not kinds or context.view_layer is None:
        return

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    prefs = preferences.get(context)
    colours = {True: tuple(prefs.color_offset_ok), False: tuple(prefs.color_offset_bad)}
    depsgraph = context.evaluated_depsgraph_get()
    viewport = context.space_data if isinstance(context.space_data, bpy.types.SpaceView3D) else None
    cache = _overlay["cache"]

    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('LESS_EQUAL')
    gpu.state.face_culling_set('BACK')
    try:
        shader.bind()
        for obj in context.view_layer.objects:
            if obj.type != 'MESH' or not obj.visible_get(viewport=viewport):
                continue
            status = overlay_status(obj, kinds)
            if status is None:
                continue
            if obj.name not in cache:
                cache[obj.name] = build_overlay_batch(obj, depsgraph, shader)
            batch = cache[obj.name]
            if batch:
                shader.uniform_float("color", colours[status == 'OK'])
                batch.draw(shader)
    finally:
        gpu.state.face_culling_set('NONE')
        gpu.state.depth_test_set('NONE')
        gpu.state.blend_set('NONE')


@persistent
def on_depsgraph_update(scene, depsgraph):
    cache = _overlay["cache"]
    if not cache:
        return
    for update in depsgraph.updates:
        if isinstance(update.id, bpy.types.Object):
            cache.pop(update.id.name, None)
        elif isinstance(update.id, bpy.types.Mesh):
            cache.clear()  # shared mesh data: cheaper to rebuild everything
            return


@persistent
def on_frame_change(scene, depsgraph=None):
    _overlay["cache"].clear()


def overlay_sync(context):
    """Install the draw handler while any overlay is on, remove it otherwise."""
    wanted = bool(overlay_kinds(context.window_manager))
    handlers = bpy.app.handlers
    if wanted and _overlay["handle"] is None:
        _overlay["handle"] = bpy.types.SpaceView3D.draw_handler_add(
            draw_overlay, (), 'WINDOW', 'POST_VIEW')
        handlers.depsgraph_update_post.append(on_depsgraph_update)
        handlers.frame_change_post.append(on_frame_change)
    elif not wanted and _overlay["handle"] is not None:
        overlay_shutdown()
    overlay_invalidate()


def overlay_shutdown():
    if _overlay["handle"] is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_overlay["handle"], 'WINDOW')
        _overlay["handle"] = None
    for lst, fn in ((bpy.app.handlers.depsgraph_update_post, on_depsgraph_update),
                    (bpy.app.handlers.frame_change_post, on_frame_change)):
        if fn in lst:
            lst.remove(fn)
    _overlay["cache"].clear()
