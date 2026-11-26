# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""The Pre-Flight sidebar panel.

One panel with a tab strip on the left; the chosen tab's page is drawn beside it.
Each page is a stack of full-width collapsible boxes whose controls sit in a grid
of two columns, or one when the sidebar is narrow.
"""

import math
import textwrap

import bpy
from bpy.props import BoolProperty

from .. import integrations
from .. import issues as issue_store
from ..constants import UV_OFFSET_KINDS
from ..core import mapsize
from ..core.checks import TEXTURE_SET_PREFIX
from ..core.problems import ELEMENT_KINDS, ISSUE_ONLY
from ..operators.issues import FIXES
from ..utils import selected_meshes
from . import icons

TWO_COLUMN_WIDTH = 300  # sidebar width (at UI scale 1) from which grids use two columns
MECHANISM_ROWS = 24     # most rows the Rigging tab's list of mechanisms draws

# Baker preset settings -> labels for the "Pre-Flight Sets" list.
BAKER_SETTING_LABELS = {
    'soft_mirror_edges': "Soft edges on mirror lines",
    'triangulate_in_full': "Triangulate in Full Prep",
    'projection_average': "Projection check from averaged normals",
}

# Note under the Packing box for packers without their own settings here.
ENGINE_HINTS = {
    'UVPM': "UVPackmaster, with its panel settings",
    'ZEN': "Zen UV, with its pack settings",
    'BLENDER': "Blender's Pack Islands",
}

# Shorter button labels for the Select Problems grid (tooltips keep the full text).
SHORT_LABELS = {
    'HARD_NO_SPLIT': "Hard, No UV Split",
    'CORNER_NO_SPLIT': "Corners, No UV Split",
    'MIRROR_HARD': "Hard on Mirror Line",
    'ZERO_UV': "Zero UV Area",
    'OUTSIDE_UV': "Outside 0-1",
    'NGON': "N-gons",
    'NOT_TRIANGULATED': "Not Triangulated",
    'SKEW': "Skew Risk",
    'FLIPPED': "Flipped Faces",
    'NON_MANIFOLD': "Non-manifold",
    'DOUBLES': "Doubles",
    'ZERO_AREA': "Zero Area Faces",
    'LOOSE_VERTS': "Loose Vertices",
    'LOOSE_EDGES': "Loose Edges",
}


class PreflightPanel:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Pre-Flight'


# --- Layout helpers ---

def is_wide(context):
    return context.region.width >= TWO_COLUMN_WIDTH * context.preferences.system.ui_scale


class _Closed:
    """Stand-in layout for a collapsed section.

    Every attribute and call returns the object itself, so drawing code runs
    unchanged and draws nothing.
    """

    def __getattr__(self, name):
        return self

    def __call__(self, *args, **kwargs):
        return self

    def __setattr__(self, name, value):
        pass

    def __bool__(self):
        return False


_CLOSED = _Closed()


def section_id(key):
    return "".join(c if c.isalnum() else "_" for c in key.lower())


def is_closed(context, sid):
    return sid in context.scene.preflight.closed_sections.split(",")


def collapse_toggle(layout, context, sid, text="", **kw):
    """Draw the arrow (and optional title) that opens or closes section `sid`.

    Returns True if the section is closed.
    """
    closed = is_closed(context, sid)
    layout.operator("preflight.toggle_section", text=text, emboss=False,
                    icon='DISCLOSURE_TRI_RIGHT' if closed else 'DISCLOSURE_TRI_DOWN',
                    **kw).key = sid
    return closed


def section(layout, label, accent, key=None, context=None, icon=None):
    """A full-width collapsible box titled with an arrow, accent pip and dimmed label.

    The open/closed state is saved in scene.preflight.closed_sections under `key`
    (or the label). Returns the column to fill, or a no-op stand-in when collapsed.
    Plain rows are used instead of layout panels, whose header background would
    also span the tab strip.
    """
    context = context or bpy.context
    sid = section_id(key or label)
    box = layout.box()
    title = box.row(align=True)
    title.scale_y = 0.8
    closed = collapse_toggle(title, context, sid)
    pip = title.row(align=True)
    pip.ui_units_x = 1.0
    pip.label(icon_value=icons.icon(f"pip_{accent}"))
    text = title.row(align=True)
    text.active = False
    text.alignment = 'LEFT'
    text.operator("preflight.toggle_section", text=label, emboss=False,
                  **({"icon": icon} if icon else {})).key = sid
    if closed:
        return _CLOSED
    return box.column()


class PREFLIGHT_OT_toggle_section(bpy.types.Operator):
    """Open or close this section"""
    bl_idname = "preflight.toggle_section"
    bl_label = "Toggle Section"
    bl_options = {'INTERNAL'}

    key: bpy.props.StringProperty()

    def execute(self, context):
        s = context.scene.preflight
        closed = [k for k in s.closed_sections.split(",") if k]
        if self.key in closed:
            closed.remove(self.key)
        else:
            closed.append(self.key)
        s.closed_sections = ",".join(closed)
        return {'FINISHED'}


def strip_icon(context, tab):
    """icon_value of the pictogram shown for `tab` in the strip and page title."""
    from ..properties import tab_icon
    return tab_icon(context.window_manager.preflight, tab)


class Page:
    """One tab's page, drawn beside the tab strip. Subclasses set bl_label and draw()."""
    bl_label = ""

    def __init__(self, layout):
        self.layout = layout

    @staticmethod
    def draw_title_extra(layout, context):
        pass


def grid(layout, context, align=True):
    """Two equal columns filled row by row; one column when the sidebar is narrow."""
    return layout.grid_flow(row_major=True, columns=2 if is_wide(context) else 1,
                            even_columns=True, even_rows=True, align=align)


def labelled(layout, data, prop, label):
    """A label and a value on one line, for dropdowns that would otherwise truncate."""
    row = layout.split(factor=0.42, align=True)
    row.label(text=label)
    row.prop(data, prop, text="")


