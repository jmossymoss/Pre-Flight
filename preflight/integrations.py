# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Optional integrations with UVPackmaster 4 and Zen UV.

Neither add-on is imported: availability is detected from their registered operators.
Each feature takes an engine ('AUTO' picks the best one installed) and falls back to
Blender's own tools. Settings Pre-Flight needs for a run are overridden on the add-on's
own properties for the duration of the call and then restored.
"""

import bmesh
import bpy
import numpy as np

UVPM_MODULE_HINT = "uvpackmaster4"
ZEN_MODULE_HINT = "ZenUV"

ENGINE_NAMES = {'UVPM': "UVPackmaster", 'ZEN': "Zen UV", 'BLENDER': "Blender"}

# UVPackmaster pack modes (its mode ids), offered in Pre-Flight's Packing box.
UVPM_PACK_MODES = [
    ('pack.single_tile', "Single Tile", "Pack everything into the 0-1 tile"),
    ('pack.groups_together', "Groups Together",
     "Pack into one tile, keeping each group's islands together"),
    ('pack.groups_to_tiles', "Groups To Tiles", "Each group gets its own tile(s)"),
    ('pack.groups_independently', "Groups Independently",
     "Each group packed on its own, overlapping the others"),
    ('pack.tiles', "Tiles", "Pack across several UDIM tiles"),
]

UVPM_PACK_TO_OTHERS = '1'  # UVPackmaster's PackOpType: selected islands around the others

_SELECT_DOMAINS = {".select_vert": 'POINT', ".select_edge": 'EDGE', ".select_poly": 'FACE',
                   ".uv_select_vert": 'CORNER', ".uv_select_edge": 'CORNER',
                   ".uv_select_face": 'FACE'}


# --- Detection ---

def _addon_prefs(hint):
    for name, addon in bpy.context.preferences.addons.items():
        if name.split(".")[-1] == hint:
            return addon.preferences
    return None


def _preflight_prefs(context):
    from . import preferences
    return preferences.get(context)


def uvpm_installed():
    return "uvpackmaster4" in dir(bpy.ops) and "pack" in dir(bpy.ops.uvpackmaster4)


def zen_installed():
    return "zenuv_pack" in dir(bpy.ops.uv)


def uvpm_ready():
    prefs = _addon_prefs(UVPM_MODULE_HINT)
    return uvpm_installed() and bool(getattr(prefs, "engine_initialized", False))


def enabled(context, which):
    """Installed and not switched off in Pre-Flight's preferences."""
    prefs = _preflight_prefs(context)
    if which == 'UVPM':
        return uvpm_ready() and (prefs is None or prefs.use_uvpackmaster)
    if which == 'ZEN':
        return zen_installed() and (prefs is None or prefs.use_zen_uv)
    return which == 'BLENDER'


def resolve(context, choice, order):
    """The engine to use: `choice` if it's usable, else the first usable in `order`."""
    if choice != 'AUTO' and enabled(context, choice):
        return choice
    return next((e for e in order if enabled(context, e)), None)


def pack_engine(context):
    return resolve(context, context.scene.preflight.pack_engine, ('UVPM', 'ZEN', 'BLENDER'))


def stack_engines(context):
    """Stack engines to try, in order (Auto tries each installed one)."""
    choice = context.scene.preflight.stack_engine
    if choice != 'AUTO':
        return [choice] if enabled(context, choice) else []
    return [e for e in ('UVPM', 'ZEN') if enabled(context, e)]


# --- Edit Mode ---

def edit_mode(context, objects, fn, select="ALL"):
    """Run fn() in Edit Mode on `objects` with UV sync on, then restore the mode,
    selection and UV sync. Returns fn's result, or raises RuntimeError.

    select: 'ALL' selects every face; 'IN_TILE' only faces whose UVs sit in the 0-1
    tile (so islands offset +1U for baking are left alone); 'KEEP' leaves the user's
    selection, UV sync and select mode as they are; a callable (obj, bm) -> None
    selects faces itself.
    """
    restore = enter_edit_mode(context, objects, select)
    try:
        return fn()
    finally:
        restore()


