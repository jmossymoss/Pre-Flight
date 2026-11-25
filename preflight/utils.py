# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Small helpers shared by operators and panels."""

import bpy

from .constants import AUTO_SMOOTH_NAMES

# Issue severity -> Blender icon. Errors use the stop sign so warnings keep the triangle.
SEVERITY_ICONS = {'ERROR': 'CANCEL', 'WARNING': 'ERROR', 'INFO': 'INFO'}


def selected_meshes(context):
    return [o for o in context.selected_objects if o.type == 'MESH']


def unique_meshes(objects):
    """Yield (obj, mesh) once per mesh datablock, skipping linked duplicates."""
    seen = set()
    for obj in objects:
        if obj.data.as_pointer() not in seen:
            seen.add(obj.data.as_pointer())
            yield obj, obj.data


class object_mode:
    """Context manager that leaves Edit Mode so mesh data writes aren't overwritten."""

    def __init__(self, context):
        self.context = context
        self.mode = context.object.mode if context.object else 'OBJECT'

    def __enter__(self):
        if self.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

    def __exit__(self, *exc):
        if self.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode=self.mode)


def is_auto_smooth_mod(mod):
    return (mod.type == 'NODES' and mod.node_group is not None
            and any(n in mod.node_group.name for n in AUTO_SMOOTH_NAMES))


def tag_redraw(context=None):
    screen = (context or bpy.context).screen
    if screen:
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


class MeshOperator:
    """Mixin for operators that need at least one selected mesh."""
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(selected_meshes(context))


def show_issues(operator, context, issues, title):
    """Show (heading, icon, message) issues in a popup, grouped by heading.

    Without a window (background mode) they are printed instead.
    """
    errors = sum(1 for _, icon, _ in issues if icon == 'ERROR')
    issues = sorted(issues, key=lambda i: (i[0].startswith("Texture set"), i[0].lower()))

    def draw(menu, _context):
        col = menu.layout.column()
        current = None
        for head, icon, msg in issues:
            if head != current:
                if current is not None:
                    col.separator()
                col.label(text=head,
                          icon='UV' if head.startswith("Texture set") else 'OBJECT_DATA')
                current = head
            col.label(text=msg, icon=SEVERITY_ICONS.get(icon, icon))

    if context.window and not bpy.app.background:
        context.window_manager.popup_menu(draw, title=f"{title}: {errors} error(s)",
                                          icon='CHECKMARK')
    else:
        for head, _, msg in issues:
            print(f"[{title}] {head}: {msg}")
    operator.report({'WARNING' if errors else 'INFO'},
                    f"{len(issues)} issue(s) found, {errors} error(s)")
