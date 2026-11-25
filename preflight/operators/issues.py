# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Issue list operators: run checks, re-check, select, ignore and fix issues.

Also holds the stacked-UV offset / restore and padding suggestion operators.
"""

import bmesh
import bpy
import numpy as np
from bpy.props import IntProperty

from .. import integrations
from .. import issues as issue_store
from ..core import checks, mapsize, rigging, texel, uv_stack
from ..core.dependencies import collection_meshes, move_to_collection
from ..core.edges import (clear_custom_normals, edge_analysis, mirror_plane_edges,
                          problem_edge_mask, strip_auto_smooth)
from ..core.geometry import DOUBLES_DISTANCE
from ..core.naming import DUP_SUFFIX_RE
from ..core.problems import ELEMENT_KINDS
from ..core.triangulate import add_triangulate
from ..core.uv_layout import triangle_uvs, uv_islands, uv_offset_ok
from ..overlay import overlay_invalidate
from ..utils import MeshOperator, object_mode, selected_meshes, show_issues, unique_meshes


# --- Running checks ---

def run_mesh_check(context, objects):
    """Validate `objects` and store the results in the issue list.

    Objects unchanged since their last check reuse cached results; the texture-set
    checks (overlaps, padding, texel density) always re-run.
    """
    with object_mode(context):
        issue_store.flush(context)  # drop cached results of objects with pending edits
        issues = checks.check_meshes(objects, context.scene.preflight,
                                     context.scene.preflight.high_collection,
                                     cache=issue_store.ObjectCache())
    issue_store.store(context, 'MESH', issues, objects)
    return issues


def names_check_objects(context):
    """Return (objects, lows, highs) from the Low/High collections, else the selection."""
    s = context.scene.preflight
    if s.low_collection and s.high_collection:
        lows, highs = collection_meshes(s.low_collection), collection_meshes(s.high_collection)
        return lows + [h for h in highs if h not in lows], lows, highs
    return selected_meshes(context), None, None


def run_names_check(context, objects=None):
    """Check names of `objects`, or of the collections / selection when not given."""
    if objects is None:
        objects, lows, highs = names_check_objects(context)
        context.window_manager.preflight.names_used_collections = lows is not None
    else:
        lows = highs = None
    issues, pair_count = checks.check_names(objects, lows, highs)
    issue_store.store(context, 'NAMES', issues, objects)
    return issues, pair_count


def run_projection_check(context, objects=None):
    """Check low/high distances for pairs in `objects`, defaulting to collections or selection."""
    if objects is None:
        objects = names_check_objects(context)[0]
    with object_mode(context):
        issues = checks.check_projection(objects, context.scene.preflight,
                                         context.evaluated_depsgraph_get())
    if issues:  # no rows means no pairs: leave the list as it was
        issue_store.store(context, 'PROJECTION', issues, objects)
    return issues


def rig_check_armatures(context):
    """Armatures to check: the selected ones, else the Rigging tab's rig."""
    armatures = [o for o in context.selected_objects if o.type == 'ARMATURE']
    rig = context.scene.preflight.rig_armature
    if not armatures and rig is not None:
        armatures = [rig]
    return armatures


def run_rig_check(context, armatures):
    """Check `armatures` for game-export problems and store the results."""
    found = []
    for arm in armatures:
        for severity, message, fix, names in rigging.check_rig(arm):
            found.append(checks.Issue('RIG', severity, arm.name, message,
                                      (arm.name, *names), fix=fix))
    issue_store.store(context, 'RIG', found, armatures)
    return found


def report_check(operator, context, issues, title, ok_message):
    """Report check results as a popup or a one-line summary, per the preferences."""
    if not issues:
        operator.report({'INFO'}, ok_message)
        return
    from .. import preferences
    if preferences.get(context).show_popup:
        show_issues(operator, context,
                    [(i.heading, i.severity, i.message) for i in issues], title)
        return
    errors = sum(1 for i in issues if i.severity == 'ERROR')
    warnings = sum(1 for i in issues if i.severity == 'WARNING')
    operator.report({'WARNING' if errors or warnings else 'INFO'},
                    f"{errors} error(s), {warnings} warning(s), "
                    f"{len(issues) - errors - warnings} info: see Issues")


def _checked_objects(props, coll):
    return [o for o in (bpy.data.objects.get(n.name) for n in getattr(props, coll))
            if o is not None and o.type == 'MESH']


