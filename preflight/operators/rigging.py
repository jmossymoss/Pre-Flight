# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Rigging and engine export operators."""

import math

import bpy
from mathutils import Vector

from ..core import export, rigging
from ..core.dependencies import collection_meshes
from ..utils import object_mode
from .issues import report_check, rig_check_armatures, run_rig_check

PARTS = {'MESH', 'EMPTY', 'CURVE', 'SURFACE', 'META', 'FONT'}


def _parts(context):
    return [o for o in context.selected_objects if o.type in PARTS]


def _rig(context, create_from=None):
    """The Rigging tab's rig, else a selected armature, else a new one (remembered)."""
    s = context.scene.preflight
    rig = s.rig_armature
    if rig is None or rig.name not in context.scene.objects:
        rig = next((o for o in context.selected_objects if o.type == 'ARMATURE'), None)
    if rig is None and create_from is not None:
        context.view_layer.update()
        rig = rigging.new_armature(context, rigging.rig_name(create_from),
                                   rigging.parts_base(create_from))
    if rig is not None:
        s.rig_armature = rig
    return rig


def _pivot(context):
    s = context.scene.preflight
    if s.rig_mirror_pivot == 'CURSOR':
        return context.scene.cursor.location.copy()
    if s.rig_mirror_pivot == 'ACTIVE' and context.active_object is not None:
        return context.active_object.matrix_world.translation.copy()
    return Vector((0.0, 0.0, 0.0))


class RigOperator:
    bl_options = {'REGISTER', 'UNDO'}


# --- Parts and bones ---

class PREFLIGHT_OT_mirror_parts(RigOperator, bpy.types.Operator):
    """Copy the selected parts to the other side with mirrored pivots (positive scale),
side names and their hierarchy, ready for mirrored bones"""
    bl_idname = "preflight.mirror_parts"
    bl_label = "Mirror Parts"

    @classmethod
    def poll(cls, context):
        return bool(_parts(context))

    def execute(self, context):
        s = context.scene.preflight
        parts = _parts(context)
        with object_mode(context):
            pairs, negative = rigging.mirror_objects(parts, s.rig_mirror_axis, _pivot(context),
                                                     s.rig_name_sides)
        for obj in context.selected_objects:
            obj.select_set(False)
        for _, copy in pairs:
            copy.select_set(True)
        if pairs:
            context.view_layer.objects.active = pairs[-1][1]
        note = f" ({len(negative)} kept a negative scale)" if negative else ""
        self.report({'INFO'}, f"Mirrored {len(pairs)} part(s){note}")
        return {'FINISHED'}


class PREFLIGHT_OT_bones_from_origins(RigOperator, bpy.types.Operator):
    """Add a bone at each selected part's origin, chained by the parts' hierarchy, and
bind each part to its bone"""
    bl_idname = "preflight.bones_from_origins"
    bl_label = "Bones from Origins"

    @classmethod
    def poll(cls, context):
        return bool(_parts(context))

    def execute(self, context):
        s = context.scene.preflight
        parts = _parts(context)
        with object_mode(context):
            rig = _rig(context, create_from=parts)
            rig, names = rigging.bones_from_origins(
                context, parts, rig, axis=s.rig_bone_axis, length=s.rig_bone_length,
                root=s.rig_root, aim_children=s.rig_aim_children, bind=s.rig_bind)
        if rigging.LAST_STRIPPED:
            self.report({'WARNING'}, f"{len(names)} bone(s) on '{rig.name}'. Re-oriented "
                        f"{', '.join(rigging.LAST_STRIPPED[:4])}: their drivers were removed, "
                        "run Wheel Spin / Link Gears again")
        else:
            self.report({'INFO'}, f"{len(names)} bone(s) on '{rig.name}'")
        return {'FINISHED'}