def hint(layout, text):
    row = layout.row()
    row.active = False
    row.label(text=text)


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 'es' if word.endswith('h') else 's'}"


def _select_icon(kind):
    return {'VERT': 'VERTEXSEL', 'EDGE': 'EDGESEL', 'FACE': 'FACESEL'}[
        ELEMENT_KINDS.get(kind, ('EDGE',))[0]]


def _wrapped(layout, context, text, icon='NONE'):
    """Draw `text` as labels wrapped to the sidebar width."""
    ui_scale = context.preferences.system.ui_scale
    width = max(20, int((context.region.width - 30) / (8 * ui_scale)))
    lines = textwrap.wrap(text, width) or [""]
    col = layout.column(align=True)
    col.scale_y = 0.85
    for i, line in enumerate(lines):
        col.label(text=line, icon=icon if i == 0 else 'BLANK1' if icon != 'NONE' else 'NONE')


# --- Main panel ---

class VIEW3D_PT_preflight(PreflightPanel, bpy.types.Panel):
    bl_label = "Pre-Flight"
    bl_idname = "VIEW3D_PT_preflight"

    def draw(self, context):
        wm = context.window_manager.preflight
        row = self.layout.row()
        strip = row.column(align=True)
        strip.ui_units_x = 1.6
        strip.scale_y = 1.6
        strip.prop(wm, "tab", expand=True, icon_only=True)
        page = row.column()
        self.draw_top(page, context)

        cls = PAGES.get(wm.tab, VIEW3D_PT_preflight_pairs)
        page.separator(factor=0.5)
        title = page.box().row(align=True)
        title.alignment = 'CENTER'
        title.label(text=cls.bl_label, icon_value=strip_icon(context, wm.tab))
        cls.draw_title_extra(title, context)
        cls(page.column()).draw(context)

    def draw_top(self, layout, context):
        """Selection summary, shown on every tab."""
        s = context.scene.preflight
        box = layout.box()
        g = grid(box, context, align=False)
        count = len(selected_meshes(context))
        g.label(text=f"{plural(count, 'mesh')} selected", icon='RESTRICT_SELECT_OFF')
        if s.low_collection and s.high_collection:
            g.label(text=f"Low {len(s.low_collection.all_objects)} · "
                         f"High {len(s.high_collection.all_objects)}",
                    icon='OUTLINER_COLLECTION')
        else:
            g.label(text="No Low / High set", icon='OUTLINER_COLLECTION')


def _size_fields(row, owner):
    row.prop(owner, "texture_size", text="")
    row.prop(owner, "texture_square", text="",
             icon='LINKED' if owner.texture_square else 'UNLINKED')
    if not owner.texture_square:
        row.prop(owner, "texture_height", text="")


def draw_map_size(layout, s):
    """The scene's map size, used by every material without its own."""
    row = layout.row(align=True)
    sub = row.row(align=True)
    sub.ui_units_x = 3.2
    sub.label(text="Maps", icon='TEXTURE')
    _size_fields(row, s)


def draw_material_size(layout, context):
    """The active material's (texture set's) own map size toggle and fields."""
    obj = context.active_object
    mat = obj.active_material if obj is not None and obj.type == 'MESH' else None
    if mat is None or not hasattr(mat, "preflight"):
        return
    own = mat.preflight
    row = layout.row(align=True)
    sub = row.row(align=True)
    sub.ui_units_x = 3.2
    sub.label(text=mat.name, icon='MATERIAL')
    row.prop(own, "use_own_size", text="" if own.use_own_size else "Own Size",
             toggle=True, icon='DECORATE_OVERRIDE' if own.use_own_size else 'NONE')
    if own.use_own_size:
        _size_fields(row, own)


# --- Pages ---

class VIEW3D_PT_preflight_shading(Page):
    bl_label = "Shading"

    def draw(self, context):
        s = context.scene.preflight
        layout = self.layout

        body = section(layout, "Hard Edges from UVs", "shading")
        g = grid(body, context)
        g.prop(s, "edge_source", expand=True)
        g = grid(body, context, align=False)
        if s.edge_source == 'ISLANDS':
            g.prop(s, "sync_seams")
        g.prop(s, "soft_mirror_edges")
        g.prop(s, "clear_custom_normals")
        g.prop(s, "strip_auto_smooth")
        g = grid(body, context)
        g.prop(s, "min_angle")
        g.operator("preflight.hard_edges", text="Apply Hard Edges", icon='EDGESEL')

        body = section(layout, "Triangulate", "shading")
        g = grid(body, context, align=False)
        labelled(g, s, "quad_method", "Quads")
        labelled(g, s, "ngon_method", "N-gons")
        if "keep_custom_normals" in bpy.types.TriangulateModifier.bl_rna.properties:
            g.prop(s, "keep_normals")
        g.prop(s, "triangulate_in_full", text="In Full Prep")
        g = grid(body, context)
        g.operator("preflight.triangulate", text="Add", icon='MOD_TRIANGULATE')
        g.operator("preflight.untriangulate", text="Remove", icon='X')

        body = section(layout, "Custom Normals", "shading")
        g = grid(body, context)
        hint(g, "Override sharp edges")
        g.operator("preflight.clear_custom_normals", text="Clear", icon='X')

        body = section(layout, "Textures", "shading")
        draw_map_size(body, s)
        draw_material_size(body, context)
        body.prop(s, "texture_folder", text="")
        g = grid(body, context)
        g.operator("preflight.load_textures", text="Load", icon='IMPORT')
        g.operator("preflight.reload_textures", text="Reload", icon='FILE_REFRESH')
        g = grid(body, context)
        g.operator("preflight.flip_normal_green", text="Flip Normal G",
                   icon='ORIENTATION_NORMAL')
        g.prop(context.window_manager.preflight, "auto_reload_textures")


