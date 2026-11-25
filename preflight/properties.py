# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Scene, material and window-manager settings."""

import bpy
import numpy as np
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty,
                       IntProperty, PointerProperty, StringProperty)

from . import integrations
from . import issues as issue_store
from .core import bakers, export, rigging, texel
from .core.naming import SUFFIX_STYLES
from .overlay import overlay_sync
from .texel_overlay import texel_overlay_invalidate, texel_overlay_sync

MAP_SIZES = [(str(n), str(n), "") for n in (256, 512, 1024, 2048, 4096, 8192)]
TRI_METHODS_QUAD = [
    ('BEAUTY', "Beauty", "Split along the best-looking diagonal"),
    ('FIXED', "Fixed", "Split from the first to the third vertex"),
    ('FIXED_ALTERNATE', "Fixed Alternate", "Split from the second to the fourth vertex"),
    ('SHORTEST_DIAGONAL', "Shortest Diagonal", "Split along the shortest diagonal"),
    ('LONGEST_DIAGONAL', "Longest Diagonal", "Split along the longest diagonal"),
]
TRI_METHODS_NGON = [
    ('BEAUTY', "Beauty", "Arrange new triangles evenly"),
    ('CLIP', "Clip", "Split the polygon with an ear-clipping algorithm"),
]


# --- Update callbacks ---

def _auto_reload_update(self, context):
    from .operators.textures import auto_reload_update
    auto_reload_update(self, context)


def _apply_baker(self, context):
    bakers.apply(self, self.baker)


def _texel_changed(self, context):
    texel_overlay_invalidate()


def _get_texel_target(self):
    return texel.from_unit(self.texel_target_display, self.texel_unit)


def _set_texel_target(self, value):
    self.texel_target_display = texel.to_unit(value, self.texel_unit)


# --- Scene settings ---