def _edit_hinge(context):
    """(object, centre, axis) from the Edit Mode selection, or raise ValueError."""
    obj = context.edit_object
    if obj is None or obj.type != 'MESH':
        raise ValueError("Select the hinge in a mesh's Edit Mode")
    points = rigging.selected_points(obj)
    if not points:
        raise ValueError("Select the hinge: two axle vertices, an edge ring or a face")
    centre, axis = rigging.hinge_from_points(points)
    return obj, centre, axis


def _back_to_edit(context, obj):
    context.view_layer.objects.active = obj
    obj.select_set(True)
    if obj.mode != 'EDIT':
        bpy.ops.object.mode_set(mode='EDIT')


class PREFLIGHT_OT_bone_from_hinge(RigOperator, bpy.types.Operator):
    """Add a bone on the selected hinge (two axle vertices, an edge ring or a face),
pointing along the hinge axis, and bind the part to it"""
    bl_idname = "preflight.bone_from_hinge"
    bl_label = "Bone from Hinge"

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def execute(self, context):
        s = context.scene.preflight
        try:
            obj, centre, axis = _edit_hinge(context)
        except ValueError as ex:
            self.report({'ERROR'}, str(ex))
            return {'CANCELLED'}
        bpy.ops.object.mode_set(mode='OBJECT')
        rig = _rig(context, create_from=[obj])
        bone = rigging.bone_on_hinge(context, rig, obj, centre, axis, root=s.rig_root)
        rigging.bind_parts(rig, {obj: bone}, s.rig_bind)
        _back_to_edit(context, obj)
        self.report({'INFO'}, f"Bone '{bone}' on the hinge of '{obj.name}'")
        return {'FINISHED'}


class PREFLIGHT_OT_origin_to_hinge(RigOperator, bpy.types.Operator):
    """Move the part's origin onto the selected hinge with its local Z along the hinge
axis, without moving the mesh (so Bones from Origins lands on the hinge)"""
    bl_idname = "preflight.origin_to_hinge"
    bl_label = "Origin to Hinge"

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def execute(self, context):
        try:
            obj, centre, axis = _edit_hinge(context)
        except ValueError as ex:
            self.report({'ERROR'}, str(ex))
            return {'CANCELLED'}
        bpy.ops.object.mode_set(mode='OBJECT')
        rigging.origin_to_hinge(obj, centre, axis)
        _back_to_edit(context, obj)
        self.report({'INFO'}, f"Origin of '{obj.name}' on its hinge")
        return {'FINISHED'}


def _hinge_bones(context):
    """(rig, bone names): selected pose bones in Pose Mode, else the selected parts' bones."""
    if context.mode == 'POSE' and context.active_object is not None:
        rig = context.active_object
        return rig, [pb.name for pb in (context.selected_pose_bones or [])]
    rig = context.scene.preflight.rig_armature
    names = []
    for obj in context.selected_objects:
        armature, bone = rigging.bone_target(obj)
        if armature is not None and bone:
            rig = armature
            names.append(bone)
    return rig, names


class PREFLIGHT_OT_hinge_limits(RigOperator, bpy.types.Operator):
    """Make the selected bones (or the selected parts' bones) hinges: they turn around
one axis within the limits, with the other axes locked"""
    bl_idname = "preflight.hinge_limits"
    bl_label = "Hinge Limits"

    @classmethod
    def poll(cls, context):
        rig, names = _hinge_bones(context)
        return rig is not None and bool(names)

    def execute(self, context):
        s = context.scene.preflight
        rig, names = _hinge_bones(context)
        count = rigging.hinge_limits(rig, names, s.rig_limit_axis, s.rig_limit_min,
                                     s.rig_limit_max, s.rig_lock_location)
        self.report({'INFO'}, f"{count} hinge(s) limited on {s.rig_limit_axis}")
        return {'FINISHED'}


