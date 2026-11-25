# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""UV operators: pack, stack, texel density, map ratio and baker presets.

Packing and stacking use UVPackmaster or Zen UV when installed, else Blender's own.
"""

import bmesh
import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty

from .. import integrations
from .. import issues as issue_store
from ..core import bakers, mapsize, texel
from ..core.checks import ratio_text
from ..core.edges import edge_analysis
from ..core.uv_layout import triangle_uvs, uv_islands
from ..utils import MeshOperator, object_mode, selected_meshes, unique_meshes
from .checks import select_elements
from .issues import recheck


# --- Helpers ---

def _recheck_mesh(context):
    if context.window_manager.preflight.mesh_checked:
        recheck(context, ('MESH',))


def _pack_selection(context):
    """Edit Mode packs the user's selection; Object Mode packs every island in the 0-1 tile."""
    return "KEEP" if context.mode == 'EDIT_MESH' else "IN_TILE"


def _some_unselected(context):
    """True in Edit Mode when some visible faces are unselected (pack around them)."""
    if context.mode != 'EDIT_MESH':
        return False
    sync = context.scene.tool_settings.use_uv_select_sync
    for obj in context.objects_in_mode_unique_data:
        if obj.type != 'MESH':
            continue
        bm = bmesh.from_edit_mesh(obj.data)
        for face in bm.faces:
            if face.hide:
                continue
            if not (face.select if sync else (face.select and face.uv_select)):
                return True
    return False


def start_interactive_pack(context, objects, select, to_others, after):
    """Start UVPackmaster's interactive pack, as its own Pack button does.

    When it finishes, restores the user's mode and calls `after(context)`, or shows the
    error in a popup. Returns False when an interactive pack isn't available.
    """
    mode = context.object.mode if context.object else 'OBJECT'
    window = context.window

    def done(error):
        override = {"window": window} if window is not None and window.screen else {}
        with bpy.context.temp_override(**override):
            if mode == 'EDIT' and bpy.context.object and bpy.context.object.mode != 'EDIT':
                bpy.ops.object.mode_set(mode='EDIT')
            if error:
                bpy.context.window_manager.popup_menu(
                    lambda menu, _c: menu.layout.label(text=error), title="Pack", icon='ERROR')
            else:
                after(bpy.context)
    return integrations.pack_interactive(context, objects, done, select, to_others)


def _island_densities(context, obj):
    """Return (face_island, island ids, densities in px/m, world areas) for obj's 0-1 islands."""
    s = context.scene.preflight
    me = obj.data
    uv_split, _, _ = edge_analysis(me)
    islands = uv_islands(me, uv_split)
    tri_uv, tri_face = triangle_uvs(me)
    scale = context.scene.unit_settings.scale_length or 1.0
    texel_size = mapsize.texel_size(mapsize.of_object(s, obj))
    ids, density, world = texel.island_densities(obj, me, tri_uv, tri_face, islands,
                                                 texel_size, scale)
    return islands, ids, density, world


# --- Packing and stacking ---

class PREFLIGHT_OT_pack_uvs(MeshOperator, bpy.types.Operator):
    """Pack UV islands with the chosen packer. In Edit Mode: the selected islands, as
UVPackmaster's own button does. In Object Mode: the selected meshes' 0-1 islands
(copies offset +1U for baking stay where they are)"""
    bl_idname = "preflight.pack_uvs"
    bl_label = "Pack"

    @classmethod
    def description(cls, context, props):
        engine = integrations.pack_engine(context)
        what = ("the selected UV islands" if context.mode == 'EDIT_MESH'
                else "the selected meshes' 0-1 islands")
        return f"Pack {what} with {integrations.ENGINE_NAMES.get(engine, 'Blender')}"

    def invoke(self, context, event):
        """Run UVPackmaster interactively when available (progress, Esc to stop)."""
        objects = [o for o in selected_meshes(context) if o.data.uv_layers]
        if not objects:
            self.report({'WARNING'}, "No selected mesh has UVs")
            return {'CANCELLED'}
        try:
            started = start_interactive_pack(context, objects, _pack_selection(context),
                                             _some_unselected(context), _recheck_mesh)
        except RuntimeError as ex:
            self.report({'ERROR'}, str(ex))
            return {'CANCELLED'}
        if started:
            self.report({'INFO'}, "Packing with UVPackmaster: Esc stops it, any key closes "
                                  "its summary")
            return {'FINISHED'}
        return self.execute(context)

    def execute(self, context):
        objects = [o for o in selected_meshes(context) if o.data.uv_layers]
        if not objects:
            self.report({'WARNING'}, "No selected mesh has UVs")
            return {'CANCELLED'}
        select = _pack_selection(context)
        to_others = _some_unselected(context)
        try:
            with object_mode(context):
                engine = integrations.pack(context, objects, select, to_others)
        except RuntimeError as ex:
            self.report({'ERROR'}, str(ex))
            return {'CANCELLED'}
        _recheck_mesh(context)
        if to_others and engine != 'UVPM':
            self.report({'WARNING'}, f"Packed the selection with "
                        f"{integrations.ENGINE_NAMES[engine]}, which ignores unselected islands: "
                        "it may land on them (UVPackmaster packs around them)")
        else:
            self.report({'INFO'}, f"Packed {len(objects)} object(s) with "
                                  f"{integrations.ENGINE_NAMES[engine]}"
                                  + (" around the unselected islands" if to_others else ""))
        return {'FINISHED'}