class PreflightSceneSettings(bpy.types.PropertyGroup):
    # Shading
    edge_source: EnumProperty(
        name="Source",
        items=[
            ('SEAMS', "Seams", "Mark sharp edges where UV seams are marked"),
            ('ISLANDS', "Island Borders", "Mark sharp edges wherever the active UV map is split"),
        ],
        default='SEAMS',
    )
    min_angle: FloatProperty(
        name="Min Angle",
        description="Only harden UV borders at least this sharp. Shallow seams, like a "
                    "cylinder's side seam, can stay soft. 0 hardens every border",
        subtype='ANGLE', min=0, max=np.pi, default=0,
    )
    soft_mirror_edges: BoolProperty(
        name="Soft Mirror Line",
        description="Keep edges on a Mirror modifier's plane soft, so the mirrored halves "
                    "shade as one surface",
        default=True,
    )
    sync_seams: BoolProperty(
        name="Update Seams",
        description="Also mark the detected island borders as seams",
        default=True,
    )
    clear_custom_normals: BoolProperty(
        name="Clear Custom Normals",
        description="Custom normals override sharp edges, so remove them",
        default=True,
    )
    strip_auto_smooth: BoolProperty(
        name="Remove Smooth by Angle",
        description="Remove Smooth by Angle / Auto Smooth modifiers, which would add extra "
                    "sharp edges",
        default=True,
    )
    quad_method: EnumProperty(name="Quads", items=TRI_METHODS_QUAD, default='BEAUTY')
    ngon_method: EnumProperty(name="N-gons", items=TRI_METHODS_NGON, default='BEAUTY')
    keep_normals: BoolProperty(name="Keep Normals", default=True)
    triangulate_in_full: BoolProperty(
        name="Include Triangulate",
        description="Add the Triangulate modifier as part of Full Prep",
        default=True,
    )

    # Checks
    texture_size: EnumProperty(
        name="Map Width",
        description="Resolution the maps are baked at (width). Sets the padding, the "
                    "padding check, packing margins and texel density",
        items=MAP_SIZES, default='2048', update=_texel_changed,
    )
    texture_height: EnumProperty(
        name="Map Height",
        description="Height of a non-square map",
        items=MAP_SIZES, default='2048', update=_texel_changed,
    )
    texture_square: BoolProperty(
        name="Square",
        description="Maps are square (height = width). Off: set the height for a "
                    "non-square map",
        default=True, update=_texel_changed,
    )
    padding_auto: BoolProperty(
        name="Auto",
        description="Padding follows the map size: size / 128, at least 4px (8px at 1024, "
                    "16px at 2048, 32px at 4096), so islands stay apart down to small mips",
        default=True,
    )
    padding: IntProperty(
        name="Padding",
        description="Minimum gap between UV islands, in pixels at the map size "
                    "(used when Auto is off)",
        min=1, max=128, default=16,
    )
    skew_angle: FloatProperty(
        name="Skew Angle",
        description="Warn about faces whose shading normals bend more than this from the "
                    "face: rays leave at an angle there, skewing details",
        subtype='ANGLE', min=0, max=np.pi / 2, default=np.radians(30),
    )
    texel_tolerance: FloatProperty(
        name="Texel Tolerance",
        description="Warn about UV islands whose texel density is further than this "
                    "from the texture set's",
        subtype='PERCENTAGE', min=1, max=100, default=25, update=_texel_changed,
    )
    projection_tolerance: FloatProperty(
        name="Distance Tolerance",
        description="Warn where the high sits further from the low than this, as a "
                    "percentage of the low's size",
        subtype='PERCENTAGE', min=0.1, max=50, default=2,
    )
    pack_engine: EnumProperty(
        name="Packer",
        description="What packs UV islands (Pack button and the Pack UV fix)",
        items=[('AUTO', "Auto", "UVPackmaster if installed, then Zen UV, then Blender"),
               ('UVPM', "UVPackmaster", "UVPackmaster 4, with the settings in its own panel"),
               ('ZEN', "Zen UV", "Zen UV's Pack, with its own settings and engine"),
               ('BLENDER', "Blender", "Blender's built-in Pack Islands")],
        default='AUTO',
    )
    stack_engine: EnumProperty(
        name="Stacker",
        description="What stacks similar islands",
        items=[('AUTO', "Auto", "UVPackmaster if installed, else Zen UV"),
               ('UVPM', "UVPackmaster",
                "UVPackmaster's Align Similar, with its similarity settings"),
               ('ZEN', "Zen UV", "Zen UV's Stack Similar (needs the Zen UV Library)")],
        default='AUTO',
    )
    pack_use_padding: BoolProperty(
        name="Use Bake Padding",
        description="Pack with Pre-Flight's padding and texture size as the margin, instead of "
                    "the packer's own margin setting (restored afterwards)",
        default=True,
    )
    pack_keep_stacks: BoolProperty(
        name="Keep Stacks",
        description="UVPackmaster: pack overlapping (stacked) islands as one, so stacks stay "
                    "stacked (Lock Overlapping: Exact, for this pack only)",
        default=True,
    )
    # Pre-Flight's own UVPackmaster settings, applied only for Pre-Flight's packs
    # (see integrations._uvpm_overrides).
    uvpm_pack_mode: EnumProperty(
        name="Pack Mode",
        description="UVPackmaster pack mode Pre-Flight packs with",
        items=[(m[0], m[1], m[2], i) for i, m in enumerate(integrations.UVPM_PACK_MODES)],
        default='pack.single_tile',
    )
    uvpm_rotate: BoolProperty(
        name="Rotate", default=True,
        description="Let the packer rotate islands (on a non-square map only by 180 degrees, "
                    "so texels stay square)",
    )
    uvpm_rotation_step: IntProperty(
        name="Step", min=1, max=180, default=90,
        description="Rotation step in degrees (180 on a non-square map)",
    )
    uvpm_flip: BoolProperty(name="Flip", default=False,
                            description="Let the packer mirror islands")
    uvpm_normalize_scale: BoolProperty(
        name="Normalize Scale", default=True,
        description="Scale islands to the same average texel density before packing",
    )
    uvpm_heuristic: BoolProperty(
        name="Heuristic", default=False,
        description="Try many layouts and keep the tightest (slower, packs tighter)",
    )
    uvpm_heuristic_time: IntProperty(
        name="Time (s)", min=0, max=3600, default=0,
        description="Seconds to search. 0: Pack Selected and Pack UV Islands search until "
                    "you press Esc; Fix All uses 10 s",
    )
    uvpm_precision: IntProperty(
        name="Precision", min=10, max=10000, default=500,
        description="How finely the packer places islands (higher: tighter, slower)",
    )
    texel_target_mode: EnumProperty(
        name="Target",
        description="What texel density the overlay compares each island to",
        items=[('SET', "Texture Set", "The texture set's own (area-weighted median) density"),
               ('CUSTOM', "Custom", "A density you set, at the bake texture size")],
        default='SET', update=_texel_changed,
    )
    texel_target: FloatProperty(
        name="Density",
        description="The custom target in pixels per metre (the number you enter, "
                    "converted from the chosen units)",
        get=_get_texel_target, set=_set_texel_target,
    )
    texel_unit: EnumProperty(
        name="Units",
        description="Unit texel density is entered and shown in (pixels per metre, "
                    "centimetre, millimetre, inch or foot). The custom target keeps "
                    "its number: 1024 px/m becomes 1024 px/cm",
        items=texel.unit_items(), default='M', update=_texel_changed,
    )
    texel_target_display: FloatProperty(
        name="Density",
        description="Target texel density at the bake texture size, in the chosen units",
        min=0.001, soft_max=8192, default=1024, precision=2, step=100,
        update=_texel_changed,
    )
    closed_sections: StringProperty(
        name="Closed Sections",
        description="Ids of the collapsed sections in the Pre-Flight panel",
        options={'HIDDEN'},
    )
    baker: EnumProperty(
        name="Baker",
        description="The baker you'll bake in: sets Pre-Flight's prep settings to suit it "
                    "and lists the settings to match on the baker's side",
        items=bakers.items(), default='CUSTOM', update=_apply_baker,
    )
    projection_average: BoolProperty(
        name="Averaged Normals",
        description="Cast along averaged vertex normals, like a baker's default averaged "
                    "cage. Off: along the low's split shading normals (hard edges leave gaps)",
        default=True,
    )
    corner_angle: FloatProperty(
        name="Corner Angle",
        description="Flag edges sharper than this that have no UV split",
        subtype='ANGLE', min=0, max=np.pi, default=np.radians(89),
    )

    # Textures
    texture_folder: StringProperty(
        name="Texture Folder",
        description="Where Painter (or another tool) exports this asset's maps, named "
                    "<material>_<channel> (e.g. Body_BaseColor.png)",
        subtype='DIR_PATH',
    )

    # Rigging
    rig_armature: PointerProperty(
        name="Rig", type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'ARMATURE',
        description="The rig bones are added to. Empty: Bones from Origins makes a new one",
    )
    rig_mirror_axis: EnumProperty(
        name="Mirror Axis", items=[('X', "X", ""), ('Y', "Y", ""), ('Z', "Z", "")],
        default='X', description="World axis the parts are mirrored across",
    )
    rig_mirror_pivot: EnumProperty(
        name="Mirror Pivot",
        items=[('WORLD', "World Origin", "Mirror across the world origin"),
               ('CURSOR', "3D Cursor", "Mirror across the 3D cursor"),
               ('ACTIVE', "Active Object", "Mirror across the active object's origin")],
        default='WORLD', description="Where the mirror plane sits",
    )
    rig_name_sides: BoolProperty(
        name="Name Sides", default=True,
        description="Name parts without a side _L / _R (the copy gets the other side), "
                    "kept before any _low / _high suffix",
    )
    rig_bone_axis: EnumProperty(
        name="Axis",
        items=[('AUTO', "Auto", "The axis that stands out on each part: a bar's length, a "
                                "wheel's axle; a brick-shaped body's longest side"),
               ('X', "X", "Each part's local X"), ('Y', "Y", "Each part's local Y"),
               ('Z', "Z", "Each part's local Z")],
        default='AUTO', description="The part axis each bone points along",
    )
    rig_bone_length: FloatProperty(
        name="Length", min=0.01, max=4.0, default=1.0,
        description="Bone length as a fraction of how far the part reaches from its "
                    "origin: 1 runs the bone to the part's far end",
    )
    rig_root: BoolProperty(
        name="Root Bone", default=True,
        description="Put the top-level bones under one root bone (what engines expect)",
    )
    rig_aim_children: BoolProperty(
        name="Aim at Child", default=False,
        description="Point a bone with one child part at that child's origin",
    )
    rig_bind: EnumProperty(
        name="Bind", items=rigging.BIND_ITEMS, default='PARENT',
        description="How each part follows its bone",
    )
    rig_gear_mode: EnumProperty(
        name="Gears",
        items=[('MESHED', "Meshed", "Gears in contact: the driven gear turns the other way"),
               ('CHAINED', "Chained", "Sprockets on one chain or belt: same direction")],
        default='MESHED',
    )
    rig_gear_axis: EnumProperty(
        name="Spin Axis",
        items=[('X', "X", ""), ('Y', "Y", ""), ('Z', "Z", ""),
               ('AUTO', "Auto", "Each gear's own round axis")],  # after X/Y/Z: saved files keep theirs
        default='AUTO', description="The local axis gears and sprockets turn around",
    )
    rig_limit_axis: EnumProperty(
        name="Hinge Axis", items=[('X', "X", ""), ('Y', "Y", ""), ('Z', "Z", "")],
        default='Y', description="The bone's local axis the hinge turns around (Y runs "
                                 "along the bone, as Bone from Hinge places it)",
    )
    rig_limit_min: FloatProperty(
        name="Min", subtype='ANGLE', min=-6.2832, max=6.2832, default=-1.5708,
        description="Lowest hinge angle",
    )
    rig_limit_max: FloatProperty(
        name="Max", subtype='ANGLE', min=-6.2832, max=6.2832, default=1.5708,
        description="Highest hinge angle",
    )
    rig_lock_location: BoolProperty(
        name="Lock Location", default=True,
        description="Hinges can only turn, not move",
    )
    rig_shape: EnumProperty(
        name="Shape", items=rigging.SHAPE_ITEMS, default='RING',
        description="The control shape to give the selected bones",
    )
    rig_shape_axis: EnumProperty(
        name="Axis",
        items=[('AUTO', "Auto", "Flat on the ground, or about the bone's hinge"),
               ('X', "X", "Face along the bone's X"), ('Y', "Y", "Face along the bone's length"),
               ('Z', "Z", "Face along the bone's Z")],
        default='AUTO', description="Which of the bone's axes the shape faces along",
    )
    rig_shape_size: FloatProperty(
        name="Size", min=0.0, default=0.0, subtype='DISTANCE', unit='LENGTH',
        description="Radius of the shape in the scene. 0: just clear of the part the bone moves",
    )
    rig_shape_line: BoolProperty(
        name="Bone Line", default=True,
        description="Draw the bone inside its shape (a custom shape otherwise hides it)",
    )
    rig_ik_length: IntProperty(
        name="Chain", min=1, max=16, default=2,
        description="How many bones the IK moves, counting up from the selected one",
    )
    rig_ik_follow: EnumProperty(
        name="Target",
        items=[('MOUNT', "Follows Mount", "Target and pole ride on the bone the arm is mounted "
                                          "on: turning the turret or driving carries them"),
               ('ROOT', "In World", "Target and pole stay put under the root: the tip holds "
                                    "its place while the base moves")],
        default='MOUNT',
    )
    rig_ik_pole: BoolProperty(
        name="Pole", default=True,
        description="Add a pole bone that sets which way the chain bends",
    )
    rig_wheel_travel_axis: EnumProperty(
        name="Travel", items=[('X', "X", ""), ('Y', "Y", ""), ('Z', "Z", "")],
        default='Y', description="The body's location channel that drives the wheels",
    )
    rig_wheel_spin_axis: EnumProperty(
        name="Axle",
        items=[('AUTO', "Auto", "The axis the wheel's mesh is round about (else the side "
                                "unlike the other two)"),
               ('X', "X", "The wheel's local X"), ('Y', "Y", "The wheel's local Y"),
               ('Z', "Z", "The wheel's local Z")],
        default='AUTO', description="The wheel's own axis it spins about (Wheel Spin and "
                                    "Path Around Wheels)",
    )
    rig_wheel_reverse: BoolProperty(
        name="Reverse", default=False,
        description="Spin the other way (for wheels whose axle points the other way)",
    )
    rig_chain_axis: EnumProperty(
        name="Link Axis", items=[('X', "X", ""), ('Y', "Y", ""), ('Z', "Z", "")],
        default='X', description="The link's local axis that runs along the chain",
    )
    rig_path_offset: FloatProperty(
        name="Offset", default=0.0, subtype='DISTANCE', unit='LENGTH', soft_min=-1.0, soft_max=1.0,
        description="How far out from the wheel rims Path Around Wheels runs (about half "
                    "the link's thickness sits the links on the teeth)",
    )
    rig_chain_instances: BoolProperty(
        name="Instances", default=False,
        description="Make each link its own object sharing the link's mesh, parented to "
                    "its bone (edit the link to change them all), instead of one skinned mesh",
    )
    rig_chain_keep_link: BoolProperty(
        name="Keep Original", default=True,
        description="Instances: leave the original link showing where it is, so you can "
                    "edit it there and every link follows (off: hide it)",
    )
    rig_chain_count: IntProperty(
        name="Links", min=0, max=2000, default=0,
        description="Number of links. 0: the number that fills the path most exactly "
                    "(Calculate Links shows it)",
    )
    rig_join: BoolProperty(
        name="Join Parts", default=False,
        description="Make Game Ready: join the skinned parts into one mesh",
    )
    rig_bake: BoolProperty(
        name="Bake Motion", default=True,
        description="Make Game Ready: bake constraints and drivers to keyframes over the "
                    "scene's frame range, then remove them",
    )

    # Engine export
    export_combine: EnumProperty(
        name="Combine", items=export.COMBINE_ITEMS, default='ALL',
        description="How the engine copies' parts are combined",
    )

    # Issues marked as intended (keys from issues.ignore_key)
    ignored_issues: CollectionProperty(type=issue_store.PreflightNameItem)

    # Low / high
    suffix_style: EnumProperty(
        name="Suffixes",
        description="Suffixes Match & Rename, Pair Selected and Duplicate as Pair give "
                    "low and high names. Checks accept every style, but a pair must use one",
        items=[(sid, label, f"Name pairs <name>_{lo} / <name>_{hi}")
               for sid, lo, hi, label in SUFFIX_STYLES],
        default='LOW_HIGH',
    )
    low_collection: PointerProperty(type=bpy.types.Collection, name="Low")
    high_collection: PointerProperty(type=bpy.types.Collection, name="High")
    explode_axis: EnumProperty(
        name="Axis", items=[(a, a, f"Explode along {a}") for a in "XYZ"], default='X',
    )
    explode_split_shared: BoolProperty(
        name="Separate Shared",
        description="When pairs share a cutter or helper, give each extra pair its own copy "
                    "(parented to the part using it) so every pair explodes with intact cuts",
        default=True,
    )
    explode_spacing: FloatProperty(
        name="Spacing",
        description="Gap between exploded pairs, as a fraction of the largest part",
        min=0, soft_max=4, default=0.5,
    )