def recheck_after_fix(context, sources=('MESH', 'NAMES', 'PROJECTION', 'RIG')):
    """Re-check after a fix, or only mark the list out of date if that preference is off."""
    from .. import preferences
    if preferences.get(context).recheck_after_fix:
        recheck(context, sources)
    else:
        issue_store.mark_stale(context)


def recheck(context, sources=('MESH', 'NAMES', 'PROJECTION', 'RIG')):
    """Re-run the checks that have been run, on the same objects as last time."""
    props = context.window_manager.preflight
    if 'MESH' in sources and props.mesh_checked:
        run_mesh_check(context, _checked_objects(props, "checked_mesh"))
    if 'NAMES' in sources and props.names_checked:
        if props.names_used_collections:
            run_names_check(context)
        else:
            run_names_check(context, _checked_objects(props, "checked_names"))
    if 'PROJECTION' in sources and props.projection_checked:
        run_projection_check(context, _checked_objects(props, "checked_projection"))
    if 'RIG' in sources and props.rig_checked:
        rigs = [o for o in (bpy.data.objects.get(n.name) for n in props.checked_rig)
                if o is not None and o.type == 'ARMATURE']
        run_rig_check(context, rigs)


# --- Stacked UVs ---

def offset_stacked(objects, settings):
    """Move stacked copies +1U, one per stack staying put.

    Returns (islands moved, objects touched).
    """
    moved = touched = 0
    for obj, (face_island, _, copies) in uv_stack.find_stacks(objects, settings).items():
        if copies and uv_stack.offset_islands(obj.data, face_island, copies):
            moved += len(copies)
            touched += 1
    return moved, touched


# --- Fixes ---

def per_object(fn):
    """Adapt fn(obj, item, context) -> error or None to the set-level fix signature."""
    def run(objects, item, context):
        return [err for err in (fn(obj, item, context) for obj in objects) if err]
    return run


def _apply_scale(obj, item, context):
    if obj.data.users > 1:
        return f"{obj.name} shares its mesh, so scale can't be applied"
    with context.temp_override(object=obj, active_object=obj,
                               selected_objects=[obj], selected_editable_objects=[obj]):
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)


def _soften_mirror(obj, item, context):
    mesh = obj.data
    sharp = np.zeros(len(mesh.edges), dtype=bool)
    mesh.edges.foreach_get("use_edge_sharp", sharp)
    mesh.edges.foreach_set("use_edge_sharp", sharp & ~mirror_plane_edges(obj, mesh))
    mesh.update()


def _uv_offset(obj, item, context):
    mod = obj.modifiers.get(item.fix_data)
    if mod is None:
        return f"Modifier '{item.fix_data}' is gone"
    if not uv_offset_ok(mod):
        mod.offset_u = 1.0
    overlay_invalidate()


def _sync_data_name(obj, item, context):
    holder = bpy.data.meshes.get(obj.name)
    if holder is not None and holder is not obj.data:
        return f"Another mesh is already named '{obj.name}'"
    obj.data.name = obj.name


def _split_mesh(obj, item, context):
    if obj.data.users > 1:
        obj.data = obj.data.copy()


def _bmesh_edit(obj, edit):
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        edit(bm)
        bm.to_mesh(obj.data)
    finally:
        bm.free()
    obj.data.update()


def _recalc_normals(obj, item, context):
    _bmesh_edit(obj, lambda bm: bmesh.ops.recalc_face_normals(bm, faces=bm.faces))


def _merge_doubles(obj, item, context):
    _bmesh_edit(obj, lambda bm: bmesh.ops.remove_doubles(bm, verts=bm.verts,
                                                         dist=DOUBLES_DISTANCE))


def _delete_loose(obj, item, context):
    def edit(bm):
        edges = [e for e in bm.edges if not e.link_faces]
        if edges:
            bmesh.ops.delete(bm, geom=edges, context='EDGES')
        verts = [v for v in bm.verts if not v.link_edges]
        if verts:
            bmesh.ops.delete(bm, geom=verts, context='VERTS')
    _bmesh_edit(obj, edit)


def _offset_stacked(objects, item, context):
    moved, _ = offset_stacked(objects, context.scene.preflight)
    return [] if moved else ["No stacked islands found"]


def _edit_mode(context, objects, fn):
    """Run fn() in Edit Mode on `objects` (all faces selected). Returns an error or None."""
    try:
        integrations.edit_mode(context, objects, fn)
    except RuntimeError as ex:
        return str(ex)