def _selection_attributes(mesh):
    """The hidden attributes holding vertex, edge, face and UV selection."""
    return [a for a in mesh.attributes if a.name.startswith(".") and a.data_type == 'BOOLEAN'
            and ("select" in a.name or a.name[:4] in (".vs.", ".es."))]


def _element_selection(mesh):
    """{attribute name: selection flags} in Object Mode, to restore after editing."""
    out = {}
    for attr in _selection_attributes(mesh):
        flags = np.zeros(len(attr.data), dtype=bool)
        attr.data.foreach_get("value", flags)
        out[attr.name] = flags
    return out


def _set_element_selection(mesh, saved):
    try:
        attrs = {a.name: a for a in _selection_attributes(mesh)}
    except ReferenceError:
        return
    for name in list(attrs):
        if name not in saved:  # selection that didn't exist before: none selected
            mesh.attributes.remove(attrs.pop(name))
    for name, flags in saved.items():
        attr = attrs.get(name) or mesh.attributes.get(name)
        if attr is None:
            try:
                attr = mesh.attributes.new(name, 'BOOLEAN', _SELECT_DOMAINS.get(name, 'POINT'))
            except (RuntimeError, TypeError):
                continue
        if len(attr.data) == len(flags):
            attr.data.foreach_set("value", flags)
    mesh.update()


def enter_edit_mode(context, objects, select="ALL"):
    """Enter Edit Mode on `objects` with UV sync on and faces selected (see edit_mode).

    Returns restore(), which puts the mode, selection and UV sync back. It may run
    later (after a modal operator), so it reads bpy.context rather than `context`.
    """
    view_layer = context.view_layer
    usable = [o for o in objects if o.type == 'MESH' and o.name in view_layer.objects
              and o.visible_get()]
    if not usable:
        raise RuntimeError(f"{', '.join(o.name for o in objects)}: hidden, can't edit")
    window = context.window
    prev_selected = list(view_layer.objects.selected)
    prev_active = view_layer.objects.active
    tool = context.scene.tool_settings
    prev_sync, prev_select_mode = tool.use_uv_select_sync, tuple(tool.mesh_select_mode)
    if context.object and context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    saved_selection = {o.data: _element_selection(o.data) for o in usable}
    for o in prev_selected:
        o.select_set(False)
    for o in usable:
        o.select_set(True)
    view_layer.objects.active = usable[0]

    def restore():
        override = {"window": window} if window is not None and window.screen else {}
        with bpy.context.temp_override(**override):
            ctx = bpy.context
            tool.use_uv_select_sync = prev_sync
            tool.mesh_select_mode = prev_select_mode
            if ctx.object and ctx.object.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            for mesh, saved in saved_selection.items():
                _set_element_selection(mesh, saved)
            for o in usable:
                try:
                    o.select_set(False)
                except ReferenceError:
                    pass
            for o in prev_selected:
                try:
                    if o.name in view_layer.objects:
                        o.select_set(True)
                except ReferenceError:
                    pass
            try:
                view_layer.objects.active = prev_active
            except ReferenceError:
                pass

    try:
        bpy.ops.object.mode_set(mode='EDIT')
        if select == "KEEP":
            return restore
        tool.use_uv_select_sync = True
        tool.mesh_select_mode = (False, False, True)
        for obj in usable:
            bm = bmesh.from_edit_mesh(obj.data)
            uv = bm.loops.layers.uv.active
            for f in bm.faces:
                if select == "ALL" or uv is None:
                    f.select = True
                elif select == "IN_TILE":
                    n = len(f.loops)
                    cx = sum(l[uv].uv.x for l in f.loops) / n
                    cy = sum(l[uv].uv.y for l in f.loops) / n
                    f.select = 0 <= cx <= 1 and 0 <= cy <= 1
            if callable(select):
                select(obj, bm)
            bm.select_flush_mode()
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    except Exception:
        restore()
        raise
    return restore