class PREFLIGHT_OT_ik_chain(RigOperator, bpy.types.Operator):
    """Add IK to the selected part's bone (or the active pose bone): a target at its end
and a pole, over the chain length. Hinge Limits on the chain carry over to the IK"""
    bl_idname = "preflight.ik_chain"
    bl_label = "IK Chain"

    @classmethod
    def poll(cls, context):
        rig, names = _hinge_bones(context)
        return rig is not None and len(names) == 1 or (
            context.mode == 'POSE' and context.active_pose_bone is not None)

    def execute(self, context):
        s = context.scene.preflight
        if context.mode == 'POSE' and context.active_pose_bone is not None:
            rig, tip = context.active_object, context.active_pose_bone.name
        else:
            rig, names = _hinge_bones(context)
            tip = names[0]
        mode = context.mode
        if mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        try:
            target, pole, chain = rigging.ik_chain(context, rig, tip, s.rig_ik_length,
                                                   s.rig_ik_pole, s.rig_ik_follow)
        except ValueError as ex:
            self.report({'ERROR'}, str(ex))
            return {'CANCELLED'}
        if mode == 'POSE':
            context.view_layer.objects.active = rig
            bpy.ops.object.mode_set(mode='POSE')
        self.report({'INFO'}, f"IK on {' > '.join(chain)} (target '{target}'"
                              + (f", pole '{pole}')" if pole else ")"))
        return {'FINISHED'}


class PREFLIGHT_OT_wheel_spin(RigOperator, bpy.types.Operator):
    """Make the selected wheels roll with the active object (the vehicle body): each
turns by the distance travelled divided by its radius"""
    bl_idname = "preflight.wheel_spin"
    bl_label = "Wheel Spin"

    @classmethod
    def poll(cls, context):
        parts = _parts(context)
        if context.mode != 'OBJECT':
            cls.poll_message_set("Object Mode: select the wheels, then the vehicle body")
            return False
        if len(parts) < 2 or context.active_object not in parts:
            cls.poll_message_set("Select the wheels, then the vehicle body (active)")
            return False
        return True

    def execute(self, context):
        s = context.scene.preflight
        body = context.active_object
        wheels = [o for o in _parts(context) if o != body]
        try:
            radii = rigging.wheel_spin(body, wheels, s.rig_wheel_travel_axis,
                                       s.rig_wheel_spin_axis, s.rig_wheel_reverse)
        except ValueError as ex:
            self.report({'ERROR'}, str(ex))
            return {'CANCELLED'}
        shown = ", ".join(f"{r:.3g}" for r in radii[:4])
        self.report({'INFO'}, f"{len(wheels)} wheel(s) roll with '{body.name}' (radius {shown})")
        return {'FINISHED'}


class PREFLIGHT_OT_drive_control(RigOperator, bpy.types.Operator):
    """Add a "drive" bone on the ground under the active vehicle body, between the root
and the body's bone: move and turn it (Pose Mode) to drive the vehicle. A body that
isn't rigged yet gets bones first, along with the other selected parts"""
    bl_idname = "preflight.drive_control"
    bl_label = "Drive Control"

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if obj is None or obj.type not in PARTS:
            cls.poll_message_set("Make the vehicle body the active object")
            return False
        return True

    def execute(self, context):
        s = context.scene.preflight
        body = context.active_object
        rig, bone = rigging.bone_target(body)
        made = ""
        if rig is None:
            parts = _parts(context) or [body]
            with object_mode(context):
                rig = _rig(context, create_from=parts)
                rig, names = rigging.bones_from_origins(
                    context, parts, rig, axis=s.rig_bone_axis, length=s.rig_bone_length,
                    root=True, aim_children=False, bind=s.rig_bind)
            bone = names[body]
            made = f" ({len(names)} part(s) boned first)"
        forward = rigging.object_axis(body, s.rig_wheel_travel_axis)
        with object_mode(context):
            name = rigging.add_drive_bone(context, rig, bone, forward)
        self.report({'INFO'}, f"'{name}' drives '{bone}' on '{rig.name}'{made}")
        return {'FINISHED'}


