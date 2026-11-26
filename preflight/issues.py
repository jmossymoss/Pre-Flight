# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""The persistent issue list.

Results live on the WindowManager (session state). Each check source replaces only
its own rows. When results are stored, a cheap signature of every checked object is
kept (no per-vertex data); a depsgraph handler marks the list stale when a watched
object's geometry or signature changes in Object Mode.

ObjectCache keeps Validate's per-object results until the object changes, so a
re-check only reads edited objects.
"""

import json
import re

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, StringProperty

from .core.checks import Issue, sort_issues
from .utils import tag_redraw

SOURCES = [('MESH', "Mesh", "From Validate"), ('NAMES', "Names", "From Check Names"),
           ('PROJECTION', "Projection", "From Check Projection"),
           ('RIG', "Rig", "From Check Rig")]
SEVERITIES = [('ERROR', "Error", ""), ('WARNING', "Warning", ""), ('INFO', "Info", "")]
CHECKED = {'MESH': ("checked_mesh", "mesh_checked"),
           'NAMES': ("checked_names", "names_checked"),
           'PROJECTION': ("checked_projection", "projection_checked"),
           'RIG': ("checked_rig", "rig_checked")}

_state = {
    "watched": {},        # object pointer -> signature at check time
    "stale": False,
    "suppress_select": False,
    "flushing": False,    # ignore updates our own fixes left pending (see flush)
    "object_cache": {},   # object pointer -> (name, mesh pointer, signature, key, result)
}


class PreflightNameItem(bpy.types.PropertyGroup):
    """Just a name (bpy's built-in `name`): an object an issue or check refers to."""


class PreflightIssue(bpy.types.PropertyGroup):
    # `name` holds "heading message" so the list's built-in filter searches both.
    source: EnumProperty(items=SOURCES)
    severity: EnumProperty(items=SEVERITIES)
    heading: StringProperty()
    message: StringProperty()
    objects: CollectionProperty(type=PreflightNameItem)
    select_kind: StringProperty()
    fix_kind: StringProperty()
    fix_data: StringProperty()
    select_data: StringProperty()
    ignored: BoolProperty()  # marked as intended by the user (see ignore_key)

    def to_issue(self):
        return Issue(self.source, self.severity, self.heading, self.message,
                     tuple(o.name for o in self.objects), self.select_kind,
                     self.fix_kind, self.fix_data, self.select_data)

    def live_objects(self):
        """The issue's objects that still exist under the same name."""
        return [o for o in (bpy.data.objects.get(n.name) for n in self.objects) if o]


# --- Ignored issues ---

_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def ignore_key(source, objects, message, kind=""):
    """The key an ignore is stored under: source, objects and kind of problem.

    `kind` is the select/fix kind; without one, the message with its numbers blanked
    is used, so an issue stays ignored when only its count changes.
    """
    what = kind or _NUMBER.sub('#', message)
    return f"{source}|{','.join(sorted(objects))}|{what}"


def item_key(item):
    kind = f"{item.select_kind}/{item.fix_kind}" if item.select_kind or item.fix_kind else ""
    return ignore_key(item.source, [o.name for o in item.objects], item.message, kind)


def ignored_keys(scene):
    return {i.name for i in scene.preflight.ignored_issues}


def set_ignored(context, item, ignored):
    """Mark `item` ignored or not. Ignores are stored on the scene, so they're saved."""
    store = context.scene.preflight.ignored_issues
    key = item_key(item)
    index = store.find(key)
    if ignored and index < 0:
        store.add().name = key
    elif not ignored and index >= 0:
        store.remove(index)
    item.ignored = ignored
    tag_redraw(context)


# --- Signatures and staleness ---

def signature(obj):
    """A cheap hash of what the checks look at for `obj`.

    No per-vertex data: geometry edits are caught by the depsgraph's geometry flag.
    """
    me = obj.data
    mesh = None
    if isinstance(me, bpy.types.Mesh):
        uv = me.uv_layers.active
        mesh = (me.name, me.users, len(me.vertices), len(me.edges), len(me.polygons),
                tuple(l.name for l in me.uv_layers), uv.name if uv else "",
                me.has_custom_normals)
    return hash((obj.name, tuple(map(tuple, obj.matrix_world)),
                 obj.active_material.name if obj.active_material else "",
                 tuple((m.name, m.type, m.show_viewport, getattr(m, "offset_u", 0),
                        getattr(m, "offset_v", 0),
                        getattr(m, "node_group", None) and m.node_group.name)
                       for m in obj.modifiers),
                 tuple(c.name for c in obj.users_collection), mesh))


@persistent
def on_depsgraph_update(scene, depsgraph):
    cache = _state["object_cache"]
    if cache:
        for update in depsgraph.updates:
            if isinstance(update.id, bpy.types.Object) and (
                    update.is_updated_geometry or update.is_updated_transform):
                cache.pop(update.id.original.as_pointer(), None)
    watched = _state["watched"]
    if not watched or _state["stale"] or _state["flushing"]:
        return
    for update in depsgraph.updates:
        obj = update.id.original if isinstance(update.id, bpy.types.Object) else None
        if obj is None:
            continue
        old = watched.get(obj.as_pointer())
        if old is None or obj.mode != 'OBJECT':
            continue
        if update.is_updated_geometry or signature(obj) != old:
            _state["stale"] = True
            tag_redraw()
            return


@persistent
def on_load_post(*_args):
    _state["watched"].clear()
    _state["object_cache"].clear()
    _state["stale"] = False
    wm = bpy.context.window_manager
    if wm and hasattr(wm, "preflight"):
        props = wm.preflight
        props.issues.clear()
        for coll, flag in CHECKED.values():
            getattr(props, coll).clear()
            setattr(props, flag, False)
        restore_saved(bpy.context)


SAVED_PROP = "preflight_saved_issues"  # on the scene: the issue list as JSON


@persistent
def on_save_pre(*_args):
    """Keep the issue list in the file, so reopening it shows the last results."""
    wm = bpy.context.window_manager
    if not wm or not hasattr(wm, "preflight"):
        return
    props = wm.preflight
    for scene in bpy.data.scenes:
        if SAVED_PROP in scene:
            del scene[SAVED_PROP]
    scene = bpy.data.scenes.get(props.checked_scene)
    if scene is None or not any_checked(props):
        return
    rows = [[i.source, i.severity, i.heading, i.message, [o.name for o in i.objects],
             i.select_kind, i.fix_kind, i.fix_data, i.select_data] for i in props.issues]
    checked = {source: [o.name for o in getattr(props, coll)]
               for source, (coll, flag) in CHECKED.items() if getattr(props, flag)}
    scene[SAVED_PROP] = json.dumps({"issues": rows, "checked": checked})


def restore_saved(context):
    """Bring back the issue list a file was saved with. Returns True if it had one."""
    for scene in bpy.data.scenes:
        raw = scene.get(SAVED_PROP)
        if not isinstance(raw, str):
            continue
        try:
            data = json.loads(raw)
            rows, checked = data["issues"], data["checked"]
        except (ValueError, KeyError, TypeError):
            continue
        props = context.window_manager.preflight
        ignored = ignored_keys(scene)
        props.issues.clear()
        for row in rows:
            source, severity, heading, message, objects, select, fix, fix_data, select_data = row
            item = props.issues.add()
            item.name = f"{heading} {message}"
            item.source, item.severity = source, severity
            item.heading, item.message = heading, message
            for name in objects:
                item.objects.add().name = name
            item.select_kind, item.fix_kind, item.fix_data = select, fix, fix_data
            item.select_data = select_data
            item.ignored = item_key(item) in ignored
        for source, names in checked.items():
            if source not in CHECKED:
                continue
            coll, flag = CHECKED[source]
            for name in names:
                getattr(props, coll).add().name = name
            setattr(props, flag, True)
        props.checked_scene = scene.name
        _state["suppress_select"] = True
        try:
            props.issue_index = 0
        finally:
            _state["suppress_select"] = False
        try:
            _rewatch(context, props)
        except (AttributeError, RuntimeError):
            pass
        return True
    return False


def any_checked(props):
    return any(getattr(props, flag) for _, flag in CHECKED.values())


def is_stale():
    return _state["stale"]


def mark_stale(context=None):
    """Flag the results as out of date (e.g. after UVs changed in Edit Mode)."""
    if _state["watched"]:
        _state["stale"] = True
        tag_redraw(context)


# --- Storing results ---

def flush(context):
    """Evaluate pending updates now, before a check runs or stores.

    A fix's own edits then reach the handler early: they don't mark the fresh
    results stale, and they still drop the edited objects' cached results.
    """
    _state["flushing"] = True
    try:
        context.view_layer.update()
    except RuntimeError:
        pass
    finally:
        _state["flushing"] = False


class ObjectCache:
    """Validate results per object, reused while the object is unchanged.

    Entries are dropped on geometry or transform updates, undo/redo, file load and
    forget(); get() also rejects an entry whose signature or key no longer matches.
    """

    def get(self, obj, key):
        entry = _state["object_cache"].get(obj.as_pointer())
        if entry is None:
            return None
        name, mesh, sig, old_key, result = entry
        if (name != obj.name or mesh != obj.data.as_pointer() or old_key != key
                or sig != signature(obj)):
            return None
        return result

    def put(self, obj, key, result):
        _state["object_cache"][obj.as_pointer()] = (
            obj.name, obj.data.as_pointer(), signature(obj), key, result)


def forget(objects):
    """Drop the cached Validate results of `objects`."""
    cache = _state["object_cache"]
    for obj in objects:
        try:
            cache.pop(obj.as_pointer(), None)
        except ReferenceError:
            pass


@persistent
def on_undo_redo(*_args):
    _state["object_cache"].clear()


def _rewatch(context, props):
    flush(context)
    names = {o.name for coll, _ in CHECKED.values() for o in getattr(props, coll)}
    for item in props.issues:
        names.update(o.name for o in item.objects)
    watched = {}
    for name in names:
        obj = bpy.data.objects.get(name)
        if obj is not None:
            watched[obj.as_pointer()] = signature(obj)
    _state["watched"] = watched
    _state["stale"] = False


def store(context, source, issues, checked):
    """Replace the rows from `source` with `issues`.

    `checked` is the objects the check ran on.
    """
    props = context.window_manager.preflight
    keep = [item.to_issue() for item in props.issues if item.source != source]
    ignored = ignored_keys(context.scene)
    props.issues.clear()
    for issue in sort_issues(keep + list(issues)):
        item = props.issues.add()
        item.name = f"{issue.heading} {issue.message}"
        item.source, item.severity = issue.source, issue.severity
        item.heading, item.message = issue.heading, issue.message
        for name in issue.objects:
            item.objects.add().name = name
        item.select_kind, item.fix_kind, item.fix_data = issue.select, issue.fix, issue.fix_data
        item.select_data = issue.select_data
        item.ignored = item_key(item) in ignored

    props.checked_scene = context.scene.name
    coll, flag = CHECKED[source]
    target = getattr(props, coll)
    target.clear()
    for obj in checked:
        target.add().name = obj.name
    setattr(props, flag, True)

    _state["suppress_select"] = True
    try:
        props.issue_index = min(props.issue_index, len(props.issues) - 1) if props.issues else 0
    finally:
        _state["suppress_select"] = False
    _rewatch(context, props)
    tag_redraw(context)


def clear(context):
    props = context.window_manager.preflight
    props.issues.clear()
    for coll, flag in CHECKED.values():
        getattr(props, coll).clear()
        setattr(props, flag, False)
    _state["watched"].clear()
    _state["object_cache"].clear()
    _state["stale"] = False
    tag_redraw(context)


def counts(props):
    """(errors, warnings, info), not counting ignored issues."""
    out = {'ERROR': 0, 'WARNING': 0, 'INFO': 0}
    for i in props.issues:
        if not i.ignored:
            out[i.severity] += 1
    return out['ERROR'], out['WARNING'], out['INFO']


# --- Selecting an issue's objects ---

def select_objects(context, objects):
    """Select the selectable ones among `objects` and return them."""
    view_layer = context.view_layer
    if context.object and context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    # Use the view layer, not context.selected_objects or a plain visible_get(): in
    # local view those only see objects inside the current isolation.
    for obj in list(view_layer.objects.selected):
        obj.select_set(False)
    with context.temp_override(area=None, region=None):
        visible = {o.name for o in objects
                   if o.name in view_layer.objects and o.visible_get(view_layer=view_layer)}
    picked = []
    for obj in objects:
        if obj.name in visible:
            obj.select_set(True)
            picked.append(obj)
    if picked:
        view_layer.objects.active = picked[0]
    return picked


def _view3d(context):
    """(window, area, region) of the 3D view to focus, or None without a window.

    Prefers the area the panel is drawn in, then the window's first 3D view.
    """
    win = context.window
    if win is None:
        return None
    areas = [context.area] if context.area and context.area.type == 'VIEW_3D' else []
    areas += [a for a in win.screen.areas if a.type == 'VIEW_3D']
    for area in areas:
        region = next((r for r in area.regions if r.type == 'WINDOW'), None)
        if region is not None:
            return win, area, region
    return None


def focus(context, isolate):
    """Frame the selected objects, or the selected elements in Edit Mode.

    With `isolate` in Object Mode, show only the selected objects in local view,
    replacing any earlier isolation. Returns False without a 3D view.
    """
    target = _view3d(context)
    if target is None:
        return False
    win, area, region = target
    space = area.spaces.active
    with context.temp_override(window=win, area=area, region=region):
        # Not context.selected_objects: in local view it misses objects outside it.
        if isolate and context.mode == 'OBJECT' and len(context.view_layer.objects.selected):
            if space.local_view:
                # Swap what the local view shows: leaving and re-entering it in one
                # click doesn't reliably take effect.
                for obj in context.view_layer.objects:
                    obj.local_view_set(space, obj.select_get())
                bpy.ops.view3d.view_selected()
            else:
                bpy.ops.view3d.localview(frame_selected=True)
        elif context.mode != 'EDIT_MESH' or not _frame_elements(context, space.region_3d):
            bpy.ops.view3d.view_selected()
    return True


def _frame_elements(context, rv3d):
    """Centre the view on the selected Edit Mode elements. False if none are selected.

    Unlike View Selected, it keeps about a third of the object in view, so a tiny
    selection is still shown in context.
    """
    import bmesh
    from mathutils import Vector
    pts, size = [], 0.0
    for obj in context.objects_in_mode_unique_data:
        if obj.type != 'MESH':
            continue
        mw = obj.matrix_world
        pts += [mw @ v.co for v in bmesh.from_edit_mesh(obj.data).verts if v.select]
        size = max(size, *obj.dimensions)
    if not pts:
        return False
    lo = Vector(min(p[i] for p in pts) for i in range(3))
    hi = Vector(max(p[i] for p in pts) for i in range(3))
    rv3d.view_location = (lo + hi) / 2
    rv3d.view_distance = max((hi - lo).length * 1.5, size * 0.35, 1e-3)
    return True


def on_index_changed(props, context):
    if _state["suppress_select"] or not (0 <= props.issue_index < len(props.issues)):
        return
    objects = props.issues[props.issue_index].live_objects()
    if objects:
        try:
            if select_objects(context, objects):
                from . import preferences
                focus(context, preferences.get(context).isolate_issues)
        except RuntimeError:
            pass  # e.g. another modal operator is running


classes = (PreflightNameItem, PreflightIssue)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.app.handlers.depsgraph_update_post.append(on_depsgraph_update)
    bpy.app.handlers.load_post.append(on_load_post)
    bpy.app.handlers.save_pre.append(on_save_pre)
    bpy.app.handlers.undo_post.append(on_undo_redo)
    bpy.app.handlers.redo_post.append(on_undo_redo)


def unregister():
    for lst, fn in ((bpy.app.handlers.depsgraph_update_post, on_depsgraph_update),
                    (bpy.app.handlers.load_post, on_load_post),
                    (bpy.app.handlers.save_pre, on_save_pre),
                    (bpy.app.handlers.undo_post, on_undo_redo),
                    (bpy.app.handlers.redo_post, on_undo_redo)):
        if fn in lst:
            lst.remove(fn)
    _state["watched"].clear()
    _state["object_cache"].clear()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