# --- Add-on settings ---

def _uvpm_main_props(context):
    """The UVPackmaster settings its operators use (the active set when it has several)."""
    scene_props = context.scene.uvpm4_props
    if getattr(scene_props, "main_prop_sets_enable", False):
        try:  # UVPackmaster's own lookup for the active settings set
            import sys
            pkg = next(n for n in context.preferences.addons.keys()
                       if n.split(".")[-1] == UVPM_MODULE_HINT)
            props = sys.modules[pkg + ".app_iface"].get_main_props(context)
            if props is not None:
                return props
        except Exception:  # its internals moved: use the default set
            pass
    return scene_props.default_main_props


def uvpm_settings(context):
    """UVPackmaster's settings, or None when it isn't installed and enabled."""
    if not enabled(context, 'UVPM') or not hasattr(context.scene, "uvpm4_props"):
        return None
    return _uvpm_main_props(context)


def zen_settings(context):
    """Zen UV's preferences, or None when it isn't installed and enabled."""
    return _addon_prefs(ZEN_MODULE_HINT) if enabled(context, 'ZEN') else None


class _Override:
    """Context manager that sets attributes on `target` and restores them on exit."""

    def __init__(self, target, **values):
        self.target, self.values, self.saved = target, values, {}

    def __enter__(self):
        # Save everything before setting anything: a linked setting (Zen UV's margin_px
        # rewrites margin) would otherwise be saved already changed.
        self.saved = {k: getattr(self.target, k) for k in self.values if hasattr(self.target, k)}
        for k in self.saved:
            setattr(self.target, k, self.values[k])

    def __exit__(self, *exc):
        # Restore in the order set: linked settings are listed driver first, so the
        # dependent one is restored last.
        for k, v in self.saved.items():
            setattr(self.target, k, v)


# --- Pack ---

def scripted_search_seconds(context):
    """Heuristic search time for packs that can't be stopped by hand, when it's 0."""
    return _preflight_prefs(context).scripted_search_seconds


def pack(context, objects, select="IN_TILE", to_others=False):
    """Pack the 0-1 tile of `objects` together. Returns the engine used.

    Texture sets with different map sizes (per material) pack separately, each with
    its own padding; those sharing a size pack together as one layout.
    Raises RuntimeError when the engine reports a failure.
    """
    engine = None
    for ms, group in _size_groups(context, objects):
        engine = _pack(context, group, ms, select, to_others)
    return engine


def _uvpm_pack_args(context, to_others):
    """UVPackmaster pack operator arguments: Pre-Flight's pack mode, Pack To Others."""
    args = {"mode_id": context.scene.preflight.uvpm_pack_mode}
    if to_others:
        args["pack_op_type"] = UVPM_PACK_TO_OTHERS
    return args


def _size_groups(context, objects):
    """[(map size, objects)]: objects whose texture sets share a map size."""
    from .core import mapsize
    s = context.scene.preflight
    groups = {}
    for obj in objects:
        ms = mapsize.of_object(s, obj)
        groups.setdefault(mapsize.map_size(ms), (ms, []))[1].append(obj)
    return list(groups.values())


def _uvpm_overrides(context, mp, ms, interactive=False):
    """UVPackmaster settings for one pack at map size `ms` (restored afterwards).

    Packing options come from Pre-Flight's own settings, not UVPackmaster's panel.
    """
    from .core import mapsize
    s = context.scene.preflight
    overrides = {}
    if s.pack_use_padding:
        overrides.update(pixel_margin_enable=True, pixel_margin=mapsize.padding(ms),
                         pixel_margin_tex_size=mapsize.padding_size(ms))
    time = s.uvpm_heuristic_time
    if not interactive and s.uvpm_heuristic and not time:
        # Time 0 means "search until stopped", which UVPackmaster only allows when
        # run interactively: give a scripted run a limit instead.
        time = scripted_search_seconds(context)
    overrides.update(rotation_enable=s.uvpm_rotate, rotation_step=s.uvpm_rotation_step,
                     flipping_enable=s.uvpm_flip, normalize_scale=s.uvpm_normalize_scale,
                     heuristic_enable=s.uvpm_heuristic, heuristic_search_time=time,
                     precision=s.uvpm_precision)
    if not mapsize.is_square(ms) and s.uvpm_rotate:
        # UVs on a non-square map are stretched to its ratio, so a 90 degree turn would
        # distort an island's texels: allow 180 degree turns only.
        overrides.update(rotation_step=180, pre_rotation_disable=True)
    overrides["lock_overlapping_enable"] = s.pack_keep_stacks
    if s.pack_keep_stacks:
        overrides["lock_overlapping_mode"] = '2'  # Exact
    return overrides