class VIEW3D_PT_preflight_uvs(Page):
    bl_label = "UVs"

    def draw(self, context):
        s = context.scene.preflight
        ms = mapsize.of_object(s, context.active_object)
        wm = context.window_manager.preflight
        layout = self.layout

        body = section(layout, "Mirror / Array Copies", "uvs")
        g = grid(body, context)
        g.prop(wm, "overlay_mirror", text="Mirror Overlay", toggle=True, icon='MOD_MIRROR')
        g.prop(wm, "overlay_array", text="Array Overlay", toggle=True, icon='MOD_ARRAY')
        for kind, name in UV_OFFSET_KINDS.items():
            g.operator("preflight.set_uv_offset", text=f"{name} +1U",
                       icon='MOD_MIRROR' if kind == 'MIRROR' else 'MOD_ARRAY').kind = kind
        if wm.overlay_mirror or wm.overlay_array:
            g = grid(body, context, align=False)
            legend = g.row()
            legend.active = False
            legend.label(text="Offset", icon='CHECKMARK')
            legend = g.row()
            legend.active = False
            legend.label(text="Overlapping", icon='ERROR')

        body = section(layout, "Packing", "uvs")
        engine = integrations.pack_engine(context)
        g = grid(body, context)
        g.prop(s, "pack_engine", text="")
        g.operator("preflight.pack_uvs", text="Pack Selected", icon='PACKAGE')
        g = grid(body, context, align=False)
        row = g.row(align=True)
        if s.padding_auto:
            sub = row.row(align=True)
            sub.enabled = False
            sub.label(text=f"Padding {mapsize.padding(ms)}px")
        else:
            row.prop(s, "padding")
            row.operator("preflight.suggest_padding", text="", icon='FILE_REFRESH')
        row.prop(s, "padding_auto", toggle=True)
        g.prop(s, "pack_use_padding")
        g = grid(body, context)
        g.operator("preflight.match_uv_ratio", text="Match Map Ratio",
                   icon='FULLSCREEN_ENTER')
        if not mapsize.is_square(ms):
            hint(g, "Non-square: 180° turns only")
        if engine == 'UVPM':
            g = grid(body, context, align=False)
            g.prop(s, "pack_keep_stacks")
            draw_uvpm_pack(body, context, s)
        elif engine == 'ZEN':
            draw_zen_pack(body, context, s)
        else:
            hint(body, ENGINE_HINTS.get(engine, ""))

        body = section(layout, "Stacking", "uvs")
        engines = integrations.stack_engines(context)
        g = grid(body, context)
        g.prop(s, "stack_engine", text="")
        g.operator("preflight.stack_similar", icon='DUPLICATE')
        if engines and engines[0] == 'UVPM':
            draw_uvpm_stack(body, context)
        elif engines and engines[0] == 'ZEN':
            draw_zen_stack(body, context)
        g = grid(body, context)
        g.operator("preflight.select_stacked", text="Select Stacked",
                   icon='RESTRICT_SELECT_OFF').copies_only = False
        g.operator("preflight.select_stacked", text="Select Copies",
                   icon='RESTRICT_SELECT_OFF').copies_only = True
        g.operator("preflight.offset_stacked_uvs", text="Offset for Bake", icon='UV_ISLANDSEL')
        g.operator("preflight.restore_stacked_uvs", text="Restore", icon='LOOP_BACK')
        if not engines:
            hint(body, "Stack Similar needs UVPackmaster or Zen UV")

        body = section(layout, "Texel Density", "uvs")
        g = grid(body, context)
        g.prop(wm, "overlay_texel", text="Density Overlay", toggle=True, icon='TEXTURE')
        g.row(align=True).prop(s, "texel_target_mode", expand=True)
        from ..core.texel import UNITS, fmt
        g = grid(body, context)
        hint(g, f"At {mapsize.label(mapsize.of_object(s, context.active_object))} maps")
        g.prop(s, "texel_unit", text="")
        if s.texel_target_mode == 'CUSTOM':
            g.prop(s, "texel_target_display", text=UNITS[s.texel_unit][0])
        g.prop(s, "texel_tolerance", text="Tolerance")
        g = grid(body, context)
        g.operator("preflight.texel_pick", icon='EYEDROPPER')
        g.operator("preflight.texel_set", icon='UV_DATA')
        if wm.overlay_texel:
            legend = body.row()
            for name, text in (("info", "Low"), ("ok", "On Target"), ("error", "High")):
                key = legend.row()
                key.active = False
                key.label(text=text, icon_value=icons.icon(f"dot_{name}"))
            from ..texel_overlay import stats
            obj = context.active_object
            info = stats(obj.name) if obj else None
            if info and info[0] and info[1]:
                hint(body, f"{obj.name}: {fmt(info[0], s.texel_unit)}, target "
                           f"{fmt(info[1], s.texel_unit, label=False)} "
                           f"({info[0] / info[1] - 1:+.0%})")
            elif obj is not None and obj.mode == 'EDIT':
                hint(body, "Leave Edit Mode to update the overlay")


class VIEW3D_PT_preflight_pairs(Page):
    bl_label = "Low / High"

    def draw(self, context):
        s = context.scene.preflight
        layout = self.layout

        body = section(layout, "Collections", "pairs")
        g = grid(body, context)
        g.prop(s, "low_collection", text="", icon='MESH_CUBE')
        g.prop(s, "high_collection", text="", icon='MESH_MONKEY')

        body = section(layout, "Make Pairs", "pairs")
        hint(body, "Duplicate selected in place as:")
        g = grid(body, context)
        g.operator("preflight.duplicate_pair", text="High → Low",
                   icon='DUPLICATE').direction = 'HIGH'
        g.operator("preflight.duplicate_pair", text="Low → High",
                   icon='DUPLICATE').direction = 'LOW'
        g = grid(body, context)
        g.operator("preflight.pair_selected", text="Pair Selected", icon='LINKED')
        g.operator("preflight.separate_cutters", text="Separate Cutters", icon='MOD_BOOLEAN')

        body = section(layout, "Names", "pairs")
        body.prop(s, "suffix_style")
        g = grid(body, context)
        g.operator("preflight.match_names", text="Match & Rename", icon='SORTALPHA')
        g.operator("preflight.check_names", text="Check Names", icon='CHECKMARK')
        body.operator("preflight.sync_data_names", text="Sync Mesh Names", icon='MESH_DATA')

        body = section(layout, "Explode", "pairs")
        g = grid(body, context, align=False)
        g.row(align=True).prop(s, "explode_axis", expand=True)
        g.prop(s, "explode_spacing")
        g.prop(s, "explode_split_shared")
        row = g.row(align=True)
        row.operator("preflight.explode", text="Explode")
        row.operator("preflight.unexplode", text="Restore")