def _unwrap(obj, item, context):
    margin = mapsize.uv_margin(mapsize.of_object(context.scene.preflight, obj))
    if not obj.data.uv_layers:
        obj.data.uv_layers.new(name="UVMap")
    return _edit_mode(context, [obj], lambda: bpy.ops.uv.smart_project(island_margin=margin))


def _soften_unsplit(obj, item, context):
    mesh = obj.data
    sharp = np.zeros(len(mesh.edges), dtype=bool)
    mesh.edges.foreach_get("use_edge_sharp", sharp)
    unsplit = problem_edge_mask(obj, 'HARD_NO_SPLIT', context.scene.preflight)
    mesh.edges.foreach_set("use_edge_sharp", sharp & ~unsplit)
    mesh.update()


def _seams_from_islands(obj, item, context):
    mesh = obj.data
    uv_split, _, _ = edge_analysis(mesh)
    seams = np.zeros(len(mesh.edges), dtype=bool)
    mesh.edges.foreach_get("use_seam", seams)
    mesh.edges.foreach_set("use_seam", seams | uv_split)
    mesh.update()


def _pack_uvs(objects, item, context):
    """Repack the texture set's 0-1 islands with the chosen packer; +1U copies stay put."""
    meshes = [o for o in objects if o.type == 'MESH' and o.data.uv_layers]
    try:
        integrations.pack(context, meshes)
    except RuntimeError as ex:
        return [str(ex)]
    return []


def _match_texel(obj, item, context):
    """Scale the object's texel-density outliers to the texture set's density."""
    s = context.scene.preflight
    mesh = obj.data
    if not mesh.uv_layers:
        return f"{obj.name} has no UVs"
    uv_split, _, _ = edge_analysis(mesh)
    islands = uv_islands(mesh, uv_split)
    tri_uv, tri_face = triangle_uvs(mesh)
    scale = s.id_data.unit_settings.scale_length or 1.0
    texel_size = mapsize.texel_size(mapsize.of_object(s, obj))
    ids, density, world = texel.island_densities(obj, mesh, tri_uv, tri_face, islands,
                                                 texel_size, scale)
    if not len(ids):
        return None
    target = float(item.fix_data) if item.fix_data else texel.weighted_median(density, world)
    off_ids, off_d = texel.outlier_islands(ids, density, target, s.texel_tolerance / 100)
    if len(off_ids):
        integrations.set_island_density(context, obj, islands, off_ids, off_d, target)


def _strip_dup_suffix(obj, item, context):
    clean = DUP_SUFFIX_RE.sub("", obj.name)
    holder = bpy.data.objects.get(clean)
    if holder is not None and holder is not obj:
        return f"'{clean}' is taken by another object: use Match & Rename"
    obj.name = clean
    if obj.data and obj.data.users == 1 and bpy.data.meshes.get(clean) in (None, obj.data):
        obj.data.name = clean


def _move_collection(obj, item, context):
    s = context.scene.preflight
    target = s.low_collection if item.fix_data == "low" else s.high_collection
    if target is None:
        return "Low / High collections aren't set"
    move_to_collection(obj, target)


def _match_uv_ratio(objects, item, context):
    s = context.scene.preflight
    for obj, me in unique_meshes(objects):
        mapsize.match_uv_ratio(me, mapsize.aspect(mapsize.of_object(s, obj)))
    return []


def _match_names(objects, item, context):
    if bpy.ops.preflight.match_names.poll():
        bpy.ops.preflight.match_names()
        return []
    return ["Set the Low and High collections to use Match & Rename"]


# --- Rig fixes ---

def _rig_of(objects):
    return next((o for o in objects if o.type == 'ARMATURE'), None)


def _rig_fix(fn):
    """A fix that needs the issue's armature: fn(armature, objects, context)."""
    def run(objects, item, context):
        armature = _rig_of(objects)
        if armature is None:
            return ["The rig is gone"]
        return fn(armature, objects, context) or []
    return run


def _add_root(armature, objects, context):
    rigging.add_root(context, armature)


def _rename_rig(armature, objects, context):
    armature.name = rigging.safe_rig_name(armature)


def _rigid_skin(armature, objects, context):
    for obj in rigging.bone_parented_meshes(armature):
        rigging.rigid_skin(obj, armature, rigging.bone_target(obj)[1])