class PREFLIGHT_OT_stack_similar(MeshOperator, bpy.types.Operator):
    """Stack similar UV islands of the selected meshes on top of each other
(UVPackmaster's Align Similar or Zen UV's Stack Similar)"""
    bl_idname = "preflight.stack_similar"
    bl_label = "Stack Similar"

    @classmethod
    def poll(cls, context):
        return super().poll(context) and bool(integrations.stack_engines(context))

    @classmethod
    def description(cls, context, props):
        engines = integrations.stack_engines(context)
        if not engines:
            return "Needs UVPackmaster or Zen UV (enabled in Pre-Flight's preferences)"
        return ("Stack similar UV islands of the selected meshes on top of each other with "
                f"{integrations.ENGINE_NAMES[engines[0]]}")

    def execute(self, context):
        objects = [o for o in selected_meshes(context) if o.data.uv_layers]
        if not objects:
            self.report({'WARNING'}, "No selected mesh has UVs")
            return {'CANCELLED'}
        try:
            with object_mode(context):
                engine = integrations.stack(context, objects)
        except RuntimeError as ex:
            self.report({'ERROR'}, str(ex))
            return {'CANCELLED'}
        _recheck_mesh(context)
        self.report({'INFO'}, f"Stacked similar islands with {integrations.ENGINE_NAMES[engine]}")
        return {'FINISHED'}


class PREFLIGHT_OT_select_stacked(MeshOperator, bpy.types.Operator):
    """Select the stacked UV islands of the selected meshes in Edit Mode"""
    bl_idname = "preflight.select_stacked"
    bl_label = "Select Stacked"

    copies_only: BoolProperty(
        name="Copies Only",
        description="Only the copies Offset for Bake would move (one per stack stays)",
        default=False,
    )

    @classmethod
    def description(cls, context, props):
        if props.copies_only:
            return ("Select the stacked copies Offset for Bake would move (one per stack stays "
                    "in place)")
        return "Select every UV island that's stacked on another, in Edit Mode"

    def execute(self, context):
        objects = [o for o in selected_meshes(context) if o.data.uv_layers]
        if not objects:
            self.report({'WARNING'}, "No selected mesh has UVs")
            return {'CANCELLED'}
        kind = 'STACKED_COPIES' if self.copies_only else 'STACKED'
        data = "\n".join(o.name for o in objects)  # stacks can span objects sharing a texture
        total = select_elements(context, objects, kind, context.scene.preflight, data)
        what = "stacked copies" if self.copies_only else "stacked faces"
        self.report({'INFO'} if total else {'WARNING'},
                    f"Selected {total} {what}" if total else "No stacked islands found")
        return {'FINISHED'}


# --- Texel density ---

class PREFLIGHT_OT_texel_pick(bpy.types.Operator):
    """Use the active object's texel density (its area-weighted median) as the custom target"""
    bl_idname = "preflight.texel_pick"
    bl_label = "Pick from Active"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH' and bool(obj.data.uv_layers)

    def execute(self, context):
        obj = context.active_object
        with object_mode(context):
            _, ids, density, world = _island_densities(context, obj)
        if not len(ids):
            self.report({'WARNING'}, f"{obj.name} has no UV islands in the 0-1 tile")
            return {'CANCELLED'}
        s = context.scene.preflight
        s.texel_target_display = float(texel.fmt(texel.weighted_median(density, world),
                                                 s.texel_unit, label=False))
        s.texel_target_mode = 'CUSTOM'
        self.report({'INFO'}, f"Target texel density: {texel.fmt(s.texel_target, s.texel_unit)}")
        return {'FINISHED'}


class PREFLIGHT_OT_texel_set(MeshOperator, bpy.types.Operator):
    """Scale every 0-1 UV island of the selected meshes to the target texel density
(Zen UV's Set Texel Density when installed). Repack afterwards"""
    bl_idname = "preflight.texel_set"
    bl_label = "Set to Target"

    def execute(self, context):
        s = context.scene.preflight
        objects = [o for o in selected_meshes(context) if o.data.uv_layers]
        if not objects:
            self.report({'WARNING'}, "No selected mesh has UVs")
            return {'CANCELLED'}
        with object_mode(context):
            per_object = {obj: _island_densities(context, obj) for obj in objects}
            targets = {}
            if s.texel_target_mode == 'SET':  # each texture set's own median
                sets = {}
                for obj, (_, ids, density, world) in per_object.items():
                    key = obj.active_material.name if obj.active_material else obj.name
                    sets.setdefault(key, []).append((density, world))
                for key, entries in sets.items():
                    density = np.concatenate([e[0] for e in entries])
                    world = np.concatenate([e[1] for e in entries])
                    targets[key] = texel.weighted_median(density, world) if len(density) else None
            moved = 0
            for obj, (islands, ids, density, _) in per_object.items():
                key = obj.active_material.name if obj.active_material else obj.name
                target = s.texel_target if s.texel_target_mode == 'CUSTOM' else targets.get(key)
                if not len(ids) or not target:
                    continue
                off = np.abs(density / target - 1) > 1e-3
                if off.any():
                    try:
                        integrations.set_island_density(context, obj, islands, ids[off],
                                                        density[off], target)
                    except RuntimeError as ex:
                        self.report({'ERROR'}, str(ex))
                        return {'CANCELLED'}
                    moved += int(off.sum())
        _recheck_mesh(context)
        self.report({'INFO'}, f"Scaled {moved} island(s) to the target density" if moved
                    else "Every island is already at the target")
        return {'FINISHED'}


