# Pre-Flight

Pre-bake prep and checks for Blender. Pre-Flight gets low and high poly meshes ready
for baking in Substance Painter, Marmoset Toolbag, Fyr Baker, Blender or xNormal, and
catches what would spoil the bake before you export.

Blender 4.2 or newer (developed on 5.2). Author: Jordan Moss. Licence: GPL-3.0-or-later.

**New here? Read the [User Guide](docs/USER_GUIDE.md)**: every tab and tool explained in plain
language, with pictures.

## What it does

The panel lives in the 3D View sidebar (**N** > **Pre-Flight**), with a tab strip down
its left side. Map size is in Shading > Textures, the baker preset in Baker, and Full Prep with the Validate, Names and Projection checks in Checks.

- **Shading**: hard edges from UV seams or island borders (mirror lines stay soft),
  Triangulate modifier, clearing custom normals. **Textures** loads maps exported from
  Substance Painter (every default export preset, UDIMs included) into the materials,
  reloads them, watches the folder for re-exports, and has a normal map green-channel
  flip. AO multiplies into the base colour only where the surface isn't lit (EEVEE).
- **UVs**: mirror / array copies offset +1U (with an overlay), packing with
  UVPackmaster, Zen UV or Blender, stacking similar islands, texel density overlay and
  tools, and **Match Map Ratio** for non-square maps.
- **Low / High**: collections, pairing, naming (`_low/_high`, `_lo/_hi`, `_lp/_hp`,
  `_l/_h`), Match & Rename, and explode for checking pairs.
- **Baker**: baker presets (the prep settings that suit each baker, plus a checklist for
  the baker's side) and **ID Colors** for the highs (per object, part or material).
- **Rigging** (hard surface): **Mirror Parts** copies parts to the other side with
  properly mirrored pivots (positive scale, side names, hierarchy and bone parents
  carried over). **Bones from Origins** adds a bone on each part's pivot, chained by
  the part hierarchy under a root, and binds each part rigidly (bone parent or a 100%
  vertex group). **Piston**, **Link Gears** (meshed or chained, by size ratio;
  with a chain or track selected, the active wheel drives it and the other wheels turn
  with it) and
  **Chain Along Curve** (one bone per link, driven by a sprocket; one skinned mesh, or
  **Instances** that share the link's mesh, with **Keep Original** leaving the link
  where it is to edit; **Path Around Wheels**
  wraps the path round the selected wheels like a chain or belt, about the axis each
  wheel is round about, or the Axle you set; **Calculate Links** sets
  the link count that fills it most exactly; **Roll Links** turns a finished chain's
  links about the path to stand them up, lay them flat or flip them) cover the usual
  mechanisms, with **Wheel Spin**: wheels and sprockets roll by the distance the vehicle
  really moves, by any control, and by their side's share of a turn, so tracks
  counter-rotate on a spin. **Drive Control** adds a ground bone to drive and steer
  with; **Bake Wheel Roll** keys each wheel from its own path for arcs and loops.
  **Clear Drivers** strips every driver off the selection (or the selected bones). Bind
  **Keep Hierarchy** leaves the parts' own parenting in place (they follow helper bones). **Bone from Hinge** and
  **Origin to Hinge** take a hinge from an Edit Mode selection (axle vertices, an edge
  ring or a face); **Hinge Limits** frees one axis within a range. **IK Chain** puts a
  target and a pole on a chain of bones (an excavator arm, a robot arm), carrying the
  hinge limits over as IK locks and limits; the target rides on the arm's mount, so
  slewing the turret carries the reach round. **Set Up Controls** sorts bones into
  coloured Controls / Mechanism / Deform collections and gives each control a shape: flat on the ground, or a Rotate arrow about a hinge. In Pose Mode, **Apply Shape** sets any bone's shape (Ring, Rotate, Square, Arrows, Diamond, Cross, Cube, Sphere), the axis it faces and its size; Set Up Controls keeps hand-set shapes, **Auto Shape** puts them back.
  **Check Rig** and **Make Game Ready** get the rig past game engines: one root bone, a
  safe rig name, rigid skinning, applied and positive scale, no dependency cycles, and
  constraints and drivers baked to keys.
- **Engine Mesh** (in Low / High): **Make Engine Mesh** duplicates the lows with
  modifiers applied, `_low` removed and parts combined (all in one, per texture set or
  per parent). Rigged parts stay skinned, the bake setup isn't touched, and a new copy
  replaces the last one.
- **Checks** / **Issues**: Validate, Check Names and Check Projection fill the Issues
  list. Click an issue to isolate and frame it; Select picks the problem elements;
  Fix (or Fix All) repairs it. Issues you mean to keep can be ignored, and stay ignored
  across re-checks.

Map size is set per scene, with an optional size per material (texture set). Padding,
the padding and overlap checks, packing margins and texel density all follow it.

## Install

Download or build `dist/preflight-<version>.zip`, then in Blender open
**Edit > Preferences > Get Extensions**, click the menu at the top right and choose
**Install from Disk**, or drag the zip into the Blender window.

To build the zip yourself:

```
python tools/build.py
```

This validates the package and writes the zip to `dist/`. It finds Blender from an
argument, the `BLENDER` environment variable or the default Windows install folder.

## Preferences

**Edit > Preferences > Add-ons > Pre-Flight**:

- **Interface**: sidebar tab name; a popup listing results after checks.
- **Issues**: isolate an issue's objects when you click it; re-check after fixes (turn
  off on very large scenes, and the list is marked out of date instead).