class VIEW3D_PT_preflight_baker(Page):
    bl_label = "Baker"

    def draw(self, context):
        from ..core import bakers
        s = context.scene.preflight
        layout = self.layout

        body = section(layout, "Preset", "baker")
        g = grid(body, context)
        g.prop(s, "baker", text="")
        g.operator("preflight.apply_baker", text="Apply Again", icon='FILE_REFRESH')
        if s.baker == 'CUSTOM':
            hint(body, "Pick a baker to set prep to suit it")
        else:
            body = section(layout, "Pre-Flight Sets", "baker")
            for attr, value in bakers.settings_for(s.baker).items():
                ok = getattr(s, attr, None) == value
                row = body.row()
                row.active = ok
                row.label(text=BAKER_SETTING_LABELS.get(attr, attr),
                          icon='CHECKMARK' if ok else 'ERROR')

            body = section(layout, f"In {bakers.BAKERS[s.baker]['name']}", "baker",
                           key="baker_notes")
            from ..core.naming import style_label
            for note in bakers.notes(s.baker, style_label(s.suffix_style)):
                _wrapped(body, context, note, icon='DOT')

        body = section(layout, "ID Colors", "baker")
        hint(body, "Distinct colours on the highs for a Color ID bake")
        g = grid(body, context)
        op = g.operator("preflight.assign_id_colors", text="Per Object", icon='COLOR')
        op.mode = 'OBJECT'
        op = g.operator("preflight.assign_id_colors", text="Per Part", icon='COLOR')
        op.mode = 'PART'
        g = grid(body, context)
        op = g.operator("preflight.assign_id_colors", text="Per Material", icon='MATERIAL')
        op.mode = 'MATERIAL'
        g.operator("preflight.clear_id_colors", text="Clear", icon='X')


class VIEW3D_PT_preflight_export(Page):
    bl_label = "Export"

    def draw(self, context):
        from ..core import bakers, exporter
        s = context.scene.preflight
        layout = self.layout

        body = section(layout, "Files", "export", icon='FILE_FOLDER')
        body.prop(s, "export_folder", text="", placeholder="Export folder")
        body.prop(s, "export_name", text="", icon='FILE', placeholder="File name")
        name = exporter.asset_name(context, s.export_name)
        hint(body, "Empty: an 'export' folder beside the .blend, named after it")

        body = section(layout, "For the Baker", "export", icon='EXPORT')
        baker = bakers.BAKERS[s.baker]["name"] if s.baker in bakers.BAKERS else "Custom"
        hint(body, f"Baker: {baker} (set on the Baker tab)")
        g = grid(body, context, align=False)
        labelled(g, s, "bake_files", "Files")
        labelled(g, s, "normal_format", "Normals")
        g.prop(s, "bake_export_cages")
        body.operator("preflight.export_bake", icon='EXPORT')
        files = exporter.baker_files(s.baker, s.bake_files)
        if files is None:
            hint(body, "This baker works inside Blender: no files needed")
        else:
            hint(body, f"{name}_bake.fbx" if files == 'SINGLE'
                 else f"{name}_low.fbx + {name}_high.fbx")

        body = section(layout, "Cage & Ray Distance", "export", icon='MOD_SOLIDIFY')
        hint(body, "From what the Projection check measures")
        g = grid(body, context)
        g.prop(s, "cage_margin")
        g.operator("preflight.make_cages", icon='MOD_SOLIDIFY')
        body.operator("preflight.set_ray_distance", icon='LIGHT_SUN')
        hint(body, "Set Bake Distances is for baking in Blender")

        body = section(layout, "For the Engine", "export", icon='EXPORT')
        hint(body, "Selected lows, or Low collection")
        g = grid(body, context, align=False)
        labelled(g, s, "engine_target", "Engine")
        labelled(g, s, "export_combine", "Combine")
        labelled(g, s, "export_collision", "Collision")
        g.prop(s, "engine_animation")
        g = grid(body, context)
        g.operator("preflight.engine_copy", icon='DUPLICATE')
        g.operator("preflight.export_engine", icon='EXPORT')
        hint(body, f"{name}{'.glb' if s.engine_target == 'GODOT' else '.fbx'}"
                   ": the engine mesh, its rig and animation")
        hint(body, "Make Engine Mesh only makes the copy, to look at")
        hint(body, "Bake setup is left as it is")


