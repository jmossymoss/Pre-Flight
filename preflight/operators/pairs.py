# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Low / high pairing, renaming and name checks."""

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty

from ..core.dependencies import (all_cutters, collect_cutters, collection_meshes,
                                 copy_cutters, copy_visibility, ensure_pair_collections,
                                 move_to_collection, remap_references,
                                 separate_dependencies)
from ..core.naming import (DUP_SUFFIX_RE, apply_names, base_name, face_count,
                           free_base, match_low_high, natural_key, pair_names, pair_role,
                           suffixes, unshare_pair_data)
from ..utils import MeshOperator, selected_meshes


class PREFLIGHT_OT_match_names(bpy.types.Operator):
    """Pair each high poly with the low poly it overlaps most and rename them
<base>_low / <base>_high so bakers can match by mesh name"""
    bl_idname = "preflight.match_names"
    bl_label = "Match & Rename"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        s = context.scene.preflight
        return s.low_collection is not None and s.high_collection is not None

    def execute(self, context):
        s = context.scene.preflight
        lows = sorted(collection_meshes(s.low_collection), key=lambda o: natural_key(o.name))
        highs = [o for o in collection_meshes(s.high_collection) if o not in lows]
        if not lows:
            self.report({'WARNING'}, "Low collection has no meshes")
            return {'CANCELLED'}
        pairs, unmatched, by_name = match_low_high(lows, highs)

        unshared = unshare_pair_data(lows, highs)
        targets, used = [], set()
        everything = lows + highs
        def rename_order(obj):
            return bool(DUP_SUFFIX_RE.search(obj.name)), natural_key(obj.name)

        # Clean names first so existing pairs keep their names; .001 duplicates fill in after.
        for low in sorted(lows, key=rename_order):
            base = free_base(base_name(low.name), everything, used, s.suffix_style)
            used.add(base.lower())  # also avoid names differing only in case
            low_highs = sorted(pairs[low], key=lambda o: natural_key(o.name))
            targets += pair_names(base, low, low_highs, s.suffix_style)
        apply_names(targets)

        lonely = sum(1 for lo in lows if not pairs[lo])
        msg = (f"Renamed {len(lows)} low / {len(highs) - len(unmatched)} high "
               f"({by_name} paired by name)")
        if unshared:
            msg += f", split {unshared} shared mesh(es)"
        if unmatched or lonely:
            msg += f" ({len(unmatched)} high unmatched, {lonely} low without a high)"
            for obj in context.selected_objects:
                obj.select_set(False)
            for obj in unmatched + [lo for lo in lows if not pairs[lo]]:
                obj.select_set(True)
        self.report({'WARNING' if unmatched or lonely else 'INFO'}, msg)
        return {'FINISHED'}


class PREFLIGHT_OT_duplicate_pair(MeshOperator, bpy.types.Operator):
    """Duplicate the selected meshes in place as their bake partners.
Originals and copies get the same base name, with _high and _low suffixes"""
    bl_idname = "preflight.duplicate_pair"
    bl_label = "Duplicate as Pair"

    direction: EnumProperty(
        name="Selected Are",
        items=[
            ('HIGH', "High", "Selected become <name>_high, the copies become <name>_low"),
            ('LOW', "Low", "Selected become <name>_low, the copies become <name>_high"),
        ],
    )
    use_collections: BoolProperty(
        name="Sort into Low / High",
        description="Move originals and copies into the Low and High collections "
                    "(created if they don't exist yet)",
        default=True,
    )
    copy_cutters: BoolProperty(
        name="Copy Boolean Cutters",
        description="Give the copies their own boolean cutters, so editing the low's cuts "
                    "doesn't change the high's",
        default=True,
    )
    strip_subdivision: BoolProperty(
        name="Strip Subdivision",
        description="Remove Subdivision and Multires from the new low, "
                    "for highs built by subdividing a low cage",
        default=True,
    )

    @classmethod
    def description(cls, context, props):
        src, dst = ("high", "low") if props.direction == 'HIGH' else ("low", "high")
        return (f"Duplicate the selected meshes in place: originals become <name>_{src}, "
                f"copies become <name>_{dst}")

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "direction", expand=True)
        layout.prop(self, "use_collections")
        layout.prop(self, "copy_cutters")
        if self.direction == 'HIGH':
            layout.prop(self, "strip_subdivision")

    def execute(self, context):
        s = context.scene.preflight
        src, dst = ("high", "low") if self.direction == 'HIGH' else ("low", "high")
        if self.use_collections:
            low_coll, high_coll = ensure_pair_collections(context)
            src_coll, dst_coll = (high_coll, low_coll) if src == "high" else (low_coll, high_coll)
        else:
            src_coll, dst_coll = None, (s.low_collection if dst == "low" else s.high_collection)
        cutters = all_cutters()
        originals = [o for o in selected_meshes(context) if o not in cutters]
        if not originals:
            self.report({'WARNING'}, "Only boolean cutters are selected")
            return {'CANCELLED'}
        if context.object and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        copies, targets, used, renamed_base = [], [], set(), 0
        for obj in originals:
            dup = obj.copy()
            dup.data = obj.data.copy()  # never share a mesh between low and high
            for coll in ([dst_coll] if dst_coll else obj.users_collection):
                coll.objects.link(dup)
            copy_visibility(obj, dup)
            if dst == "low" and self.strip_subdivision:
                for mod in [m for m in dup.modifiers if m.type in {'SUBSURF', 'MULTIRES'}]:
                    dup.modifiers.remove(mod)
            wanted = base_name(obj.name)
            base = free_base(wanted, [obj, dup], used, s.suffix_style)
            renamed_base += base != wanted
            used.add(base.lower())
            sfx = dict(zip(("low", "high"), suffixes(s.suffix_style)))
            targets += [(obj, f"{base}_{sfx[src]}"), (dup, f"{base}_{sfx[dst]}")]
            copies.append(dup)

        # Copies must point at copies: parents, cutters, mirror objects and so on.
        mapping = dict(zip(originals, copies))
        cut_objs, cut_colls = collect_cutters(originals) if self.copy_cutters else ([], [])
        copy_cutters(cut_objs, cut_colls, dst, mapping)
        for dup in copies + [mapping[c] for c in cut_objs]:
            remap_references(dup, mapping)
        apply_names(targets)
        if src_coll:
            for obj in originals:
                move_to_collection(obj, src_coll)

        for obj in context.selected_objects:
            obj.select_set(False)
        for dup in copies:
            dup.select_set(True)
        context.view_layer.objects.active = copies[-1]

        msg = f"Created {len(copies)} _{dst} cop{'y' if len(copies) == 1 else 'ies'}"
        if dst_coll:
            msg += f" in '{dst_coll.name}'"
        if src_coll:
            msg += f", originals in '{src_coll.name}'"
        if cut_objs:
            msg += f", {len(cut_objs)} cutter(s)"
        if renamed_base:
            msg += f", {renamed_base} name(s) were taken so got a _01 base"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class PREFLIGHT_OT_separate_cutters(MeshOperator, bpy.types.Operator):
    """Give the selected objects their own copies of any cutters or helpers (mirror
empties, array offsets...) they share with other objects, so editing one side doesn't
change the other"""
    bl_idname = "preflight.separate_cutters"
    bl_label = "Separate Cutters"

    def execute(self, context):
        cutters = all_cutters()
        selected = [o for o in selected_meshes(context) if o not in cutters]
        if not selected:
            self.report({'WARNING'}, "Only boolean cutters are selected")
            return {'CANCELLED'}
        roles = {r for o in selected if (r := pair_role(o.name))}
        label = roles.pop() if len(roles) == 1 else "copy"
        copies = separate_dependencies(selected, label)
        if not copies:
            self.report({'INFO'}, "No shared cutters")
        else:
            self.report({'INFO'}, f"Separated {len(copies)} shared cutter(s)/helper(s)")
        return {'FINISHED'}