def _uvpm_running(window):
    try:
        return any("uvpackmaster4" in op.bl_idname.lower() for op in window.modal_operators)
    except (ReferenceError, AttributeError):
        return False


def pack_interactive(context, objects, on_done, select="IN_TILE", to_others=False):
    """Start UVPackmaster's pack as its own button does: modal, with viewport progress,
    Esc to stop and no time limit needed for the heuristic search.

    Edit Mode, selection and UV sync stay set up while it runs; once it finishes they
    are restored and on_done(error or None) is called. Returns False without starting
    when that isn't possible: another packer, no window, or texture sets of different
    map sizes (those pack one after another, so not interactively).
    """
    window = context.window
    if (pack_engine(context) != 'UVPM' or window is None
            or not hasattr(window, "modal_operators") or _uvpm_running(window)):
        return False
    groups = _size_groups(context, objects)
    if len(groups) != 1:
        return False
    ms, group = groups[0]
    mp = _uvpm_main_props(context)
    prefs = _addon_prefs(UVPM_MODULE_HINT)
    restore = enter_edit_mode(context, group, select)
    try:
        # UVPackmaster reads its settings and the UVs when it starts, so the overrides
        # can be restored as soon as the call returns.
        with _Override(mp, **_uvpm_overrides(context, mp, ms, interactive=True)):
            bpy.ops.uvpackmaster4.pack('INVOKE_DEFAULT', **_uvpm_pack_args(context, to_others))
    except Exception:
        restore()
        raise

    def finish():
        restore()
        code = getattr(prefs, "engine_retcode", 0)
        on_done(None if code in (0, None) else f"UVPackmaster stopped (code {code}): see its panel")

    if not _uvpm_running(window):
        finish()
        return True

    def tick():
        if _uvpm_running(window):
            return 0.25
        finish()
        return None

    bpy.app.timers.register(tick, first_interval=0.25)
    return True


def _pack(context, objects, ms, select="IN_TILE", to_others=False):
    """Pack `objects` for map size `ms` (mapsize.of). Returns the engine used.

    to_others: pack the selected islands into the space around the unselected ones,
    which stay put (UVPackmaster's Pack To Others), moving them off any they overlap.
    """
    from .core import mapsize
    s = context.scene.preflight
    engine = pack_engine(context)
    size = mapsize.padding_size(ms)  # the shorter side: both axes get at least the padding
    padding = mapsize.padding(ms)
    margin = padding / size
    square = mapsize.is_square(ms)  # non-square: no 90 degree turns (see _uvpm_overrides)

    if engine == 'UVPM':
        mp = _uvpm_main_props(context)
        overrides = _uvpm_overrides(context, mp, ms)

        def run():
            with _Override(mp, **overrides):
                bpy.ops.uvpackmaster4.pack('EXEC_DEFAULT', **_uvpm_pack_args(context, to_others))
            code = getattr(_addon_prefs(UVPM_MODULE_HINT), "engine_retcode", 0)
            if code not in (0, None):
                raise RuntimeError(f"UVPackmaster stopped (code {code}): see its panel")

        edit_mode(context, objects, run, select=select)
        return engine

    if engine == 'ZEN':
        zp = _addon_prefs(ZEN_MODULE_HINT)
        # margin_px first: setting it rewrites margin from Zen's texture size.
        overrides = dict(margin_px=padding, margin=margin) if s.pack_use_padding else {}
        if not square:
            overrides["rotateOnPack"] = False
        # Only the selected (0-1) islands, so copies offset +1U for baking stay put.
        overrides["packSelectedIslOnly"] = True

        def run():
            with _Override(zp, **overrides):
                result = bpy.ops.uv.zenuv_pack()
            if 'CANCELLED' in result:
                raise RuntimeError("Zen UV pack was cancelled: see its report")

        edit_mode(context, objects, run, select=select)
        return engine

    def run():
        try:
            bpy.ops.uv.pack_islands(udim_source='CLOSEST_UDIM', rotate=square,
                                    rotate_method='CARDINAL', margin_method='FRACTION',
                                    margin=margin)
        except TypeError:  # older option set
            bpy.ops.uv.pack_islands(rotate=square, margin=margin)

    edit_mode(context, objects, run, select=select)
    return 'BLENDER'