def _make_rigid(armature, objects, context):
    for obj in objects:
        if obj.type == 'MESH':
            rigging.make_rigid(obj, armature)


def _apply_rig_scale(armature, objects, context):
    rigging.apply_rig_scale(context, armature)


def _apply_part_scale(armature, objects, context):
    for obj in objects:
        if obj.type == 'MESH' and rigging.is_scaled(obj):
            rigging.apply_object_scale(obj)


def _fix_negative(armature, objects, context):
    for obj in objects:
        rigging.fix_negative_scale(obj)


def _bake_rig(armature, objects, context):
    scene = context.scene
    rigging.bake_motion(context, armature, scene.frame_start, scene.frame_end)


# kind -> (button label, fn(objects, item, context) -> list of error messages)
FIXES = {
    'ADD_ROOT': ("Add Root Bone", _rig_fix(_add_root)),
    'RENAME_RIG': ("Rename Rig", _rig_fix(_rename_rig)),
    'RIGID_SKIN': ("Skin Parts Rigidly", _rig_fix(_rigid_skin)),
    'MAKE_RIGID': ("Make Rigid", _rig_fix(_make_rigid)),
    'BAKE_RIG': ("Bake Motion", _rig_fix(_bake_rig)),
    'APPLY_RIG_SCALE': ("Apply Rig Scale", _rig_fix(_apply_rig_scale)),
    'APPLY_PART_SCALE': ("Apply Part Scale", _rig_fix(_apply_part_scale)),
    'FIX_NEGATIVE': ("Make Scale Positive", _rig_fix(_fix_negative)),
    'APPLY_SCALE': ("Apply Scale", per_object(_apply_scale)),
    'SOFTEN_MIRROR': ("Soften Mirror Line", per_object(_soften_mirror)),
    'TRIANGULATE': ("Add Triangulate", per_object(
        lambda obj, item, ctx: add_triangulate(obj, ctx.scene.preflight))),
    'CLEAR_CUSTOM_NORMALS': ("Clear Custom Normals", per_object(
        lambda obj, item, ctx: clear_custom_normals(obj) and None)),
    'STRIP_AUTO_SMOOTH': ("Remove Smooth by Angle", per_object(
        lambda obj, item, ctx: strip_auto_smooth(obj) and None)),
    'UV_OFFSET': ("Set +1U", per_object(_uv_offset)),
    'SYNC_DATA_NAME': ("Sync Mesh Name", per_object(_sync_data_name)),
    'SPLIT_MESH': ("Split Shared Mesh", per_object(_split_mesh)),
    'RECALC_NORMALS': ("Recalculate Normals Outside", per_object(_recalc_normals)),
    'MERGE_DOUBLES': ("Merge Doubles", per_object(_merge_doubles)),
    'DELETE_LOOSE': ("Delete Loose", per_object(_delete_loose)),
    'OFFSET_STACKED': ("Offset Stacked UVs +1U", _offset_stacked),
    'UNWRAP': ("Smart UV Project", per_object(_unwrap)),
    'SOFTEN_UNSPLIT': ("Soften Edges Without UV Split", per_object(_soften_unsplit)),
    'SEAMS_FROM_ISLANDS': ("Seams from UV Islands", per_object(_seams_from_islands)),
    'PACK_UVS': ("Pack UV Islands", _pack_uvs),
    'MATCH_TEXEL': ("Match Texel Density", per_object(_match_texel)),
    'STRIP_DUP_SUFFIX': ("Remove .### Suffix", per_object(_strip_dup_suffix)),
    'MOVE_COLLECTION': ("Move to the Right Collection", per_object(_move_collection)),
    'MATCH_NAMES': ("Match & Rename", _match_names),
    'MATCH_UV_RATIO': ("Match UVs to Map Ratio", _match_uv_ratio),
}


# --- Operators ---

def _get_item(context, index):
    items = context.window_manager.preflight.issues
    return items[index] if 0 <= index < len(items) else None


class IssueOperator:
    """Mixin for operators on one issue row: `index`, or the active row when -1."""
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    index: IntProperty(default=-1, options={'SKIP_SAVE'})

    def item(self, context):
        props = context.window_manager.preflight
        return _get_item(context, self.index if self.index >= 0 else props.issue_index)