# --- Map ratio and baker presets ---

class PREFLIGHT_OT_match_uv_ratio(MeshOperator, bpy.types.Operator):
    """Stretch each UV island of the selected meshes to its map's width / height, so
texels stay square on a non-square map (or back, for a square one). Choose which way
to stretch. Doesn't pack: Pack afterwards if islands overlap or leave 0-1"""
    bl_idname = "preflight.match_uv_ratio"
    bl_label = "Match UVs to Map Ratio"

    axis: EnumProperty(name="Stretch", items=mapsize.STRETCH_AXES, default='BOTH')
    drawn_square: BoolProperty(
        name="UVs Unwrapped Again",
        description="Treat the UVs as drawn for a square map (stretch from square). "
                    "Usually not needed: re-unwrapped islands are detected by their shapes",
        default=False, options={'SKIP_SAVE'})

    def invoke(self, context, event):
        # Summarise each mesh's state once here; draw() runs on every dialog redraw.
        s = context.scene.preflight
        axis_names = {k: n for k, n, _ in mapsize.STRETCH_AXES}
        lines = set()
        for obj, me in unique_meshes(selected_meshes(context)):
            if not me.uv_layers:
                continue
            if me.is_editmode:
                obj.update_from_editmode()
            ms = mapsize.of_object(s, obj)
            again = mapsize.redrawn(me)
            drawn = 1.0 if again else mapsize.recorded_ratio(me)
            how = (f" ({axis_names.get(mapsize.uv_axis(me), '')})"
                   if abs(drawn - 1) > 1e-6 else "")
            if mapsize.ratio_matches(me, ms):
                lines.add((f"Matched to {mapsize.label(ms)}{how}", 'CHECKMARK'))
            else:
                lines.add((f"Drawn for {ratio_text(drawn)}{how}  →  {mapsize.label(ms)} map"
                           + (" (unwrapped again)" if again else ""), 'FULLSCREEN_ENTER'))
        self._lines = sorted(lines)
        return context.window_manager.invoke_props_dialog(self, width=320,
                                                          confirm_text="Match")

    def draw(self, context):
        layout = self.layout
        for text, icon in getattr(self, "_lines", [])[:4]:
            layout.label(text=text, icon=icon)
        layout.prop(self, "axis", expand=True)
        layout.prop(self, "drawn_square")

    def execute(self, context):
        s = context.scene.preflight
        objects = [o for o in selected_meshes(context) if o.data.uv_layers]
        if not objects:
            self.report({'WARNING'}, "No selected mesh has UVs")
            return {'CANCELLED'}
        # Stay in Edit Mode if already there: a mode switch would make this a slow
        # full-scene undo step.
        changed = 0
        for obj, me in unique_meshes(objects):
            if me.is_editmode:
                obj.update_from_editmode()
            changed += mapsize.match_uv_ratio(me, mapsize.aspect(mapsize.of_object(s, obj)),
                                              self.axis, self.drawn_square)
        issue_store.forget(objects)
        if changed:
            issue_store.mark_stale(context)  # flag the Issues list as out of date, no re-check
            self.report({'INFO'}, f"Stretched UVs on {changed} mesh(es): Pack if islands "
                                  "overlap or leave 0-1")
        else:
            self.report({'INFO'}, "UVs already match their maps' ratio this way")
        return {'FINISHED'}


class PREFLIGHT_OT_apply_baker(bpy.types.Operator):
    """Apply the chosen baker's prep settings again (after you've changed some)"""
    bl_idname = "preflight.apply_baker"
    bl_label = "Apply Baker Preset"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene.preflight.baker != 'CUSTOM'

    def execute(self, context):
        s = context.scene.preflight
        changed = bakers.apply(s, s.baker)
        self.report({'INFO'}, f"{bakers.BAKERS[s.baker]['name']}: "
                    + (f"set {len(changed)} setting(s)" if changed else "already set"))
        return {'FINISHED'}


classes = (PREFLIGHT_OT_pack_uvs, PREFLIGHT_OT_stack_similar, PREFLIGHT_OT_select_stacked,
           PREFLIGHT_OT_texel_pick, PREFLIGHT_OT_texel_set, PREFLIGHT_OT_match_uv_ratio,
           PREFLIGHT_OT_apply_baker)