# --- Stack ---

def stack(context, objects):
    """Stack similar islands on top of each other. Returns the engine used.

    Tries each available stack engine in turn; raises RuntimeError listing why each
    one failed (e.g. Zen UV without its library).
    """
    reasons = []
    for engine in stack_engines(context):
        if engine == 'UVPM':
            def run():
                bpy.ops.uvpackmaster4.align_similar('EXEC_DEFAULT', align_op_type='0')
                code = getattr(_addon_prefs(UVPM_MODULE_HINT), "engine_retcode", 0)
                if code not in (0, None):
                    raise RuntimeError(f"UVPackmaster stopped (code {code})")

            try:
                edit_mode(context, objects, run, select="IN_TILE")
                return engine
            except RuntimeError as ex:
                reasons.append(f"UVPackmaster: {ex}")
        elif engine == 'ZEN':
            def run():
                return bpy.ops.uv.zenuv_stack_similar(selected=False)

            if 'CANCELLED' in edit_mode(context, objects, run, select="IN_TILE"):
                reasons.append("Zen UV: Stack Similar needs the Zen UV Library "
                               "(or 'Stack Move Only' in Zen UV's preferences)")
                continue
            return engine
    if not reasons:
        reasons.append("Stacking needs UVPackmaster or Zen UV")
    raise RuntimeError("; ".join(reasons))


# --- Texel density ---

def set_island_density(context, obj, face_island, island_ids, densities, target):
    """Scale the given islands to `target` px/m. Returns the engine used.

    Uses Zen UV's Set Texel Density when available, else scales each island's UVs
    about its centre by target / current density.
    """
    from .core import mapsize
    width, height = mapsize.map_size(mapsize.of_object(context.scene.preflight, obj))
    faces = np.nonzero(np.isin(face_island, island_ids))[0]
    if not len(faces):
        return None

    if enabled(context, 'ZEN') and context.scene.unit_settings.scale_length == 1.0:
        face_set = set(faces.tolist())

        def pick(o, bm):
            for f in bm.faces:
                f.select = f.index in face_set

        def run():
            return bpy.ops.uv.zenuv_set_texel_density(
                global_mode=False, td_value=float(target), units='m', set_mode='ISLAND',
                image_size_x=width, image_size_y=height)

        if 'CANCELLED' not in edit_mode(context, [obj], run, select=pick):
            return 'ZEN'

    mesh = obj.data
    uv_layer = mesh.uv_layers.active
    from .core.geometry import fetch
    uv = fetch(uv_layer.data, "uv", np.float64, 2)
    totals = fetch(mesh.polygons, "loop_total", np.int64)
    loop_island = np.asarray(face_island)[np.repeat(np.arange(len(mesh.polygons)), totals)]
    for isl, density in zip(island_ids, densities):
        loops = np.nonzero(loop_island == isl)[0]
        centre = uv[loops].mean(axis=0)
        uv[loops] = centre + (uv[loops] - centre) * (target / density)
    uv_layer.data.foreach_set("uv", uv.astype(np.float32).ravel())
    mesh.update()
    return 'BLENDER'