class PREFLIGHT_OT_issue_select(IssueOperator, bpy.types.Operator):
    """Select the problem elements of this issue in Edit Mode"""
    bl_idname = "preflight.issue_select"
    bl_label = "Select"

    @classmethod
    def description(cls, context, props):
        item = _get_item(context, props.index)
        if item and item.select_kind in ELEMENT_KINDS:
            return ELEMENT_KINDS[item.select_kind][2]
        return cls.__doc__

    def execute(self, context):
        from .checks import select_elements  # checks imports this module
        item = self.item(context)
        if item is None or item.select_kind not in ELEMENT_KINDS:
            return {'CANCELLED'}
        kind, data = item.select_kind, item.select_data
        # Projection issues list the highs too, but only the low has the problem.
        names = [item.heading] if item.source == 'PROJECTION' else [o.name for o in item.objects]
        objects = [o for o in (bpy.data.objects.get(n) for n in names) if o and o.type == 'MESH']
        picked = issue_store.select_objects(context, objects)
        if not picked:
            self.report({'WARNING'}, "The issue's objects are gone or hidden")
            return {'CANCELLED'}
        from .. import preferences
        if preferences.get(context).isolate_issues:
            issue_store.focus(context, isolate=True)
        total = select_elements(context, picked, kind, context.scene.preflight, data)
        if total:
            issue_store.focus(context, isolate=False)  # frame the problem elements
        label = ELEMENT_KINDS[kind][1].lower()
        self.report({'INFO'}, f"Selected {total} {label}" if total else f"No {label} left")
        return {'FINISHED'}


class PREFLIGHT_OT_exit_isolation(bpy.types.Operator):
    """Leave the isolated (local) view and show every object again (Numpad /)"""
    bl_idname = "preflight.exit_isolation"
    bl_label = "Un-isolate"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        target = issue_store._view3d(context)
        if target is None or not target[1].spaces.active.local_view:
            return {'CANCELLED'}
        win, area, region = target
        with context.temp_override(window=win, area=area, region=region):
            bpy.ops.view3d.localview()
        return {'FINISHED'}


class PREFLIGHT_OT_issue_ignore(IssueOperator, bpy.types.Operator):
    """Mark this issue as intended (e.g. a deliberately smooth mirror seam): it stays in
the list greyed out, isn't counted and Fix All skips it, across re-checks. Saved with
the file. Click again to count it again"""
    bl_idname = "preflight.issue_ignore"
    bl_label = "Ignore"

    def execute(self, context):
        item = self.item(context)
        if item is None:
            return {'CANCELLED'}
        issue_store.set_ignored(context, item, not item.ignored)
        return {'FINISHED'}


class PREFLIGHT_OT_clear_ignored(bpy.types.Operator):
    """Count every ignored issue again"""
    bl_idname = "preflight.clear_ignored"
    bl_label = "Unignore All"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    def execute(self, context):
        context.scene.preflight.ignored_issues.clear()
        for item in context.window_manager.preflight.issues:
            item.ignored = False
        return {'FINISHED'}


class PREFLIGHT_OT_issue_fix(IssueOperator, bpy.types.Operator):
    """Fix this issue, then re-check"""
    bl_idname = "preflight.issue_fix"
    bl_label = "Fix"

    @classmethod
    def description(cls, context, props):
        item = _get_item(context, props.index)
        if item and item.fix_kind in FIXES:
            names = ", ".join(o.name for o in item.objects)
            return f"{FIXES[item.fix_kind][0]} on {names}, then re-check"
        return cls.__doc__

    def invoke(self, context, event):
        """Run Pack UV Islands interactively in UVPackmaster, like its own Pack button."""
        item = self.item(context)
        if item is not None and item.fix_kind == 'PACK_UVS':
            from .uv_tools import start_interactive_pack
            objects = [o for o in item.live_objects() if o.type == 'MESH' and o.data.uv_layers]
            source = item.source

            def after(ctx):
                issue_store.forget(objects)
                recheck(ctx, (source,))
            try:
                if objects and start_interactive_pack(context, objects, "IN_TILE", False, after):
                    self.report({'INFO'}, "Packing with UVPackmaster: Esc stops it, any key "
                                          "closes its summary")
                    return {'FINISHED'}
            except RuntimeError as ex:
                self.report({'ERROR'}, str(ex))
                return {'CANCELLED'}
        return self.execute(context)

    def execute(self, context):
        item = self.item(context)
        if item is None or item.fix_kind not in FIXES:
            return {'CANCELLED'}
        label, fn = FIXES[item.fix_kind]
        source = item.source
        objects = item.live_objects()
        if not objects:
            self.report({'WARNING'}, "The issue's objects are gone")
            return {'CANCELLED'}
        with object_mode(context):
            errors = fn(objects, item, context)  # `item` is invalid once the list is rebuilt
        issue_store.forget(objects)
        recheck_after_fix(context, (source,))
        if errors:
            self.report({'WARNING'}, "; ".join(errors))
        else:
            self.report({'INFO'}, f"{label}: done on {len(objects)} object(s)")
        return {'FINISHED'}