class VIEW3D_PT_preflight_rigging(Page):
    bl_label = "Rigging"

    def draw(self, context):
        s = context.scene.preflight
        wm = context.window_manager.preflight
        layout = self.layout

        layout.row(align=True).prop(wm, "rig_page", expand=True)
        body = section(layout, "Rig", "rigging")
        g = grid(body, context, align=False)
        g.prop(s, "rig_use_bones")
        sub = g.row()
        sub.active = s.rig_use_bones
        sub.prop(s, "rig_armature", text="", icon='ARMATURE_DATA')
        if not s.rig_use_bones:
            hint(body, "No armature: for Blender, not game engines")
        elif s.rig_armature is None:
            hint(body, "Bones from Origins makes one")

        if wm.rig_page == 'BONES':
            self.draw_bones(context, layout, s)
        elif wm.rig_page == 'MECHANISMS':
            self.draw_mechanisms(context, layout, s)
        else:
            self.draw_finish(context, layout, s)

    def draw_bones(self, context, layout, s):
        body = section(layout, "Mirror Parts", "rigging")
        g = grid(body, context, align=False)
        g.row(align=True).prop(s, "rig_mirror_axis", expand=True)
        g.prop(s, "rig_mirror_pivot", text="")
        g.prop(s, "rig_name_sides")
        g.operator("preflight.mirror_parts", text="Mirror Parts", icon='MOD_MIRROR')

        body = section(layout, "Bones from Origins", "rigging")
        if not s.rig_use_bones:
            hint(body, "Use Bones is off: parts keep their own parenting")
        g = grid(body, context, align=False)
        g.active = s.rig_use_bones
        labelled(g, s, "rig_bone_axis", "Axis")
        g.prop(s, "rig_bone_length")
        g.prop(s, "rig_root")
        g.prop(s, "rig_aim_children")
        col = body.column()
        col.active = s.rig_use_bones
        labelled(col, s, "rig_bind", "Bind")
        col.operator("preflight.bones_from_origins", icon='BONE_DATA')

        body = section(layout, "Hinges", "rigging")
        hint(body, "Edit Mode: select the axle or face")
        g = grid(body, context)
        g.operator("preflight.bone_from_hinge", icon='BONE_DATA')
        g.operator("preflight.origin_to_hinge", icon='OBJECT_ORIGIN')
        g = grid(body, context, align=False)
        g.row(align=True).prop(s, "rig_limit_axis", expand=True)
        g.prop(s, "rig_lock_location")
        for prop, which in (("rig_limit_min", 'MIN'), ("rig_limit_max", 'MAX')):
            row = g.row(align=True)
            row.prop(s, prop)
            row.operator("preflight.limit_from_pose", text="", icon='EYEDROPPER').which = which
        hint(body, "Eyedropper: take the angle it is turned to now")
        g = grid(body, context)
        g.operator("preflight.hinge_limits", icon='CON_ROTLIMIT')
        g.operator("preflight.test_motion", text="Test", icon='PLAY')
        hint(body, "Parts with no bone hinge about their own axis")

        body = section(layout, "IK Chain", "rigging", icon='CON_KINEMATIC')
        if not s.rig_use_bones:
            hint(body, "IK needs bones")
        g = grid(body, context, align=False)
        g.prop(s, "rig_ik_length")
        g.prop(s, "rig_ik_pole")
        body.prop(s, "rig_ik_follow", text="")
        body.operator("preflight.ik_chain", icon='CON_KINEMATIC')

    def role_hint(self, context, layout, tool, fallback):
        """What the selection would be to `tool`, or how to select for it."""
        from ..operators.rigging import roles
        found = roles(context, tool)
        if found:
            _wrapped(layout, context, found, icon='CHECKMARK')
        else:
            hint(layout, fallback)

    def draw_mechanisms(self, context, layout, s):
        body = section(layout, "Axle", "rigging", icon='ORIENTATION_LOCAL')
        g = grid(body, context, align=False)
        labelled(g, s, "rig_wheel_spin_axis", "Axle")
        g.operator("preflight.test_motion", text="Test Active", icon='PLAY')
        hint(body, "One axle setting for gears, paths and wheels")

        body = section(layout, "Pistons", "rigging", icon='CON_STRETCHTO')
        self.role_hint(context, body, 'PISTON', "Select the cylinder and the rod, rod active")
        body.operator("preflight.rig_piston", text="Piston", icon='CON_STRETCHTO')

        body = section(layout, "Gears", "rigging", icon='PREFERENCES')
        self.role_hint(context, body, 'GEARS', "Select the gears, the driving one active")
        hint(body, "Or a chain and its wheels: the active wheel drives it")
        body.row(align=True).prop(s, "rig_gear_mode", expand=True)
        body.operator("preflight.link_gears", text="Link Gears", icon='PREFERENCES')

        body = section(layout, "Chains & Tracks", "rigging", icon='LINKED')
        self.role_hint(context, body, 'PATH', "No path yet? Select the wheels it goes round")
        g = grid(body, context)
        g.prop(s, "rig_path_offset")
        g.operator("preflight.path_around_wheels", icon='CURVE_NCURVE')
        self.role_hint(context, body, 'CHAIN',
                       "Select the link (+ sprocket) and the path: curve or edge loop")
        g = grid(body, context, align=False)
        labelled(g, s, "rig_chain_axis", "Link Axis")
        row = g.row(align=True)
        row.prop(s, "rig_chain_count")
        row.operator("preflight.calculate_links", text="", icon='LINENUMBERS_ON')
        inst = g.row()
        inst.active = s.rig_use_bones
        inst.prop(s, "rig_chain_instances")
        keep = g.row()
        keep.active = s.rig_chain_instances or not s.rig_use_bones
        keep.prop(s, "rig_chain_keep_link")
        if not s.rig_use_bones:
            hint(body, "Without bones a chain is always instances")
        g = grid(body, context)
        g.operator("preflight.chain_along_curve", icon='LINKED')
        g.operator("preflight.rebuild_chain", icon='FILE_REFRESH')
        row = body.row(align=True)
        row.label(text="Roll Links")
        for angle, text, icon in ((-90, "-90°", 'LOOP_BACK'), (90, "+90°", 'LOOP_FORWARDS'),
                                  (180, "180°", 'ARROW_LEFTRIGHT')):
            row.operator("preflight.roll_links", text=text, icon=icon).angle = math.radians(angle)

        body = section(layout, "Wheels & Driving", "rigging", icon='DRIVER_ROTATIONAL_DIFFERENCE')
        self.role_hint(context, body, 'WHEELS',
                       "Select the wheels, then the vehicle body (active)")
        g = grid(body, context, align=False)
        labelled(g, s, "rig_wheel_travel_axis", "Travel")
        g.prop(s, "rig_wheel_reverse")
        body.operator("preflight.wheel_spin", icon='DRIVER_ROTATIONAL_DIFFERENCE')
        g = grid(body, context)
        g.operator("preflight.drive_control", icon='ORIENTATION_GIMBAL')
        g.operator("preflight.bake_wheel_roll", icon='KEYINGSET')

        self.draw_mechanism_list(context, layout)

    def draw_mechanism_list(self, context, layout):
        from ..core import mechanisms
        body = section(layout, "Built So Far", "rigging", icon='OUTLINER')
        if body is _CLOSED:
            return
        rows = mechanisms.find(context.scene)
        if not rows:
            hint(body, "Nothing yet: what the tools build is listed here")
            return
        col = body.column(align=True)
        for mech in rows[:MECHANISM_ROWS]:
            row = col.row(align=True)
            text = f"{mech.label}  ({mech.detail})" if mech.detail else mech.label
            left = row.row(align=True)
            left.alignment = 'LEFT'
            left.operator("preflight.mechanism_select", text=text, emboss=False,
                          icon=mechanisms.KIND_ICONS.get(mech.kind, 'DOT')
                          ).names = "|".join(mech.objects)
            right = row.row(align=True)
            right.alignment = 'RIGHT'
            right.operator("preflight.mechanism_remove", text="", icon='X',
                           emboss=False).key = mech.key
        if len(rows) > MECHANISM_ROWS:
            hint(body, f"and {len(rows) - MECHANISM_ROWS} more")
        hint(body, "Click a row to select it, X to remove it")

    def draw_finish(self, context, layout, s):
        body = section(layout, "Drivers", "rigging", icon='DRIVER')
        hint(body, "Selected objects, or selected bones in Pose Mode")
        body.operator("preflight.clear_drivers", icon='TRASH')

        body = section(layout, "Controls", "rigging", icon='GROUP_BONE')
        if not s.rig_use_bones:
            hint(body, "Controls are for rigs with bones")
        hint(body, "Whole rig: sort bones and give each control a shape")
        body.operator("preflight.setup_controls", icon='GROUP_BONE')
        hint(body, "Pose Mode: click bones, pick a shape and axis")
        g = grid(body, context, align=False)
        g.prop(s, "rig_shape", text="")
        g.prop(s, "rig_shape_size")
        body.row(align=True).prop(s, "rig_shape_axis", expand=True)
        body.prop(s, "rig_shape_line")
        g = grid(body, context)
        g.operator("preflight.control_shape", icon='MESH_CIRCLE')
        g.operator("preflight.control_shape_reset", icon='FILE_REFRESH')

        body = section(layout, "Game Ready", "rigging")
        if not s.rig_use_bones:
            hint(body, "Game engines need bones: turn Use Bones on")
        g = grid(body, context, align=False)
        g.prop(s, "rig_bake")
        g.prop(s, "rig_join")
        g = grid(body, context)
        g.operator("preflight.check_rig", icon='CHECKMARK')
        g.operator("preflight.make_game_ready", icon='EXPORT')
        body.operator("preflight.make_rigid", icon='MOD_VERTEX_WEIGHT')
        hint(body, "Then Export to Engine, on the Export tab")