class PREFLIGHT_OT_bake_wheel_roll(RigOperator, bpy.types.Operator):
    """Key every Wheel Spin wheel over the scene's frame range from the distance its
vehicle really travels, so turning and reversing roll right, and remove the drivers"""
    bl_idname = "preflight.bake_wheel_roll"
    bl_label = "Bake Wheel Roll"

    def execute(self, context):
        scene = context.scene
        count = rigging.bake_wheel_roll(context, scene.frame_start, scene.frame_end)
        if not count:
            self.report({'WARNING'}, "No Wheel Spin drivers to bake")
            return {'CANCELLED'}
        self.report({'INFO'}, f"{count} wheel(s) keyed over frames "
                              f"{scene.frame_start}-{scene.frame_end}")
        return {'FINISHED'}


class PREFLIGHT_OT_clear_drivers(RigOperator, bpy.types.Operator):
    """Remove every driver on the selected objects (gears, wheels, chains, anything).
In Pose Mode, only the drivers on the selected bones"""
    bl_idname = "preflight.clear_drivers"
    bl_label = "Clear Drivers"

    @classmethod
    def poll(cls, context):
        return bool(context.selected_objects) or context.mode == 'POSE'

    def execute(self, context):
        bones = None
        objects = list(context.selected_objects)
        if context.mode == 'POSE' and context.active_object is not None:
            bones = [pb.name for pb in context.selected_pose_bones]
            if context.active_object not in objects:
                objects.append(context.active_object)
        count = rigging.clear_drivers(objects, bones)
        if not count:
            self.report({'WARNING'}, "No drivers on the selection")
            return {'CANCELLED'}
        where = f"{len(bones)} bone(s)" if bones is not None else f"{len(objects)} object(s)"
        self.report({'INFO'}, f"{count} driver(s) removed from {where}")
        return {'FINISHED'}


class PREFLIGHT_OT_control_shape(RigOperator, bpy.types.Operator):
    """Give the selected bones (Pose Mode) this control shape, facing along the chosen
axis. Set Up Controls keeps shapes set here"""
    bl_idname = "preflight.control_shape"
    bl_label = "Apply Shape"

    shape: bpy.props.EnumProperty(name="Shape", items=rigging.SHAPE_ITEMS)
    axis: bpy.props.EnumProperty(
        name="Axis", items=[('AUTO', "Auto", ""), ('X', "X", ""), ('Y', "Y", ""), ('Z', "Z", "")])
    size: bpy.props.FloatProperty(name="Size", min=0.0, subtype='DISTANCE', unit='LENGTH',
                                  description="0: sized to the part")
    line: bpy.props.BoolProperty(name="Bone Line", default=True,
                                 description="Draw the bone too, inside the shape")

    @classmethod
    def poll(cls, context):
        if context.mode != 'POSE' or not context.selected_pose_bones:
            cls.poll_message_set("Pose Mode: select the bones to shape")
            return False
        return True

    def invoke(self, context, event):
        s = context.scene.preflight
        self.shape, self.axis, self.size = s.rig_shape, s.rig_shape_axis, s.rig_shape_size
        self.line = s.rig_shape_line
        return self.execute(context)

    def execute(self, context):
        for pose_bone in context.selected_pose_bones:
            armature = pose_bone.id_data
            auto_shape, auto_axis, auto_size = rigging.auto_control(armature, pose_bone)
            axis = auto_axis if self.axis == 'AUTO' else self.axis
            size = self.size or rigging.control_size(armature, pose_bone, self.shape, axis)
            rigging.set_control(pose_bone, self.shape, axis, size, custom=True, line=self.line)
        self.report({'INFO'}, f"{len(context.selected_pose_bones)} bone(s): "
                              f"{rigging.CONTROL_SHAPES[self.shape][0]}")
        return {'FINISHED'}


class PREFLIGHT_OT_control_shape_reset(RigOperator, bpy.types.Operator):
    """Put the selected bones (Pose Mode) back to the shape Set Up Controls picks"""
    bl_idname = "preflight.control_shape_reset"
    bl_label = "Auto Shape"

    @classmethod
    def poll(cls, context):
        return context.mode == 'POSE' and bool(context.selected_pose_bones)

    def execute(self, context):
        line = context.scene.preflight.rig_shape_line
        for pose_bone in context.selected_pose_bones:
            rigging.set_control(pose_bone, *rigging.auto_control(pose_bone.id_data, pose_bone),
                                line=line)
        return {'FINISHED'}