# Fix All order: split shared meshes before editing them, apply scale before geometry
# fixes, merge doubles before deleting loose geometry, and do UV / name fixes last.
FIX_ORDER = ('SPLIT_MESH', 'APPLY_SCALE', 'MERGE_DOUBLES', 'DELETE_LOOSE', 'RECALC_NORMALS',
             'UNWRAP', 'SEAMS_FROM_ISLANDS', 'SOFTEN_UNSPLIT', 'SOFTEN_MIRROR',
             'CLEAR_CUSTOM_NORMALS', 'STRIP_AUTO_SMOOTH', 'TRIANGULATE',
             'UV_OFFSET', 'OFFSET_STACKED', 'MATCH_UV_RATIO', 'MATCH_TEXEL', 'PACK_UVS',
             'MOVE_COLLECTION', 'STRIP_DUP_SUFFIX', 'MATCH_NAMES', 'SYNC_DATA_NAME',
             'FIX_NEGATIVE', 'APPLY_PART_SCALE', 'APPLY_RIG_SCALE', 'ADD_ROOT', 'RENAME_RIG',
             'RIGID_SKIN', 'MAKE_RIGID', 'BAKE_RIG')


# Fixes that act on the whole Low / High setup: Fix All runs them once.
WHOLE_SCENE_FIXES = {'MATCH_NAMES'}


def pending_fixes(props):
    """Return {fix kind: [(object names, fix_data), ...]} for unignored fixable rows."""
    out = {}
    for item in props.issues:
        if item.fix_kind in FIXES and not item.ignored:
            out.setdefault(item.fix_kind, []).append(
                (tuple(o.name for o in item.objects), item.fix_data))
    return out


class _FixData:
    """Stand-in for an issue row; fixes only read `.fix_data`."""

    def __init__(self, fix_data):
        self.fix_data = fix_data


def _fix_props():
    return {kind: bpy.props.BoolProperty(name=label, default=True)
            for kind, (label, _) in FIXES.items()}


class PREFLIGHT_OT_fix_all_issues(bpy.types.Operator):
    """Apply every available fix to the issues in the list (choose which kinds first),
then re-check"""
    bl_idname = "preflight.fix_all_issues"
    bl_label = "Fix All"
    bl_options = {'REGISTER', 'UNDO'}

    __annotations__ = {f"fix_{kind.lower()}": prop for kind, prop in _fix_props().items()}

    @classmethod
    def poll(cls, context):
        return any(i.fix_kind in FIXES and not i.ignored
                   for i in context.window_manager.preflight.issues)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=320, title="Fix All Issues",
                                                          confirm_text="Fix")

    def draw(self, context):
        pending = pending_fixes(context.window_manager.preflight)
        col = self.layout.column(align=True)
        for kind in FIX_ORDER:
            if kind in pending:
                row = col.row(align=True)
                row.prop(self, f"fix_{kind.lower()}")
                count = row.row()
                count.active = False
                count.alignment = 'RIGHT'
                count.label(text=str(len(pending[kind])))

    def execute(self, context):
        pending = pending_fixes(context.window_manager.preflight)
        done, errors = {}, []
        with object_mode(context):
            for kind in FIX_ORDER:
                if kind not in pending or not getattr(self, f"fix_{kind.lower()}"):
                    continue
                label, fn = FIXES[kind]
                seen = set()
                rows = pending[kind][:1] if kind in WHOLE_SCENE_FIXES else pending[kind]
                for names, fix_data in rows:
                    if (names, fix_data) in seen:
                        continue
                    seen.add((names, fix_data))
                    objects = [o for o in (bpy.data.objects.get(n) for n in names) if o]
                    if not objects:
                        continue
                    errors += fn(objects, _FixData(fix_data), context)
                    issue_store.forget(objects)
                    done[label] = done.get(label, 0) + 1
        recheck_after_fix(context)
        if not done:
            self.report({'INFO'}, "Nothing to fix")
            return {'CANCELLED'}
        summary = ", ".join(f"{label} \u00d7{n}" for label, n in done.items())
        if errors:
            self.report({'WARNING'}, f"{summary}. Problems: {'; '.join(errors[:3])}")
        else:
            self.report({'INFO'}, f"Fixed: {summary}")
        return {'FINISHED'}


