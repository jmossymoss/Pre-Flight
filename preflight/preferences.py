# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Add-on preferences and the defaults new files start with."""

from types import SimpleNamespace

import bpy
from bpy.props import (BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty,
                       IntProperty, StringProperty)

from .core import bakers
from .core.naming import SUFFIX_STYLES

DEFAULT_TAB = "Pre-Flight"
MAP_SIZES = [(str(n), str(n), "") for n in (256, 512, 1024, 2048, 4096, 8192)]

_defaults = None  # stand-in for the preferences when the add-on isn't enabled


def _update_category(self, context):
    """Re-register the panels so they move to the new sidebar tab."""
    from .ui.panels import PANELS
    for panel in PANELS:
        if panel.is_registered:
            bpy.utils.unregister_class(panel)
    for panel in PANELS:
        panel.bl_category = self.category or DEFAULT_TAB
        bpy.utils.register_class(panel)


def _redraw(self, context):
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _section(layout, title, icon):
    box = layout.box()
    box.label(text=title, icon=icon)
    return box.column()


class PreflightPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__ or __name__

    # Interface
    category: StringProperty(
        name="Sidebar Tab",
        description="The sidebar tab Pre-Flight's panel sits in",
        default=DEFAULT_TAB, update=_update_category,
    )
    show_popup: BoolProperty(
        name="Results Popup",
        description="Also list Validate and Check Names results in a popup, not only in "
                    "the Issues tab",
        default=False,
    )

    # Issues
    isolate_issues: BoolProperty(
        name="Isolate Issue Objects",
        description="Clicking an issue shows only its objects (local view) and frames "
                    "them. Off: select and frame only. Numpad / leaves the isolation",
        default=True,
    )
    recheck_after_fix: BoolProperty(
        name="Re-check After Fixes",
        description="Run the checks again after a fix, so the list stays current. Off: "
                    "the list is marked out of date instead (faster on big scenes)",
        default=True,
    )

    # Packing
    scripted_search_seconds: IntProperty(
        name="Fix All Search Time",
        description="UVPackmaster's heuristic search time for packs that can't be "
                    "stopped by hand (Fix All), when the search time is set to 0",
        min=1, max=600, default=10,
    )

    # Textures
    auto_reload_interval: FloatProperty(
        name="Auto Reload Interval",
        description="Seconds between checks for re-exported textures while Auto Reload "
                    "is on",
        min=0.5, max=30.0, default=1.5, step=50, precision=1,
    )

    # Overlay colours
    color_offset_ok: FloatVectorProperty(
        name="Offset", subtype='COLOR', size=4, min=0.0, max=1.0,
        default=(0.1, 0.9, 0.2, 0.35), update=_redraw,
        description="UV offset overlay: copies moved out of the 0-1 tile",
    )
    color_offset_bad: FloatVectorProperty(
        name="Overlapping", subtype='COLOR', size=4, min=0.0, max=1.0,
        default=(0.95, 0.1, 0.1, 0.35), update=_redraw,
        description="UV offset overlay: copies still overlapping the original",
    )
    color_texel_low: FloatVectorProperty(
        name="Low", subtype='COLOR', size=3, min=0.0, max=1.0,
        default=(0.20, 0.45, 1.00), update=_redraw,
        description="Texel density overlay: below the target",
    )
    color_texel_ok: FloatVectorProperty(
        name="On Target", subtype='COLOR', size=3, min=0.0, max=1.0,
        default=(0.25, 0.85, 0.35), update=_redraw,
        description="Texel density overlay: within the tolerance",
    )
    color_texel_high: FloatVectorProperty(
        name="High", subtype='COLOR', size=3, min=0.0, max=1.0,
        default=(1.00, 0.25, 0.20), update=_redraw,
        description="Texel density overlay: above the target",
    )

    # Defaults for new files
    default_map_size: EnumProperty(
        name="Map Size", items=MAP_SIZES, default='2048',
        description="Map size new files start with",
    )
    default_padding_auto: BoolProperty(
        name="Auto Padding", default=True,
        description="New files work out the padding from the map size",
    )
    default_suffix_style: EnumProperty(
        name="Suffixes",
        items=[(sid, label, f"<name>_{lo} / <name>_{hi}") for sid, lo, hi, label in SUFFIX_STYLES],
        default='LOW_HIGH',
        description="Low / high suffixes new files name pairs with",
    )
    default_baker: EnumProperty(
        name="Baker", items=bakers.items(), default='CUSTOM',
        description="Baker preset new files start with",
    )

    # Integrations
    use_uvpackmaster: BoolProperty(
        name="UVPackmaster",
        description="Use UVPackmaster 4 for packing and stacking when it's installed",
        default=True,
    )
    use_zen_uv: BoolProperty(
        name="Zen UV",
        description="Use Zen UV for packing, stacking and texel density when it's installed",
        default=True,
    )

    def draw(self, context):
        from . import integrations
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        col = _section(layout, "Interface", 'WINDOW')
        col.prop(self, "category")
        col.prop(self, "show_popup")

        col = _section(layout, "Issues", 'ERROR')
        col.prop(self, "isolate_issues")
        col.prop(self, "recheck_after_fix")

        col = _section(layout, "Packing and Textures", 'UV')
        col.prop(self, "scripted_search_seconds")
        col.prop(self, "auto_reload_interval")

        col = _section(layout, "Overlay Colours", 'COLOR')
        col.prop(self, "color_offset_ok")
        col.prop(self, "color_offset_bad")
        col.prop(self, "color_texel_low")
        col.prop(self, "color_texel_ok")
        col.prop(self, "color_texel_high")

        col = _section(layout, "Defaults for New Files", 'FILE_NEW')
        col.prop(self, "default_map_size")
        col.prop(self, "default_padding_auto")
        col.prop(self, "default_suffix_style")
        col.prop(self, "default_baker")
        col.operator("preflight.apply_defaults", icon='IMPORT')

        col = _section(layout, "Integrations", 'PLUGIN')
        for prop, installed in (("use_uvpackmaster", integrations.uvpm_installed()),
                                ("use_zen_uv", integrations.zen_installed())):
            row = col.row()
            row.prop(self, prop)
            note = row.row()
            note.active = False
            note.label(text="installed" if installed else "not installed")


