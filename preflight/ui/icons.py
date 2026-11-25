# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Colour icons, made by tools/make_icons.py into preflight/icons/.

icon(name) returns an icon_value for layout calls; 0 (no icon) when the icon is
missing or previews aren't available, e.g. when running headless.
"""

from pathlib import Path

import bpy
import bpy.utils.previews

ICON_DIR = Path(__file__).resolve().parent.parent / "icons"
SEVERITY_DOTS = {'ERROR': "dot_error", 'WARNING': "dot_warning", 'INFO': "dot_info"}

_previews = None


def icon(name):
    if _previews is None or name not in _previews:
        return 0
    return _previews[name].icon_id


def severity(sev):
    """The coloured dot for an issue severity."""
    return icon(SEVERITY_DOTS.get(sev, "dot_info"))


def register():
    global _previews
    _previews = bpy.utils.previews.new()
    for path in sorted(ICON_DIR.glob("*.png")):
        _previews.load(path.stem, str(path), 'IMAGE')


def unregister():
    global _previews
    if _previews is not None:
        bpy.utils.previews.remove(_previews)
        _previews = None