- **Packing and Textures**: the heuristic search time for packs that can't be stopped
  by hand (Fix All); how often Auto Reload checks for re-exported textures.
- **Overlay Colours**: the UV offset and texel density overlay colours.
- **Defaults for New Files**: map size, padding, low/high suffixes and baker preset.
  **Apply to This Scene** sets them on the open scene.
- **Integrations**: turn UVPackmaster or Zen UV use on or off.

## Upgrading from Bake Prep

Pre-Flight was called Bake Prep. Files saved with Bake Prep keep everything when they
open in Pre-Flight: scene and per-material settings, ignored issues, UV ratio records,
stacked-UV offsets, explode positions, texture node setups and the triangulate modifier
are all moved to their new names on load. Uninstall Bake Prep before installing
Pre-Flight.

## Optional integrations

Pre-Flight uses these add-ons when they're installed and enabled, and falls back to
Blender's own tools without them:

- **UVPackmaster 4**: Pack, the Pack UV fix and Fix All. Pack Selected and the Pack UV
  fix run it interactively (progress in the viewport, Esc to stop). Pre-Flight's
  UVPackmaster settings in the UVs tab (mode, rotation, heuristic search, precision)
  are applied for each of its packs and UVPackmaster's own panel is left unchanged.
  Packing a partial selection in Edit Mode packs it around the unselected islands.
  Stack Similar uses its Align Similar.
- **Zen UV**: Pack (with Zen's own engine and settings), Stack Similar (needs the Zen UV
  Library) and Set Texel Density.

## Development

For a live install, link the `preflight` folder into Blender's extensions folder, for
example on Windows:

```
mklink /J "%APPDATA%\Blender Foundation\Blender\5.2\extensions\user_default\preflight" preflight
```

`tools/make_icons.py` redraws the icons in `preflight/icons/`.

## Layout

```
preflight/
  __init__.py            bl_info, register / unregister
  blender_manifest.toml  extension manifest
  preferences.py         add-on preferences and new-file defaults
  properties.py          scene, material and window-manager settings
  migrate.py             moves Bake Prep data to Pre-Flight names on load
  issues.py              the Issues list: storage, staleness, isolate and frame
  integrations.py        UVPackmaster 4 and Zen UV (pack, stack, texel density)
  overlay.py             UV offset overlay
  texel_overlay.py       texel density overlay
  constants.py, utils.py
  core/                  logic only, no operators or UI
    checks.py            Validate, Check Names, Check Projection
    problems.py          problem element masks for Select
    geometry.py          cleanup, skew, triangulation reads
    edges.py             edge analysis, hard edges, problem edges
    uv_layout.py         UV islands, overlap and padding checks
    uv_stack.py          stacked islands: find, offset +1U, restore
    mapsize.py           map size, padding, UV ratio
    texel.py             texel density
    projection.py        low/high ray distances
    naming.py            low/high suffixes and matching
    dependencies.py      boolean cutters and modifier dependencies
    bakers.py            baker presets
    id_colors.py         ID colours
    textures.py          Painter export names, texture sets
    triangulate.py       the Triangulate modifier
    rigging.py           mirrored parts, bones, pistons, gears, chains, game rigs
    export.py            engine copies
  operators/             operators, one module per area
  ui/                    sidebar panel and icons
  icons/                 icon PNGs
docs/                    the User Guide and its pictures
tools/                   build.py (package zip), make_icons.py (icons)
```