# --- Material settings ---

class PreflightMaterialSettings(bpy.types.PropertyGroup):
    """A texture set's own map size (when it differs from the scene's Maps setting)."""
    use_own_size: BoolProperty(
        name="Own Size",
        description="Bake this material's maps at their own size instead of the scene's "
                    "Maps setting (padding, checks, packing and texel density follow it)",
        default=False, update=_texel_changed,
    )
    texture_size: EnumProperty(name="Map Width", items=MAP_SIZES, default='2048',
                               update=_texel_changed)
    texture_height: EnumProperty(name="Map Height", items=MAP_SIZES, default='2048',
                                 update=_texel_changed)
    texture_square: BoolProperty(name="Square", default=True, update=_texel_changed,
                                 description="Square map (height = width)")


# --- Panel tabs ---

# (id, name, description, accent). Issues has no accent: its icon shows the results.
TABS = (
    ('SHADING', "Shading",
     "Hard edges, triangulation, custom normals and loading exported textures", "shading"),
    ('UVS', "UVs", "Mirror/array offsets, packing, stacking and texel density", "uvs"),
    ('PAIRS', "Low / High", "Collections, pairing, naming, exploding and engine meshes",
     "pairs"),
    ('BAKER', "Baker", "Baker preset and ID colours", "baker"),
    ('RIGGING', "Rigging", "Hard surface rigging: mirrored parts, bones on origins, "
                           "pistons, gears, chains and game-ready rigs", "rigging"),
    ('CHECKS', "Checks", "Full prep, validation, thresholds, fixes and problem selection",
     "checks"),
    ('ISSUES', "Issues", "Results of the last checks", None),
)
_tab_items_cache = []  # enum items must stay referenced while Blender uses them