class VIEW3D_PT_preflight_checks(Page):
    bl_label = "Checks"

    def draw(self, context):
        s = context.scene.preflight
        layout = self.layout

        body = section(layout, "Prep & Validate", "checks")
        row = body.row(align=True)
        row.scale_y = 1.4
        row.operator("preflight.full_prep", icon='SHADERFX', depress=True)
        row.operator("preflight.run_all_checks", icon='CHECKMARK')
        hint(body, "Or one check at a time:")
        g = grid(body, context)
        g.operator("preflight.validate", text="Validate", icon='CHECKMARK')
        g.operator("preflight.check_names", text="Names", icon='SORTALPHA')
        g.operator("preflight.check_projection", text="Projection", icon='LIGHT_SUN')

        body = section(layout, "Thresholds", "checks")
        g = grid(body, context)
        g.prop(s, "corner_angle", text="Corner")
        g.prop(s, "skew_angle", text="Skew")
        g.prop(s, "texel_tolerance", text="Texel")
        g.prop(s, "projection_tolerance", text="Ray Gap")
        body.prop(s, "projection_average")

        body = section(layout, "Fix Problems", "checks")
        props = context.window_manager.preflight
        fixable = sum(1 for i in props.issues if i.fix_kind in FIXES and not i.ignored)
        g = grid(body, context)
        hint(g, f"{fixable} fixable in Issues" if issue_store.any_checked(props)
             else "Run a check first")
        g.operator("preflight.fix_all_issues", icon='TOOL_SETTINGS')

        body = section(layout, "Select Problems", "checks")
        g = grid(body, context)
        for kind, (domain, label, _) in ELEMENT_KINDS.items():
            if kind in ISSUE_ONLY:
                continue
            g.operator("preflight.select_problem_edges", text=SHORT_LABELS.get(kind, label),
                       icon=_select_icon(kind)).kind = kind


# --- Packer and stacker settings ---
# Drawn from UVPackmaster's and Zen UV's own properties, so edits here apply there.

def engine_heading(layout, text, key):
    """A dimmed, collapsible heading for another add-on's settings.

    Returns the layout to fill, or a no-op stand-in when collapsed.
    """
    layout.separator(factor=0.5)
    sid = section_id("engine_" + key)
    row = layout.row(align=True)
    row.active = False
    row.alignment = 'LEFT'
    closed = collapse_toggle(row, bpy.context, sid)
    row.operator("preflight.toggle_section", text=text, icon='PREFERENCES',
                 emboss=False).key = sid
    return _CLOSED if closed else layout.column()


def _active_prop(layout, data, prop, active, **kw):
    row = layout.row(align=True)
    row.active = active
    row.prop(data, prop, **kw)


