# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Sidebar panel and its colour icons."""

from . import icons, panels
from .panels import PANELS  # noqa: F401


def register(category="Pre-Flight"):
    icons.register()
    panels.register(category)


def unregister():
    panels.unregister()
    icons.unregister()