class PREFLIGHT_OT_setup_controls(RigOperator, bpy.types.Operator):
    """Sort the rig's bones into coloured Controls, Mechanism and Deform collections and
give the controls ring shapes sized to their parts"""
    bl_idname = "preflight.setup_controls"
    bl_label = "Set Up Controls"

    @classmethod
    def poll(cls, context):
        return bool(rig_check_armatures(context))

    def execute(self, context):
        totals = {}
        for rig in rig_check_armatures(context):
            for name, count in rigging.setup_controls(
                    rig, line=context.scene.preflight.rig_shape_line).items():
                totals[name] = totals.get(name, 0) + count
        self.report({'INFO'}, ", ".join(f"{n} {c}" for n, c in totals.items()))
        return {'FINISHED'}


class PREFLIGHT_OT_rig_piston(RigOperator, bpy.types.Operator):
    """Rig a piston from two parts: the active part is the rod, the other the cylinder.
Each gets a bone on its pivot that tracks the other"""
    bl_idname = "preflight.rig_piston"
    bl_label = "Piston"

    @classmethod
    def poll(cls, context):
        parts = _parts(context)
        return len(parts) == 2 and context.active_object in parts

    def execute(self, context):
        s = context.scene.preflight
        rod = context.active_object
        cylinder = next(o for o in _parts(context) if o != rod)
        with object_mode(context):
            rig = _rig(context, create_from=[cylinder, rod])
            try:
                rigging.piston(context, rig, cylinder, rod, bind=s.rig_bind)
            except ValueError as ex:
                self.report({'ERROR'}, str(ex))
                return {'CANCELLED'}
        self.report({'INFO'}, f"Piston rigged on '{rig.name}'")
        return {'FINISHED'}


def _gear_chain(context):
    """((rig, path), wheels) when a chain or its path is selected, else (None, [])."""
    chain, wheels = None, []
    for o in context.selected_objects:
        found = rigging.chain_of(o)
        if found is not None:
            chain = chain or found
        elif o.type == 'MESH':
            wheels.append(o)
    return chain, (wheels if chain else [])


class PREFLIGHT_OT_link_gears(RigOperator, bpy.types.Operator):
    """Make the selected gears or sprockets turn with the active one, by their size
ratio (their bones, when they're rigged). With a chain or track (or its path) in the
selection, the selected wheels turn with the chain instead"""
    bl_idname = "preflight.link_gears"
    bl_label = "Link Gears"

    @classmethod
    def poll(cls, context):
        chain, wheels = _gear_chain(context)
        if chain is not None:
            if not wheels:
                cls.poll_message_set("Select the wheels to turn with the chain too")
                return False
            return True
        parts = _parts(context)
        if len(parts) < 2 or context.active_object not in parts:
            cls.poll_message_set("Select the gears, the driving one active (or a chain "
                                 "and the wheels it goes round)")
            return False
        return True

    def execute(self, context):
        s = context.scene.preflight
        chain, wheels = _gear_chain(context)
        if chain is not None:
            rig, curve = chain
            turned, skipped = [], []
            lead = ""
            if context.active_object in wheels:
                # The active wheel drives (as in gear mode): it slides the chain.
                driver = context.active_object
                rigging.drive_chain_by(rig, curve, driver, s.rig_gear_axis)
                wheels = [w for w in wheels if w is not driver]
                lead = f"'{driver.name}' drives the chain; "
            elif rigging._chain_source(rig, curve) is None:
                lead = "nothing drives the chain yet (make a wheel active to); "
            for wheel in wheels:
                try:
                    rigging.drive_from_chain(rig, curve, wheel, s.rig_gear_axis)
                    turned.append(wheel.name)
                except ValueError:
                    skipped.append(wheel.name)
            note = f" ({', '.join(skipped)} drives it)" if skipped else ""
            self.report({'WARNING'} if lead.startswith("nothing") else {'INFO'},
                        f"{lead}{len(turned)} wheel(s) turn with the chain on "
                        f"'{curve.name}'{note}")
            return {'FINISHED'}
        driver = context.active_object
        driven = [o for o in _parts(context) if o != driver]
        ratios = [rigging.link_rotation(driver, o, s.rig_gear_axis, s.rig_gear_mode)
                  for o in driven]
        shown = ", ".join(f"{r:+.2f}" for r in ratios[:4])
        self.report({'INFO'}, f"{len(driven)} part(s) follow '{driver.name}' ({shown})")
        return {'FINISHED'}