class PREFLIGHT_OT_apply_defaults(bpy.types.Operator):
    """Set this scene's map size, padding, suffixes and baker to the defaults in the
preferences"""
    bl_idname = "preflight.apply_defaults"
    bl_label = "Apply to This Scene"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        apply_defaults(context.scene)
        self.report({'INFO'}, f"Pre-Flight defaults applied to '{context.scene.name}'")
        return {'FINISHED'}


def get(context=None):
    """The add-on preferences, or their defaults when the add-on isn't enabled
    (e.g. headless tests)."""
    global _defaults
    context = context or bpy.context
    addon = context.preferences.addons.get(PreflightPreferences.bl_idname)
    if addon is not None:
        return addon.preferences
    if _defaults is None:
        values = {}
        for prop in PreflightPreferences.bl_rna.properties:
            if prop.identifier in ("rna_type", "bl_idname"):
                continue
            values[prop.identifier] = (tuple(prop.default_array)
                                       if getattr(prop, "is_array", False)
                                       else getattr(prop, "default", None))
        _defaults = SimpleNamespace(**values)
    return _defaults


def sidebar_category():
    """The sidebar tab name saved in the preferences, or the default."""
    return get().category or DEFAULT_TAB


def apply_defaults(scene):
    """Set `scene`'s map size, padding, suffix style and baker to the defaults."""
    prefs = get()
    s = scene.preflight
    s.texture_size = prefs.default_map_size
    s.texture_square = True
    s.padding_auto = prefs.default_padding_auto
    s.suffix_style = prefs.default_suffix_style
    if s.baker != prefs.default_baker:
        s.baker = prefs.default_baker


def _has_settings(scene):
    """True if `scene` already has saved Pre-Flight settings."""
    stores = [scene]
    if hasattr(scene, "bl_system_properties_get"):
        system = scene.bl_system_properties_get()
        if system is not None:
            stores.insert(0, system)
    return any("preflight" in store for store in stores)


def apply_new_file_defaults():
    """Apply the defaults to scenes that have no Pre-Flight settings yet (new files)."""
    for scene in bpy.data.scenes:
        if not scene.library and not _has_settings(scene):
            apply_defaults(scene)


classes = (PreflightPreferences, PREFLIGHT_OT_apply_defaults)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