class PREFLIGHT_OT_pair_selected(bpy.types.Operator):
    """Name already-made low and high(s) as a pair, using the active object's name.
The mesh with the fewest faces (after modifiers) becomes the low"""
    bl_idname = "preflight.pair_selected"
    bl_label = "Pair Selected"
    bl_options = {'REGISTER', 'UNDO'}

    base: StringProperty(
        name="Name",
        description="Base name for the pair. Empty uses the active object's name",
    )

    @classmethod
    def poll(cls, context):
        return len(selected_meshes(context)) >= 2

    def invoke(self, context, event):
        active = context.view_layer.objects.active
        self.base = base_name(active.name) if active else ""
        return self.execute(context)

    def execute(self, context):
        objects = selected_meshes(context)
        depsgraph = context.evaluated_depsgraph_get()
        objects.sort(key=lambda o: face_count(o, depsgraph))
        low, highs = objects[0], sorted(objects[1:], key=lambda o: o.name)
        wanted = self.base.strip() or base_name(low.name)

        unshared = unshare_pair_data([low], highs)
        style = context.scene.preflight.suffix_style
        base = free_base(wanted, objects, style=style)
        apply_names(pair_names(base, low, highs, style))
        msg = f"{low.name} + {len(highs)} high"
        if base != wanted:
            msg += f" ('{wanted}' is used elsewhere)"
        if unshared:
            msg += f", split {unshared} shared mesh(es)"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class PREFLIGHT_OT_sync_data_names(MeshOperator, bpy.types.Operator):
    """Rename each selected object's mesh data to match the object name"""
    bl_idname = "preflight.sync_data_names"
    bl_label = "Sync Mesh Names"

    make_single_user: BoolProperty(
        name="Split Shared Meshes",
        description="Give objects that share a mesh their own copy so every mesh can be named",
        default=False,
    )

    def execute(self, context):
        renamed = shared = blocked = 0
        for obj in selected_meshes(context):
            if obj.data.users > 1:
                if not self.make_single_user:
                    shared += 1
                    continue
                obj.data = obj.data.copy()
            if obj.data.name == obj.name:
                continue
            holder = bpy.data.meshes.get(obj.name)
            if holder is not None and holder is not obj.data:
                blocked += 1  # another mesh owns that name; leave it alone
                continue
            obj.data.name = obj.name
            renamed += 1
        msg = f"Renamed {renamed} mesh(es)"
        if blocked:
            msg += f", {blocked} name(s) already used by another mesh"
        if shared:
            msg += (f", skipped {shared} shared mesh(es) "
                    "(enable Split Shared Meshes in the redo panel)")
        self.report({'WARNING' if shared or blocked else 'INFO'}, msg)
        return {'FINISHED'}


class PREFLIGHT_OT_check_names(bpy.types.Operator):
    """Check low/high naming: missing partners, case mismatches, .001 suffixes,
mesh names out of sync and meshes shared between low and high"""
    bl_idname = "preflight.check_names"
    bl_label = "Check Names"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        s = context.scene.preflight
        return bool((s.low_collection and s.high_collection) or selected_meshes(context))

    def execute(self, context):
        from .issues import report_check, run_names_check
        issues, pair_count = run_names_check(context)
        report_check(self, context, issues, "Pre-Flight: Names",
                     f"Names look good ({pair_count} pair(s))")
        return {'FINISHED'}