def _chain_path(context):
    """The chain's path: the active object when it's a curve or an edge loop, else the
    one curve in the selection (so the link can be the active object)."""
    active = context.active_object
    if active is not None and (active.type == 'CURVE' or rigging.is_edge_loop(active)):
        return active
    curves = [o for o in context.selected_objects if o.type == 'CURVE']
    return curves[0] if len(curves) == 1 else None


class PREFLIGHT_OT_path_around_wheels(RigOperator, bpy.types.Operator):
    """Wrap a closed path round the selected wheels the way a chain or belt goes: arcs
round each wheel joined by straight runs. Then add the link and press Chain Along Curve"""
    bl_idname = "preflight.path_around_wheels"
    bl_label = "Path Around Wheels"

    @classmethod
    def poll(cls, context):
        if context.mode != 'OBJECT':
            cls.poll_message_set("Object Mode: select the wheels")
            return False
        if sum(1 for o in context.selected_objects if o.type == 'MESH') < 2:
            cls.poll_message_set("Select two or more wheels (sprockets, pulleys, idlers)")
            return False
        return True

    def execute(self, context):
        wheels = [o for o in context.selected_objects if o.type == 'MESH']
        active = context.active_object if context.active_object in wheels else wheels[0]
        try:
            s = context.scene.preflight
            curve = rigging.path_around(wheels, offset=s.rig_path_offset,
                                        name=f"{active.name}_Path", axis=s.rig_wheel_spin_axis)
        except ValueError as ex:
            self.report({'ERROR'}, str(ex))
            return {'CANCELLED'}
        # Leave the wheels selected with the path: shift-click the link and Chain Along
        # Curve has everything (the sprocket is picked from the wheels).
        curve.select_set(True)
        context.view_layer.objects.active = curve
        self.report({'INFO'}, f"'{curve.name}' round {len(wheels)} wheels: shift-click the "
                              "link, then Chain Along Curve")
        return {'FINISHED'}


class PREFLIGHT_OT_calculate_links(RigOperator, bpy.types.Operator):
    """Set Links to the whole number of links that fills the path most exactly, measured
from the link's bounding box along Link Axis"""
    bl_idname = "preflight.calculate_links"
    bl_label = "Calculate Links"

    @classmethod
    def poll(cls, context):
        return PREFLIGHT_OT_chain_along_curve.poll(context)

    def execute(self, context):
        s = context.scene.preflight
        path = _chain_path(context)
        meshes = [o for o in context.selected_objects if o.type == 'MESH' and o is not path]
        link = rigging.chain_parts(meshes)[0]
        length = (rigging.curve_length(path) if path.type == 'CURVE'
                  else rigging.loop_length(path))
        pitch = rigging.link_pitch(link, s.rig_chain_axis)
        count = max(int(round(length / pitch)), 2)
        s.rig_chain_count = count
        stretch = (length / count) / pitch - 1.0
        fit = ("an exact fit" if abs(stretch) < 5e-4 else
               f"each link {abs(stretch) * 100:.1f}% {'apart' if stretch > 0 else 'squeezed'}")
        self.report({'INFO'}, f"{count} links of {pitch:.4g} m round {length:.4g} m: {fit}")
        return {'FINISHED'}