def draw_uvpm_pack(layout, context, s):
    mp = integrations.uvpm_settings(context)
    if mp is None:
        return
    layout = engine_heading(layout, "UVPackmaster", "uvpm_pack")
    col = layout.column(align=True)
    labelled(col, s, "uvpm_pack_mode", "Mode")
    mode = s.uvpm_pack_mode
    if "groups" in mode:
        labelled(col, mp, "group_method_pack", "Group By")
        if mp.group_method_pack == '4':
            hint(layout, "Groups from UVPackmaster's grouping scheme")
        else:
            ag = mp.auto_group_options
            labelled(col, ag, "tdensity_policy", "Texel Density")
            if mode == 'pack.groups_to_tiles':
                labelled(col, ag, "group_layout_mode", "Layout")
                col.prop(ag, "tile_count_per_group")
            elif mode == 'pack.groups_together':
                col.prop(ag.base, "grouping_compactness", text="Compactness")
    if mode in ('pack.groups_to_tiles', 'pack.tiles'):
        hint(layout, "Packs past 0-1: bakers skip it without UDIMs")
    # Pre-Flight's own settings for its packs; UVPackmaster's panel keeps its own.
    g = grid(layout, context, align=False)
    g.prop(s, "uvpm_rotate")
    square = mapsize.is_square(mapsize.of_object(s, context.active_object))
    step = g.row(align=True)
    step.active = s.uvpm_rotate and square
    step.prop(s, "uvpm_rotation_step")
    g.prop(s, "uvpm_flip")
    g.prop(s, "uvpm_normalize_scale")
    g.prop(s, "uvpm_heuristic")
    time = g.row(align=True)
    time.active = s.uvpm_heuristic
    time.prop(s, "uvpm_heuristic_time")
    g.prop(s, "uvpm_precision")
    if s.uvpm_heuristic and not s.uvpm_heuristic_time:
        hint(layout, f"Time 0: Pack runs until Esc; Fix All uses "
                     f"{integrations.scripted_search_seconds(context)} s")
    if not s.pack_use_padding:
        g.prop(mp, "pixel_margin_enable", text="Pixel Margin")
        if mp.pixel_margin_enable:
            g.prop(mp, "pixel_margin")
        else:
            g.prop(mp, "margin")


def draw_zen_pack(layout, context, s):
    zp = integrations.zen_settings(context)
    if zp is None:
        return
    layout = engine_heading(layout, "Zen UV", "zen_pack")
    labelled(layout, zp, "packEngine", "Engine")
    g = grid(layout, context, align=False)
    g.prop(zp, "averageBeforePack", text="Average Scale")
    g.prop(zp, "rotateOnPack", text="Rotate")
    g.prop(zp, "packFixedScale")
    g.prop(zp, "lock_overlapping_enable", text="Lock Overlapping")
    _active_prop(g, zp, "lock_overlapping_mode", zp.lock_overlapping_enable, text="")
    if not s.pack_use_padding:
        g.prop(zp, "margin_px")


def draw_uvpm_stack(layout, context):
    mp = integrations.uvpm_settings(context)
    if mp is None:
        return
    simi = mp.simi_props
    layout = engine_heading(layout, "UVPackmaster Similarity", "uvpm_stack")
    col = layout.column(align=True)
    labelled(col, simi, "simi_mode", "Mode")
    labelled(col, simi, "match_3d_axis", "Match 3D Axis")
    g = grid(layout, context, align=False)
    g.prop(simi, "threshold")
    g.prop(simi, "check_holes")
    g.prop(simi, "adjust_scale")
    g.prop(simi, "correct_vertices")
    if simi.correct_vertices:
        g.prop(simi, "vertex_threshold")


def draw_zen_stack(layout, context):
    zp = integrations.zen_settings(context)
    if zp is None:
        return
    layout = engine_heading(layout, "Zen UV", "zen_stack")
    layout.prop(zp, "stackMoveOnly")
    hint(layout, "Without the Zen UV Library, try Stack Move Only")


# --- Issues page ---

def _short_heading(heading):
    if heading.startswith(TEXTURE_SET_PREFIX):
        return "Set: " + heading[len(TEXTURE_SET_PREFIX):]
    return heading


_longest_heading = {"key": None, "value": 4}  # cached for _heading_factor


def _heading_factor(context, items):
    """Split factor for the heading column, fitted to the longest heading within limits."""
    # Called for every drawn row, so only re-measure when the list changes.
    key = (len(items), items[0].heading if len(items) else "",
           items[-1].heading if len(items) else "")
    if _longest_heading["key"] != key:
        _longest_heading["key"] = key
        _longest_heading["value"] = max((len(_short_heading(i.heading)) for i in items),
                                        default=4)
    longest = _longest_heading["value"]
    ui_scale = context.preferences.system.ui_scale
    char_px = 6.5 * ui_scale
    needed = (longest + 1) * char_px + 24 * ui_scale  # text plus icon
    return min(0.45, max(0.18, needed / max(context.region.width - 60, 1)))


class PREFLIGHT_UL_issues(bpy.types.UIList):
    show_errors: BoolProperty(name="Errors", default=True, description="Show errors")
    show_warnings: BoolProperty(name="Warnings", default=True, description="Show warnings")
    show_info: BoolProperty(name="Info", default=True, description="Show info")
    show_ignored: BoolProperty(name="Ignored", default=True,
                               description="Show ignored issues (greyed out, at the end)")

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        # Grey out ignored issues and issues whose objects are gone.
        row.active = not item.ignored and len(item.live_objects()) == len(item.objects)
        split = row.split(factor=_heading_factor(context, data.issues), align=True)
        # Red text for errors, except on the highlighted row where it wouldn't be legible.
        split.alert = item.severity == 'ERROR' and index != getattr(active_data, active_propname)
        split.label(text=_short_heading(item.heading), icon_value=icons.severity(item.severity))
        split.label(text=item.message)
        if item.ignored:
            row.operator("preflight.issue_ignore", text="", icon='HIDE_ON',
                         emboss=False).index = index
            return
        if item.select_kind:
            row.operator("preflight.issue_select", text="", icon=_select_icon(item.select_kind),
                         emboss=False).index = index
        if item.fix_kind:
            row.operator("preflight.issue_fix", text="", icon='TOOL_SETTINGS',
                         emboss=False).index = index

    def draw_filter(self, context, layout):
        row = layout.row(align=True)
        row.prop(self, "filter_name", text="", icon='VIEWZOOM')
        row.prop(self, "show_errors", text="", icon_value=icons.severity('ERROR'), toggle=True)
        row.prop(self, "show_warnings", text="", icon_value=icons.severity('WARNING'),
                 toggle=True)
        row.prop(self, "show_info", text="", icon_value=icons.severity('INFO'), toggle=True)
        row.prop(self, "show_ignored", text="", icon='HIDE_ON', toggle=True)
        if len(context.scene.preflight.ignored_issues):
            row.operator("preflight.clear_ignored", text="", icon='LOOP_BACK')

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        flags = bpy.types.UI_UL_list.filter_items_by_name(
            self.filter_name, self.bitflag_filter_item, items, "name")
        if not flags:
            flags = [self.bitflag_filter_item] * len(items)
        shown = {'ERROR': self.show_errors, 'WARNING': self.show_warnings,
                 'INFO': self.show_info}
        flags = [f if shown[it.severity] and (self.show_ignored or not it.ignored) else 0
                 for f, it in zip(flags, items)]
        # Ignored issues go last; the rest keep their order.
        order = sorted(range(len(items)), key=lambda i: (items[i].ignored, i))
        new_order = [0] * len(items)
        for pos, i in enumerate(order):
            new_order[i] = pos
        return flags, new_order