class PREFLIGHT_OT_recheck_issues(bpy.types.Operator):
    """Run the last checks again on the same objects"""
    bl_idname = "preflight.recheck_issues"
    bl_label = "Re-check"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return issue_store.any_checked(context.window_manager.preflight)

    def execute(self, context):
        recheck(context)
        errors, warnings, infos = issue_store.counts(context.window_manager.preflight)
        self.report({'WARNING' if errors or warnings else 'INFO'},
                    f"{errors} error(s), {warnings} warning(s), {infos} info")
        return {'FINISHED'}


class PREFLIGHT_OT_clear_issues(bpy.types.Operator):
    """Clear the issue list"""
    bl_idname = "preflight.clear_issues"
    bl_label = "Clear Issues"
    bl_options = {'REGISTER'}

    def execute(self, context):
        issue_store.clear(context)
        return {'FINISHED'}


class PREFLIGHT_OT_check_projection(bpy.types.Operator):
    """Cast rays from each low along its normals to its highs, like a baker does: flags
missed rays and parts far from the high, and gives the ray distances to use"""
    bl_idname = "preflight.check_projection"
    bl_label = "Check Projection"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        s = context.scene.preflight
        return bool((s.low_collection and s.high_collection) or selected_meshes(context))

    def execute(self, context):
        issues = run_projection_check(context)
        if not issues:
            self.report({'WARNING'}, "No <name>_low / <name>_high pairs found: "
                                     "run Match & Rename or Pair Selected first")
            return {'CANCELLED'}
        report_check(self, context, issues, "Pre-Flight: Projection", "Projection looks good")
        return {'FINISHED'}


class PREFLIGHT_OT_offset_stacked_uvs(MeshOperator, bpy.types.Operator):
    """Move stacked UV islands (mirrored or duplicated parts) one tile right for baking,
keeping one copy of each in place. Restore puts them back"""
    bl_idname = "preflight.offset_stacked_uvs"
    bl_label = "Offset Stacked"

    def execute(self, context):
        with object_mode(context):
            moved, touched = offset_stacked(selected_meshes(context), context.scene.preflight)
        self.report({'INFO'}, f"Moved {moved} stacked island(s) on {touched} object(s)"
                    if moved else "No stacked islands found")
        return {'FINISHED'}


class PREFLIGHT_OT_restore_stacked_uvs(bpy.types.Operator):
    """Move islands offset by Offset Stacked back to where they were"""
    bl_idname = "preflight.restore_stacked_uvs"
    bl_label = "Restore"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(uv_stack.has_offset(o.data) for o in selected_meshes(context))

    def execute(self, context):
        with object_mode(context):
            count = sum(uv_stack.restore(me) for _, me in unique_meshes(selected_meshes(context)))
        self.report({'INFO'}, f"Restored UVs on {count} mesh(es)")
        return {'FINISHED'}


class PREFLIGHT_OT_suggest_padding(bpy.types.Operator):
    """Set padding for the texture size: size / 128 (16px at 2048), so islands stay
apart down to small mip levels"""
    bl_idname = "preflight.suggest_padding"
    bl_label = "Suggest Padding"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.preflight
        s.padding_auto = False
        s.padding = mapsize.suggested_padding(mapsize.padding_size(s))
        self.report({'INFO'}, f"Padding set to {s.padding}px for {mapsize.label(s)}")
        return {'FINISHED'}


classes = (
    PREFLIGHT_OT_issue_select,
    PREFLIGHT_OT_exit_isolation,
    PREFLIGHT_OT_issue_ignore,
    PREFLIGHT_OT_clear_ignored,
    PREFLIGHT_OT_issue_fix,
    PREFLIGHT_OT_recheck_issues,
    PREFLIGHT_OT_clear_issues,
    PREFLIGHT_OT_fix_all_issues,
    PREFLIGHT_OT_check_projection,
    PREFLIGHT_OT_offset_stacked_uvs,
    PREFLIGHT_OT_restore_stacked_uvs,
    PREFLIGHT_OT_suggest_padding,
)