class PREFLIGHT_OT_chain_along_curve(RigOperator, bpy.types.Operator):
    """Make a chain or track: copies of the selected link along the active path (a
curve, or a mesh that is just an edge loop), one bone each, so it exports to game
engines. Select a sprocket too to drive it"""
    bl_idname = "preflight.chain_along_curve"
    bl_label = "Chain Along Curve"

    @classmethod
    def poll(cls, context):
        path = _chain_path(context)
        if path is None:
            cls.poll_message_set("Select a path: a curve (or make a mesh that is only an "
                                 "edge loop active)")
            return False
        if not [o for o in context.selected_objects if o.type == 'MESH' and o is not path]:
            cls.poll_message_set("Select the link mesh (and a sprocket to drive it) along "
                                 "with the path")
            return False
        return True

    def execute(self, context):
        s = context.scene.preflight
        path = _chain_path(context)
        meshes = [o for o in context.selected_objects if o.type == 'MESH' and o is not path]
        link, sprocket, ignored = rigging.chain_parts(meshes)
        with object_mode(context):
            if path.type != 'CURVE':
                path = rigging.curve_from_loop(path)
            rig = _rig(context, create_from=[link])
            rig, chain, count = rigging.chain_along_curve(
                context, link, path, rig, axis=s.rig_chain_axis,
                count=s.rig_chain_count, sprocket=sprocket,
                instances=s.rig_chain_instances,
                keep_link=s.rig_chain_instances and s.rig_chain_keep_link)
        # Leave the new chain selected, so Roll Links works on it straight away.
        for o in context.selected_objects:
            o.select_set(False)
        chain.select_set(True)
        context.view_layer.objects.active = chain
        driven = f", driven by '{sprocket.name}'" if sprocket else ""
        left = f"; {', '.join(o.name for o in ignored)} left alone" if ignored else ""
        self.report({'INFO'}, f"{count} links on '{path.name}'{driven}{left}; "
                              "Roll Links turns them if they face the wrong way")
        return {'FINISHED'}


def _chains(context):
    """Chain meshes to roll: the selected ones, else those on the selected rigs."""
    chains = [o for o in context.selected_objects if rigging.chain_rig(o) is not None]
    if not chains:
        rigs = {o for o in context.selected_objects if o.type == 'ARMATURE'}
        if context.active_object is not None and context.active_object.type == 'ARMATURE':
            rigs.add(context.active_object)
        chains = [o for o in context.view_layer.objects if rigging.chain_rig(o) in rigs]
    # An instanced chain is its empty, however many of its links are selected.
    return list(dict.fromkeys(rigging.chain_holder(o) or o for o in chains))


class PREFLIGHT_OT_roll_links(RigOperator, bpy.types.Operator):
    """Turn every link of the selected chains about the path, each about its own length
(stand them up, lay them flat or flip them over). Adjust Last Operation takes any angle"""
    bl_idname = "preflight.roll_links"
    bl_label = "Roll Links"

    angle: bpy.props.FloatProperty(
        name="Angle", subtype='ANGLE', default=math.pi / 2, soft_min=-math.pi, soft_max=math.pi,
        description="How far to turn each link about the path")

    @classmethod
    def poll(cls, context):
        if not _chains(context):
            cls.poll_message_set("Select a chain made by Chain Along Curve (or its rig)")
            return False
        return True

    def execute(self, context):
        chains = _chains(context)
        with object_mode(context):
            links = sum(rigging.roll_chain(c, self.angle) for c in chains)
        rolls = ", ".join(f"{c.name} at {c[rigging.CHAIN_ROLL]:.0f}°" for c in chains)
        self.report({'INFO'}, f"Rolled {links} links: {rolls}")
        return {'FINISHED'}


# --- Game export ---