class VIEW3D_PT_preflight_issues(Page):
    bl_label = "Issues"

    @staticmethod
    def draw_title_extra(layout, context):
        props = context.window_manager.preflight
        if not issue_store.any_checked(props):
            return
        row = layout.row(align=True)
        for severity, n in zip(('ERROR', 'WARNING', 'INFO'), issue_store.counts(props)):
            sub = row.row(align=True)
            sub.active = n > 0
            sub.label(text=str(n), icon_value=icons.severity(severity))

    def draw(self, context):
        props = context.window_manager.preflight
        layout = self.layout

        if not issue_store.any_checked(props):
            note = layout.column(align=True)
            note.active = False
            note.label(text="No checks run yet.", icon='INFO')
            note.label(text="Run Validate, Names or Projection.", icon='BLANK1')
            return

        fixable = sum(1 for i in props.issues if i.fix_kind in FIXES and not i.ignored)
        row = layout.row(align=True)
        row.operator("preflight.recheck_issues", icon='FILE_REFRESH')
        row.operator("preflight.fix_all_issues",
                     text=f"Fix All ({fixable})" if fixable else "Fix All",
                     icon='TOOL_SETTINGS')
        row.operator("preflight.clear_issues", text="", icon='TRASH')

        if props.checked_scene and props.checked_scene != context.scene.name:
            box = layout.box()
            box.label(text=f"Results from scene '{props.checked_scene}'", icon='SCENE_DATA')
        elif issue_store.is_stale():
            box = layout.box()
            box.alert = True
            box.label(text="Scene changed since last check", icon='TIME')

        if not props.issues:
            layout.label(text="No issues", icon_value=icons.icon("dot_ok"))
            return
        layout.template_list("PREFLIGHT_UL_issues", "", props, "issues", props, "issue_index",
                             rows=min(max(len(props.issues), 3), 8))
        row = layout.row(align=True)
        from .. import preferences
        prefs = preferences.get(context)
        if isinstance(prefs, bpy.types.AddonPreferences):
            row.prop(prefs, "isolate_issues", text="Isolate", icon='HIDE_OFF')
        space = context.space_data
        if getattr(space, "local_view", None):
            row.operator("preflight.exit_isolation", text="Un-isolate", icon='WORLD')

        if not (0 <= props.issue_index < len(props.issues)):
            return
        item = props.issues[props.issue_index]
        box = layout.box()
        title = box.row()
        title.alert = item.severity == 'ERROR'
        title.label(text=item.heading, icon_value=icons.severity(item.severity))
        _wrapped(box, context, item.message)
        if len(item.objects) > 1:
            _wrapped(box, context, "Objects: " + ", ".join(o.name for o in item.objects))
        missing = len(item.objects) - len(item.live_objects())
        if missing:
            box.label(text=f"{missing} object(s) renamed or deleted", icon='GHOST_ENABLED')
        if item.ignored:
            box.label(text="Ignored: marked as intended", icon='HIDE_ON')
        col = box.column(align=True)
        if item.select_kind or (item.fix_kind and not item.ignored):
            row = col.row(align=True)
            if item.select_kind:
                row.operator("preflight.issue_select", icon=_select_icon(item.select_kind),
                             text=f"Select {ELEMENT_KINDS[item.select_kind][1]}")
            if item.fix_kind and not item.ignored:
                row.operator("preflight.issue_fix", icon='TOOL_SETTINGS',
                             text=FIXES[item.fix_kind][0])
        col.operator("preflight.issue_ignore",
                     text="Count Again" if item.ignored else "Ignore (Intended)",
                     icon='HIDE_OFF' if item.ignored else 'HIDE_ON')


# --- Registration ---

# Tab id -> page, in strip order (ids match properties.TABS).
PAGES = {
    'PAIRS': VIEW3D_PT_preflight_pairs,
    'BAKER': VIEW3D_PT_preflight_baker,
    'SHADING': VIEW3D_PT_preflight_shading,
    'UVS': VIEW3D_PT_preflight_uvs,
    'CHECKS': VIEW3D_PT_preflight_checks,
    'ISSUES': VIEW3D_PT_preflight_issues,
    'EXPORT': VIEW3D_PT_preflight_export,
    'RIGGING': VIEW3D_PT_preflight_rigging,
}

PANELS = (VIEW3D_PT_preflight,)


def register(category="Pre-Flight"):
    bpy.utils.register_class(PREFLIGHT_OT_toggle_section)
    bpy.utils.register_class(PREFLIGHT_UL_issues)
    for panel in PANELS:
        panel.bl_category = category
        bpy.utils.register_class(panel)


def unregister():
    for panel in reversed(PANELS):
        if panel.is_registered:
            bpy.utils.unregister_class(panel)
    bpy.utils.unregister_class(PREFLIGHT_UL_issues)
    bpy.utils.unregister_class(PREFLIGHT_OT_toggle_section)
