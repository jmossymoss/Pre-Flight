# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Explode low/high pairs apart and restore them."""

import bpy
import numpy as np
from mathutils import Matrix, Vector

from ..constants import EXPLODE_KEY
from ..core.dependencies import (_modifier_ids, collection_meshes, has_ancestor_in,
                                 object_dependencies, separate_dependencies)
from ..core.naming import pair_groups, pair_role, world_bounds


def location_basis(obj):
    """Matrix mapping a change in obj.location to the change in its world position."""
    if obj.parent is None:
        return Matrix.Identity(3)
    return (obj.parent.matrix_world @ obj.matrix_parent_inverse).to_3x3()


class PREFLIGHT_OT_explode(bpy.types.Operator):
    """Move each low/high pair apart so neighbouring parts do not project onto each other"""
    bl_idname = "preflight.explode"
    bl_label = "Explode"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        s = context.scene.preflight
        return s.low_collection is not None and s.high_collection is not None

    def execute(self, context):
        s = context.scene.preflight
        objects = set(collection_meshes(s.low_collection) + collection_meshes(s.high_collection))
        groups = pair_groups(objects)
        if not groups:
            self.report({'WARNING'}, "No <name>_low / <name>_high pairs: run Match & Rename first")
            return {'CANCELLED'}

        # Each pair moves with its dependencies (cutters, mirror objects...). A dependency
        # shared by several pairs either gets a parented copy per extra pair, or stays put.
        all_parts = set().union(*groups.values())

        def find_shared():
            deps = {base: [d for d in object_dependencies(objs) if d not in all_parts]
                    for base, objs in groups.items()}
            owners = {}
            for base, ds in deps.items():
                for d in ds:
                    owners.setdefault(d, set()).add(base)
            return deps, owners, {d for d, bases in owners.items() if len(bases) > 1}

        deps, owners, shared = find_shared()
        separated = 0
        if shared and s.explode_split_shared:
            extra_bases = sorted({b for d in shared for b in sorted(owners[d])[1:]})
            for base in extra_bases:
                objs = groups[base]

                def user_in_pair(dup, objs=objs):
                    # Parent to the part using the copy directly (low first), else the low.
                    for obj in sorted(objs, key=lambda o: pair_role(o.name) != "low"):
                        if dup in set(_modifier_ids(obj)):
                            return obj
                    lows = [o for o in objs if pair_role(o.name) == "low"]
                    return (lows or objs)[0]

                separated += len(separate_dependencies(objs, base, parent_to=user_in_pair))
            deps, owners, shared = find_shared()

        # Measure each pair at its original position, then lay the pairs end to end
        # along the axis so none overlap.
        ax = "XYZ".index(s.explode_axis)
        axis = Vector([1.0 if i == ax else 0.0 for i in range(3)])
        spans = {}
        for base, objs in groups.items():
            lo, hi = np.inf, -np.inf
            for obj in objs:
                b_lo, b_hi = world_bounds(obj)
                stored = obj.get(EXPLODE_KEY)
                shift = (np.array(location_basis(obj) @ (obj.location - Vector(stored)))
                         if stored is not None else 0)
                lo, hi = min(lo, (b_lo - shift)[ax]), max(hi, (b_hi - shift)[ax])
            spans[base] = (lo, hi)
        biggest = max(hi - lo for lo, hi in spans.values())
        gap = biggest * s.explode_spacing

        moved = carried = 0
        cursor = min(lo for lo, _ in spans.values())
        for base in sorted(groups):
            lo, hi = spans[base]
            offset = axis * (cursor - lo)
            cursor += (hi - lo) + gap
            movers = set(groups[base]) | {d for d in deps[base] if d not in shared}
            for obj in movers:
                if has_ancestor_in(obj, movers):
                    continue  # children follow their parent
                if EXPLODE_KEY not in obj:
                    obj[EXPLODE_KEY] = tuple(obj.location)
                # Only location changes, so rotation and scale stay bit-for-bit identical.
                obj.location = Vector(obj[EXPLODE_KEY]) + location_basis(obj).inverted() @ offset
                if obj in groups[base]:
                    moved += 1
                else:
                    carried += 1

        msg = f"Exploded {len(groups)} pair(s): {moved} part(s), {carried} cutter(s)/helper(s)"
        if separated:
            msg += f". Gave pairs their own copies of {separated} shared cutter(s)/helper(s)"
        if shared:
            names = ", ".join(sorted(d.name for d in shared)[:3])
            msg += (f". {len(shared)} shared by several pairs left in place ({names}): "
                    "enable Separate Shared or use Separate Cutters")
        self.report({'WARNING' if shared else 'INFO'}, msg)
        return {'FINISHED'}


class PREFLIGHT_OT_unexplode(bpy.types.Operator):
    """Put exploded objects back where they were"""
    bl_idname = "preflight.unexplode"
    bl_label = "Restore"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(EXPLODE_KEY in o for o in context.scene.objects)

    def execute(self, context):
        count = 0
        for obj in context.scene.objects:
            if EXPLODE_KEY in obj:
                obj.location = Vector(obj[EXPLODE_KEY])
                del obj[EXPLODE_KEY]
                count += 1
        self.report({'INFO'}, f"Restored {count} object(s)")
        return {'FINISHED'}