def tab_icon(wm_settings, key):
    """icon_value of a tab's pictogram.

    The Issues icon is red, amber or green by result, and blue before any check.
    """
    from .ui import icons
    accent = next((t[3] for t in TABS if t[0] == key), None)
    if accent:
        return icons.icon(f"tab_{accent}")
    if not issue_store.any_checked(wm_settings):
        return icons.icon("tab_issues_info")
    errors, warnings, _ = issue_store.counts(wm_settings)
    return icons.icon("tab_issues_error" if errors else "tab_issues_warning" if warnings
                      else "tab_issues_ok")


def _tab_items(self, context):
    _tab_items_cache[:] = [(key, name, desc, tab_icon(self, key), i)
                           for i, (key, name, desc, _) in enumerate(TABS)]
    return _tab_items_cache


# --- Window-manager settings ---

class PreflightWMSettings(bpy.types.PropertyGroup):
    # Session state (overlays, issue list), kept on the window manager rather than the scene.
    tab: EnumProperty(
        name="Tab", description="Which part of Pre-Flight to show",
        items=_tab_items, default=0,
    )
    overlay_mirror: BoolProperty(
        name="Mirror", description="Show the UV offset overlay for Mirror modifiers",
        update=lambda self, ctx: overlay_sync(ctx),
    )
    overlay_texel: BoolProperty(
        name="Texel Density",
        description="Colour UV islands by texel density: blue below the target, green on "
                    "it (within the texel tolerance), red above",
        update=lambda self, ctx: texel_overlay_sync(ctx),
    )
    overlay_array: BoolProperty(
        name="Array", description="Show the UV offset overlay for Array modifiers",
        update=lambda self, ctx: overlay_sync(ctx),
    )

    auto_reload_textures: BoolProperty(
        name="Auto Reload",
        description="Reload Pre-Flight's textures whenever their files change on disk "
                    "(checks every 1.5 s while on)",
        default=False, update=lambda self, ctx: _auto_reload_update(self, ctx),
    )
    # Issue list (see issues.py)
    issues: CollectionProperty(type=issue_store.PreflightIssue)
    issue_index: IntProperty(name="Issue", update=issue_store.on_index_changed)
    checked_mesh: CollectionProperty(type=issue_store.PreflightNameItem)
    checked_names: CollectionProperty(type=issue_store.PreflightNameItem)
    checked_projection: CollectionProperty(type=issue_store.PreflightNameItem)
    checked_rig: CollectionProperty(type=issue_store.PreflightNameItem)
    rig_checked: BoolProperty()
    mesh_checked: BoolProperty()
    names_checked: BoolProperty()
    projection_checked: BoolProperty()
    names_used_collections: BoolProperty()
    checked_scene: StringProperty()


classes = (
    PreflightSceneSettings,
    PreflightMaterialSettings,
    PreflightWMSettings,
)


def register():
    issue_store.register()
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.preflight = PointerProperty(type=PreflightSceneSettings)
    bpy.types.Material.preflight = PointerProperty(type=PreflightMaterialSettings)
    bpy.types.WindowManager.preflight = PointerProperty(type=PreflightWMSettings)


def unregister():
    del bpy.types.WindowManager.preflight
    del bpy.types.Scene.preflight
    del bpy.types.Material.preflight
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    issue_store.unregister()
