# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""The Mechanisms list (select, remove), Rebuild Chain, limits from the pose and Test."""

import math

import bpy

from ..core import mechanisms, rigging
from ..utils import object_mode, tag_redraw


class PREFLIGHT_OT_mechanism_select(bpy.types.Operator):
    """Select the parts of this mechanism"""
    bl_idname = "preflight.mechanism_select"
    bl_label = "Select Mechanism"
    bl_options = {'REGISTER', 'UNDO'}

    names: bpy.props.StringProperty()

    def execute(self, context):
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        found = [context.view_layer.objects.get(n) for n in self.names.split("|")]
        found = [o for o in found if o is not None]
        if not found:
            self.report({'WARNING'}, "Those parts aren't in this view layer")
            return {'CANCELLED'}
        for obj in context.selected_objects:
            obj.select_set(False)
        for obj in found:
            obj.hide_set(False)
            obj.select_set(True)
        context.view_layer.objects.active = found[0]
        return {'FINISHED'}


class PREFLIGHT_OT_mechanism_remove(bpy.types.Operator):
    """Remove this mechanism: what the tool added (drivers, constraints, helper bones or
empties, a chain's links). The parts themselves stay"""
    bl_idname = "preflight.mechanism_remove"
    bl_label = "Remove Mechanism"
    bl_options = {'REGISTER', 'UNDO'}

    key: bpy.props.StringProperty()

    def execute(self, context):
        with object_mode(context):
            try:
                message = mechanisms.remove(context, self.key)
            except ValueError as ex:
                self.report({'ERROR'}, str(ex))
                return {'CANCELLED'}
        tag_redraw(context)
        self.report({'INFO'}, message)
        return {'FINISHED'}


def _selected_chains(context):
    chains = [o for o in context.selected_objects if rigging.chain_rig(o) is not None]
    return list(dict.fromkeys(rigging.chain_holder(o) or o for o in chains))


class PREFLIGHT_OT_rebuild_chain(bpy.types.Operator):
    """Build the selected chain again on its path with the Links and Instances settings
above, keeping its roll, what drives it and the wheels that turn with it"""
    bl_idname = "preflight.rebuild_chain"
    bl_label = "Rebuild Chain"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not _selected_chains(context):
            cls.poll_message_set("Select a chain made by Chain Along Curve")
            return False
        return True

    def execute(self, context):
        s = context.scene.preflight
        built = []
        with object_mode(context):
            for chain in _selected_chains(context):
                try:
                    new, count = mechanisms.rebuild_chain(
                        context, chain, count=s.rig_chain_count,
                        instances=s.rig_chain_instances,
                        keep_link=s.rig_chain_instances and s.rig_chain_keep_link)
                except ValueError as ex:
                    self.report({'ERROR'}, str(ex))
                    return {'CANCELLED'}
                built.append((new, count))
        for obj in context.selected_objects:
            obj.select_set(False)
        for new, _count in built:
            new.select_set(True)
        context.view_layer.objects.active = built[-1][0]
        self.report({'INFO'}, ", ".join(f"'{c.name}': {n} links" for c, n in built))
        return {'FINISHED'}


class PREFLIGHT_OT_limit_from_pose(bpy.types.Operator):
    """Set this limit to the angle the active part or bone is turned to now: pose it at
the end of its swing, then click"""
    bl_idname = "preflight.limit_from_pose"
    bl_label = "Limit from Pose"
    bl_options = {'REGISTER', 'UNDO'}

    which: bpy.props.EnumProperty(items=[('MIN', "Min", ""), ('MAX', "Max", "")])

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def execute(self, context):
        s = context.scene.preflight
        _owner, struct, name = mechanisms._struct_for(context)
        if struct.rotation_mode in {'QUATERNION', 'AXIS_ANGLE'}:
            angle = struct.matrix_basis.to_euler('XYZ')[rigging.AXES[s.rig_limit_axis]]
        else:
            angle = struct.rotation_euler[rigging.AXES[s.rig_limit_axis]]
        if self.which == 'MIN':
            s.rig_limit_min = angle
        else:
            s.rig_limit_max = angle
        self.report({'INFO'}, f"{self.which.title()} set to {math.degrees(angle):.1f} deg "
                              f"from '{name}' ({s.rig_limit_axis})")
        return {'FINISHED'}


class PREFLIGHT_OT_test_motion(bpy.types.Operator):
    """Play the active part's motion to see the rig work: a hinge swings through its
limits, a vehicle drives forward and back, anything else turns once (a driven part
turns what drives it). Everything goes back where it was; Esc or a click stops it"""
    bl_idname = "preflight.test_motion"
    bl_label = "Test Motion"
    bl_options = {'REGISTER'}

    seconds: bpy.props.FloatProperty(name="Seconds", default=3.0, min=0.5, max=20.0)

    _timer = None
    _motion = None
    _elapsed = 0.0

    @classmethod
    def poll(cls, context):
        if context.active_object is None:
            cls.poll_message_set("Make the part to test the active object")
            return False
        return True

    def invoke(self, context, event):
        s = context.scene.preflight
        try:
            self._motion = mechanisms.motion_for(context, s.rig_wheel_travel_axis,
                                                 s.rig_wheel_spin_axis)
        except ValueError as ex:
            self.report({'ERROR'}, str(ex))
            return {'CANCELLED'}
        self._elapsed = 0.0
        self._timer = context.window_manager.event_timer_add(1 / 30, window=context.window)
        context.window_manager.modal_handler_add(self)
        self.report({'INFO'}, f"Test: {self._motion.label} (Esc stops)")
        return {'RUNNING_MODAL'}

    def execute(self, context):
        # Without a window (scripts): pose the middle of the test, then put it back.
        s = context.scene.preflight
        try:
            motion = mechanisms.motion_for(context, s.rig_wheel_travel_axis,
                                           s.rig_wheel_spin_axis)
        except ValueError as ex:
            self.report({'ERROR'}, str(ex))
            return {'CANCELLED'}
        mechanisms.apply_motion(motion, 0.25)
        context.view_layer.update()
        mechanisms.end_motion(motion)
        self.report({'INFO'}, f"Test: {motion.label}")
        return {'FINISHED'}

    def _finish(self, context):
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        try:
            mechanisms.end_motion(self._motion)
        except ReferenceError:
            pass  # the part was deleted mid-test
        tag_redraw(context)

    def modal(self, context, event):
        if event.type in {'ESC', 'LEFTMOUSE', 'RIGHTMOUSE'}:
            self._finish(context)
            return {'CANCELLED'}
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}
        self._elapsed += 1 / 30
        try:
            if self._elapsed >= self.seconds:
                self._finish(context)
                return {'FINISHED'}
            mechanisms.apply_motion(self._motion, self._elapsed / self.seconds)
        except ReferenceError:
            self._finish(context)
            return {'CANCELLED'}
        tag_redraw(context)
        return {'RUNNING_MODAL'}


classes = (
    PREFLIGHT_OT_mechanism_select,
    PREFLIGHT_OT_mechanism_remove,
    PREFLIGHT_OT_rebuild_chain,
    PREFLIGHT_OT_limit_from_pose,
    PREFLIGHT_OT_test_motion,
)
