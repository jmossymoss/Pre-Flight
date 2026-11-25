# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Validate Pre-Flight and build the installable extension zip into dist/.

usage: python tools/build.py [path to blender executable]

Blender is found from the argument, the BLENDER environment variable, or the default
Windows install folder. Install the zip with Edit > Preferences > Get Extensions >
Install from Disk (or drag it into Blender).
"""

import glob
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "preflight")
OUTPUT = os.path.join(ROOT, "dist")


def find_blender():
    if len(sys.argv) > 1:
        return sys.argv[1]
    if os.environ.get("BLENDER"):
        return os.environ["BLENDER"]
    installs = sorted(glob.glob(r"C:\Program Files\Blender Foundation\Blender *\blender.exe"))
    if installs:
        return installs[-1]
    sys.exit("Blender not found: pass its path or set BLENDER")


def run(blender, *args):
    print(">", "blender --command extension", *args)
    subprocess.run([blender, "--command", "extension", *args], check=True)


def main():
    blender = find_blender()
    os.makedirs(OUTPUT, exist_ok=True)
    run(blender, "validate", SOURCE)
    run(blender, "build", "--source-dir", SOURCE, "--output-dir", OUTPUT)
    for path in sorted(glob.glob(os.path.join(OUTPUT, "*.zip"))):
        print("built", path)


if __name__ == "__main__":
    main()