class PREFLIGHT_OT_make_game_ready(RigOperator, bpy.types.Operator):
    """Prepare the rig for a game engine: one root bone, a safe rig name, parts skinned
rigidly instead of parented, and (optionally) motion baked and parts joined"""
    bl_idname = "preflight.make_game_ready"
    bl_label = "Make Game Ready"

    @classmethod
    def poll(cls, context):
        return bool(rig_check_armatures(context))

    def execute(self, context):
        s = context.scene.preflight
        done = []
        with object_mode(context):
            for rig in rig_check_armatures(context):
                done += rigging.make_game_ready(context, rig, join=s.rig_join, bake=s.rig_bake)
        self.report({'INFO'}, "Game ready: " + (", ".join(done) if done else "nothing to do"))
        return {'FINISHED'}


class PREFLIGHT_OT_check_rig(bpy.types.Operator):
    """Check the rig for what game engines reject or rigid parts get wrong (results in
Issues)"""
    bl_idname = "preflight.check_rig"
    bl_label = "Check Rig"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return bool(rig_check_armatures(context))

    def execute(self, context):
        issues = run_rig_check(context, rig_check_armatures(context))
        report_check(self, context, issues, "Check Rig", "Rig is ready for export")
        return {'FINISHED'}


class PREFLIGHT_OT_make_rigid(RigOperator, bpy.types.Operator):
    """Give every vertex of the selected skinned meshes full weight to its strongest
bone, so rigid parts stop bending"""
    bl_idname = "preflight.make_rigid"
    bl_label = "Make Rigid"

    @classmethod
    def poll(cls, context):
        return any(o.type == 'MESH' for o in context.selected_objects)

    def execute(self, context):
        changed = 0
        with object_mode(context):
            for obj in [o for o in context.selected_objects if o.type == 'MESH']:
                for mod in obj.modifiers:
                    if mod.type == 'ARMATURE' and mod.object is not None:
                        changed += rigging.make_rigid(obj, mod.object)
        self.report({'INFO'}, f"{changed} vertices made rigid")
        return {'FINISHED'}


# --- Engine copies ---

def _export_parts(context):
    """The selected meshes, else the Low collection's."""
    parts = [o for o in context.selected_objects if o.type == 'MESH']
    low = context.scene.preflight.low_collection
    if not parts and low is not None:
        parts = collection_meshes(low)
    return parts


class PREFLIGHT_OT_engine_copy(bpy.types.Operator):
    """Copy the selected lows (or the Low collection) for a game engine: modifiers
applied, _low removed from names, parts combined. The bake setup isn't touched"""
    bl_idname = "preflight.engine_copy"
    bl_label = "Make Engine Mesh"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(_export_parts(context))

    def execute(self, context):
        parts = _export_parts(context)
        with object_mode(context):
            copies = export.make_engine_copies(context, parts,
                                               context.scene.preflight.export_combine)
        names = ", ".join(o.name for o in copies[:3]) + ("..." if len(copies) > 3 else "")
        self.report({'INFO'}, f"{len(parts)} part(s) -> {len(copies)} engine mesh(es): {names}")
        return {'FINISHED'}


classes = (
    PREFLIGHT_OT_mirror_parts,
    PREFLIGHT_OT_bones_from_origins,
    PREFLIGHT_OT_bone_from_hinge,
    PREFLIGHT_OT_origin_to_hinge,
    PREFLIGHT_OT_hinge_limits,
    PREFLIGHT_OT_ik_chain,
    PREFLIGHT_OT_wheel_spin,
    PREFLIGHT_OT_bake_wheel_roll,
    PREFLIGHT_OT_drive_control,
    PREFLIGHT_OT_clear_drivers,
    PREFLIGHT_OT_control_shape,
    PREFLIGHT_OT_control_shape_reset,
    PREFLIGHT_OT_setup_controls,
    PREFLIGHT_OT_rig_piston,
    PREFLIGHT_OT_link_gears,
    PREFLIGHT_OT_path_around_wheels,
    PREFLIGHT_OT_calculate_links,
    PREFLIGHT_OT_chain_along_curve,
    PREFLIGHT_OT_roll_links,
    PREFLIGHT_OT_make_game_ready,
    PREFLIGHT_OT_check_rig,
    PREFLIGHT_OT_make_rigid,
    PREFLIGHT_OT_engine_copy,
)
