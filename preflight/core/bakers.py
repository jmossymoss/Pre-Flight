# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Baker presets: the Pre-Flight settings that suit each baker, and its checklist.

Every baker here uses MikkTSpace and pairs meshes by name, so presets share most
prep settings; the notes list what to set on the baker's side.
"""

# Settings every preset applies (scene.preflight attribute -> value).
COMMON = {
    'soft_mirror_edges': True,
    'triangulate_in_full': True,    # bake and export the same triangles
    'projection_average': True,     # these bakers cast from averaged normals by default
}

BAKERS = {
    'PAINTER': {
        "name": "Substance Painter",
        "settings": {},
        "notes": (
            "Bake Mesh Maps: Match By Mesh Name (_low / _high)",
            "Keep Average Normals on",
            "Normal format (OpenGL / DirectX) is a project setting",
            "Export the low triangulated, as Pre-Flight leaves it",
        ),
    },
    'DESIGNER': {
        "name": "Substance Designer",
        "settings": {},
        "notes": (
            "Bake Model Information on the low",
            "Add the highs, Match By Mesh Name (_low / _high)",
            "Same bakers as Painter: keep Average Normals on",
        ),
    },
    'MARMOSET': {
        "name": "Marmoset Toolbag",
        "settings": {},
        "notes": (
            "Quick Loader groups meshes by _low / _high",
            "Tangent space: Mikk (matches Blender and most engines)",
            "Set Flip Y to match your engine's normal format",
        ),
    },
    'FYR': {
        "name": "Fyr Baker",
        "settings": {},
        "notes": (
            "Bakes inside Blender: no export step",
            "Pairs by _low / _high names, as Pre-Flight sets them",
        ),
    },
    'BLENDER': {
        "name": "Blender (Cycles)",
        "settings": {},
        "notes": (
            "Selected to Active, with the high selected and the low active",
            "Cage Extrusion sets the ray distance",
            "Normal maps bake as OpenGL (Y+)",
        ),
    },
    'XNORMAL': {
        "name": "xNormal",
        "settings": {},
        "notes": (
            "Export low and high separately, the low triangulated",
            "Use Exported Normals on the low",
        ),
    },
}


def items():
    """EnumProperty items: Custom (leaves settings alone), then each baker."""
    return [('CUSTOM', "Custom", "Your own settings: nothing is changed")] + [
        (key, b["name"], f"Prep settings for {b['name']}") for key, b in BAKERS.items()]


def settings_for(key):
    """{scene.preflight attribute: value} for a baker key ({} for unknown keys)."""
    if key not in BAKERS:
        return {}
    return {**COMMON, **BAKERS[key]["settings"]}


def apply(settings, key):
    """Set the preset's values on scene.preflight. Returns the attributes changed."""
    changed = []
    for attr, value in settings_for(key).items():
        if hasattr(settings, attr) and getattr(settings, attr) != value:
            setattr(settings, attr, value)
            changed.append(attr)
    return changed


def notes(key, suffix_label="_low / _high"):
    """The baker's checklist, naming the pair suffixes in use."""
    return tuple(n.replace("_low / _high", suffix_label)
                 for n in BAKERS.get(key, {}).get("notes", ()))
