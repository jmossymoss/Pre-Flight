# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Bake checks: each returns Issue records for the operators to display.

An Issue can name a select kind (see problems.ELEMENT_KINDS) and a fix kind (see
operators/issues.py). Severities: ERROR breaks the bake, WARNING likely causes artifacts.
"""

from dataclasses import dataclass

import bpy
import numpy as np

from ..constants import TRIANGULATE_MOD, UV_OFFSET_KINDS
from ..utils import is_auto_smooth_mod
from . import geometry, mapsize, projection, texel, uv_stack
from .edges import edge_analysis, edges_from_seams, mirror_plane_edges
from .naming import DUP_SUFFIX_RE, parse_pair, style_label
from .uv_layout import (check_uv_layout, triangle_uvs, uv_islands, uv_offset_ok,
                        zero_uv_faces)

TEXTURE_SET_PREFIX = "Texture set: "
SEVERITY_ORDER = {'ERROR': 0, 'WARNING': 1, 'INFO': 2}


# --- Records ---

@dataclass
class Issue:
    source: str            # 'MESH' (Validate), 'NAMES' (Check Names) or 'PROJECTION'
    severity: str          # 'ERROR', 'WARNING' or 'INFO'
    heading: str           # object name, or "Texture set: <material>"
    message: str
    objects: tuple = ()    # names of the objects the issue is about
    select: str = ""       # problems.ELEMENT_KINDS key, or ""
    fix: str = ""          # fix kind, or ""
    fix_data: str = ""     # extra fix argument, e.g. a modifier name
    select_data: str = ""  # extra select argument, e.g. a pair's base name

    @property
    def is_texture_set(self):
        return self.heading.startswith(TEXTURE_SET_PREFIX)


@dataclass
class ObjectResult:
    """Validate's result for one object: its issues plus the data the texture-set checks need.

    Cached between runs (see issues.ObjectCache) so unchanged objects aren't re-read.
    """
    issues: list
    tex_set: str = ""          # texture set (material) name; "" when it has no UV triangles
    tri_uv: object = None      # (n, 3, 2) float32 UVs of its triangles
    tri_island: object = None  # island id per triangle
    flips: dict = None         # island -> mirrored?
    texel: tuple = None        # (island ids, density, world area), or None


def sort_issues(issues):
    """Sort by severity, then objects before texture sets, then by heading."""
    return sorted(issues, key=lambda i: (SEVERITY_ORDER.get(i.severity, 3), i.is_texture_set,
                                         i.heading.lower()))


# --- Formatting ---

def fmt_length(value, scene=None):
    """Format a length in the scene's units."""
    if abs(value) < 1e-5:  # coincident surfaces leave float noise
        value = 0.0
    scene = scene or bpy.context.scene
    units = scene.unit_settings
    try:
        return bpy.utils.units.to_string(
            units.system if units.system != 'NONE' else 'METRIC', 'LENGTH',
            value * units.scale_length, precision=3)
    except (ValueError, TypeError):
        return f"{value:.4g}"


def _plural(n, word, plural=None):
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def ratio_text(ratio):
    """'square', '2:1' or '1:2' for a width / height ratio."""
    if abs(ratio - 1) < 1e-4:
        return "square"
    return f"{ratio:g}:1" if ratio > 1 else f"1:{1 / ratio:g}"


# --- Mesh checks (Validate) ---

def is_high(obj, high_collection=None):
    """True for objects named as a high, or unnamed ones in the High collection."""
    pair = parse_pair(DUP_SUFFIX_RE.sub("", obj.name))
    if pair:
        return pair.role == "high"
    return bool(high_collection and obj.name in high_collection.all_objects)


def high_issues(obj, add):
    """Normal-direction check for a high: rays hit it from outside."""
    if obj.matrix_world.determinant() < 0:
        add('ERROR', "Negative scale (inverted normals): apply scale with Ctrl+A",
            fix='APPLY_SCALE')
    elif geometry.normals_point_inwards(obj.data):
        add('ERROR', "Normals point inwards (inverted): rays hit the back of the faces",
            fix='RECALC_NORMALS')


def cleanup_issues(obj, add):
    """Normals, topology, doubles and loose geometry checks for a low."""
    masks = geometry.cleanup_masks(obj.data)
    if masks['INSIDE_OUT']:
        add('ERROR', "Faces point inwards (inside out): rays start from the wrong side",
            fix='RECALC_NORMALS')
    flipped = int(masks['FLIPPED'][1].sum())
    if flipped:
        add('ERROR', f"{_plural(flipped, 'edge')} between faces pointing opposite ways "
                     "(flipped faces)", select='FLIPPED', fix='RECALC_NORMALS')
    non_manifold = int(masks['NON_MANIFOLD'][1].sum())
    if non_manifold:
        add('WARNING', f"{_plural(non_manifold, 'edge')} shared by 3+ faces (non-manifold)",
            select='NON_MANIFOLD')
    doubles = int(masks['DOUBLES'][1].sum())
    if doubles:
        add('WARNING', f"{_plural(doubles, 'vertex', 'vertices')} on top of another "
                       "(unmerged doubles)", select='DOUBLES', fix='MERGE_DOUBLES')
    zero = int(masks['ZERO_AREA'][1].sum())
    if zero:
        add('WARNING', f"{_plural(zero, 'face')} with zero area", select='ZERO_AREA')
    loose_v = int(masks['LOOSE_VERTS'][1].sum())
    loose_e = int(masks['LOOSE_EDGES'][1].sum())
    if loose_v or loose_e:
        parts = [p for p in (loose_v and _plural(loose_v, 'vertex', 'vertices'),
                             loose_e and _plural(loose_e, 'edge')) if p]
        add('INFO', f"Loose {' and '.join(parts)} (not part of any face)",
            select='LOOSE_EDGES' if loose_e else 'LOOSE_VERTS', fix='DELETE_LOOSE')


def settings_key(settings, high, obj=None):
    """The settings an object's Validate result depends on, as a cache key."""
    return (high, settings.corner_angle, settings.skew_angle, settings.edge_source,
            mapsize.map_size(mapsize.of_object(settings, obj)),
            settings.id_data.unit_settings.scale_length)


def check_object(obj, settings, high):
    """Validate one object.

    Highs only get a normal-direction check; they don't need UVs, applied scale,
    triangulation or clean topology.
    """
    issues = []

    def add(severity, msg, select="", fix="", fix_data="", select_data=""):
        issues.append(Issue('MESH', severity, obj.name, msg, (obj.name,),
                            select, fix, fix_data, select_data))

    if high:
        high_issues(obj, add)
        return ObjectResult(issues)
    with geometry.shared_reads():
        return _check_low(obj, settings, add, issues)


def _check_low(obj, settings, add, issues):
    me = obj.data
    result = ObjectResult(issues)
    scale = obj.matrix_world.to_scale()
    if obj.matrix_world.determinant() < 0:
        add('ERROR', "Negative scale (flipped normals): apply scale with Ctrl+A",
            fix='APPLY_SCALE')
    elif any(abs(x - 1) > 1e-4 for x in scale):
        add('ERROR', "Unapplied scale: apply with Ctrl+A", fix='APPLY_SCALE')

    cleanup_issues(obj, add)

    if not me.uv_layers:
        add('ERROR', "No UV map", fix='UNWRAP')
        return result
    if len(me.uv_layers) > 1:
        add('INFO', f"{len(me.uv_layers)} UV maps, baking uses '{me.uv_layers.active.name}'")

    # Hard edges and UV splits
    uv_split, face_angle, manifold = edge_analysis(me)
    sharp = np.zeros(len(me.edges), dtype=bool)
    me.edges.foreach_get("use_edge_sharp", sharp)
    on_mirror = mirror_plane_edges(obj, me)

    hard_no_split = int((sharp & manifold & ~uv_split & ~on_mirror).sum())
    if hard_no_split:
        add('ERROR', f"{hard_no_split} hard edge(s) without a UV split (visible seams)",
            select='HARD_NO_SPLIT', fix='SOFTEN_UNSPLIT')
    corners = int((manifold & ~uv_split & ~on_mirror & (face_angle > settings.corner_angle)).sum())
    if corners:
        add('INFO', f"{corners} sharp corner(s) without a UV split (gradients in the bake)",
            select='CORNER_NO_SPLIT')
    mirror_hard = int((sharp & on_mirror).sum())
    if mirror_hard:
        add('ERROR', f"{mirror_hard} hard edge(s) on the mirror line (keep these soft)",
            select='MIRROR_HARD', fix='SOFTEN_MIRROR')
    if settings.edge_source == 'SEAMS' and not edges_from_seams(me).any():
        add('ERROR', "No UV seams marked (try Island Borders as the source)",
            fix='SEAMS_FROM_ISLANDS')

    skewed = int(geometry.skew_mask(me, settings.skew_angle).sum())
    if skewed:
        add('WARNING', f"{_plural(skewed, 'face')} with strongly bent shading "
                       "(skew / waviness risk): harden, bevel or add support",
            select='SKEW')

    # UV layout, plus the per-object data the texture-set checks need
    tri_uv, tri_face = triangle_uvs(me)
    if len(tri_uv):
        degenerate = int(zero_uv_faces(tri_uv, tri_face, len(me.polygons)).sum())
        if degenerate:
            add('ERROR', f"{degenerate} face(s) with zero UV area", select='ZERO_UV')
        centres = tri_uv.mean(axis=1)
        outside = len(np.unique(tri_face[((centres < 0) | (centres > 1)).any(axis=1)]))
        if outside:
            add('INFO', f"{outside} face(s) outside the 0-1 UV tile", select='OUTSIDE_UV')
        ms = mapsize.of_object(settings, obj)
        if not mapsize.ratio_matches(me, ms):
            drawn = mapsize.uv_ratio(me)
            add('WARNING', f"UVs drawn for a {ratio_text(drawn)} map, but the map is "
                           f"{mapsize.label(ms)} (texels won't be square)",
                fix='MATCH_UV_RATIO')
        islands = uv_islands(me, uv_split)
        result.tex_set = obj.active_material.name if obj.active_material else obj.name
        result.tri_uv = np.asarray(tri_uv, dtype=np.float32)
        result.tri_island = islands[tri_face].astype(np.int32)
        result.flips = uv_stack.island_flipped(tri_uv, tri_face, islands)
        size = mapsize.texel_size(ms)
        scale_length = settings.id_data.unit_settings.scale_length or 1.0
        ids, density, world = texel.island_densities(obj, me, tri_uv, tri_face, islands,
                                                     size, scale_length)
        if len(ids):
            result.texel = (ids, density, world)

    # Modifiers and normals
    if TRIANGULATE_MOD not in obj.modifiers:
        untri = int(geometry.untriangulated_mask(me).sum())
        if untri:
            add('INFO', f"{_plural(untri, 'quad/n-gon', 'quads/n-gons')} not triangulated "
                        "(baker and engine may split them differently)",
                select='NOT_TRIANGULATED', fix='TRIANGULATE')
    if me.has_custom_normals:
        add('INFO', "Has custom normals (they override sharp edges)",
            fix='CLEAR_CUSTOM_NORMALS')
    if any(is_auto_smooth_mod(m) for m in obj.modifiers):
        add('INFO', "Has a Smooth by Angle modifier", fix='STRIP_AUTO_SMOOTH')
    for mod in obj.modifiers:
        if mod.type in UV_OFFSET_KINDS and mod.show_viewport and not uv_offset_ok(mod):
            add('ERROR', f"{mod.name}: copies overlap in UV space",
                fix='UV_OFFSET', fix_data=mod.name)
    return result


def check_meshes(objects, settings, high_collection=None, cache=None):
    """Mesh, UV and modifier checks for mesh objects (in Object Mode).

    `cache` (optional, an issues.ObjectCache) supplies results for unchanged objects
    and stores new ones.
    """
    issues = []
    texture_sets = {}  # material name -> [(label, tri_uv, tri_island)]
    texel_sets = {}    # material name -> [(obj, island ids, density, area)]
    flips = {}         # (label, island) -> mirrored?
    sizes = {}         # material name -> map size (mapsize.of)

    for obj in objects:
        high = is_high(obj, high_collection)
        key = settings_key(settings, high, obj)
        result = cache.get(obj, key) if cache is not None else None
        if result is None:
            result = check_object(obj, settings, high)
            if cache is not None:
                cache.put(obj, key, result)
        issues += result.issues
        if result.tex_set:
            sizes.setdefault(result.tex_set, mapsize.of_object(settings, obj))
            texture_sets.setdefault(result.tex_set, []).append(
                (obj.name, result.tri_uv, result.tri_island))
            for isl, mirrored in result.flips.items():
                flips[(obj.name, isl)] = mirrored
            if result.texel is not None:
                texel_sets.setdefault(result.tex_set, []).append((obj, *result.texel))

    for tex_set, entries in texture_sets.items():
        ms = sizes[tex_set]
        padding = mapsize.padding(ms)
        overlaps, close, stacked = check_uv_layout(entries, mapsize.padding_size(ms), padding,
                                                   details=True)
        head = TEXTURE_SET_PREFIX + tex_set
        members = tuple(sorted({label for label, _, _ in entries}))
        if stacked:
            moves = uv_stack.choose_moves(stacked, flips)
            involved = tuple(sorted({k[0] for pair in stacked for k in pair}))
            issues.append(Issue('MESH', 'ERROR', head,
                                f"{_plural(len(moves), 'stacked island')} (mirrored or duplicated "
                                "UVs bake into the same pixels)", involved,
                                select='STACKED', select_data="\n".join(involved),
                                fix='OFFSET_STACKED'))
        if overlaps:
            pairs = sorted({f"{a} / {b}" if a != b else a for a, b in overlaps})
            involved = tuple(sorted({n for pair in overlaps for n in pair}))
            issues.append(Issue('MESH', 'ERROR', head,
                                f"{len(overlaps)} overlapping island pair(s): "
                                f"{', '.join(pairs[:3])}",
                                involved, select='OVERLAP', select_data="\n".join(members),
                                fix='PACK_UVS'))
        if close:
            involved = tuple(sorted({n for pair in close for n in pair}))
            issues.append(Issue('MESH', 'ERROR', head,
                                f"{len(close)} island pair(s) closer than "
                                f"{padding}px at {mapsize.label(ms)}", involved,
                                select='TOO_CLOSE', select_data="\n".join(members),
                                fix='PACK_UVS'))
        texel_issues(issues, head, members, texel_sets.get(tex_set, []), settings, ms)
    return issues


def texel_issues(issues, head, members, entries, settings, ms):
    """Append texel density issues for one texture set whose map size is `ms`."""
    if not entries:
        return
    all_density = np.concatenate([e[2] for e in entries])
    all_area = np.concatenate([e[3] for e in entries])
    median = texel.weighted_median(all_density, all_area)
    tol = settings.texel_tolerance / 100
    unit = getattr(settings, "texel_unit", 'M')

    def num(v):
        return texel.fmt(v, unit, label=False)

    if any(e[0].active_material for e in entries):  # a real texture set, not a lone object
        issues.append(Issue('MESH', 'INFO', head,
                            f"Texel density {texel.fmt(median, unit)} at {mapsize.label(ms)} "
                            f"(islands range {num(all_density.min())}-{num(all_density.max())})",
                            members))
    for obj, ids, density, _ in entries:
        off_ids, off_d = texel.outlier_islands(ids, density, median, tol)
        if len(off_ids):
            issues.append(Issue(
                'MESH', 'WARNING', obj.name,
                f"{_plural(len(off_ids), 'UV island')} off the texel density by more than "
                f"{settings.texel_tolerance:.0f}% ({num(off_d.min())}-"
                f"{texel.fmt(off_d.max(), unit)}, set: {num(median)})", (obj.name,),
                select='TEXEL', select_data=f"{median:.6g}",
                fix='MATCH_TEXEL', fix_data=f"{median:.6g}"))


# --- Projection checks ---

def check_projection(objects, settings, depsgraph):
    """Low/high ray distance checks for every pair among `objects`."""
    issues = []
    for base, low, highs, res, tolerance, search in projection.analyse_pairs(
            objects, settings, depsgraph):
        names = (low.name, *(h.name for h in highs))

        def add(sev, msg, select="", low=low, names=names, base=base):
            issues.append(Issue('PROJECTION', sev, low.name, msg, names, select,
                                select_data=base))

        def extra(n):
            return f", plus {_plural(n, 'face')} made by modifiers" if n else ""

        miss, far = int(res["miss"].sum()), int(res["far"].sum())
        extra_miss, extra_far = res["extra_miss"], res["extra_far"]
        if miss or extra_miss:
            add('ERROR', f"{_plural(miss, 'face')} where rays find no high within "
                         f"{fmt_length(search)}{extra(extra_miss)} (black or garbage pixels)",
                'PROJ_MISS' if miss else "")
        if far or extra_far:
            add('WARNING', f"{_plural(far, 'face')} more than {fmt_length(tolerance)} from "
                           f"the high{extra(extra_far)} (skew / waviness risk)",
                'PROJ_FAR' if far else "")
        if res["hits"]:
            add('INFO', f"Ray distance needed: front {fmt_length(res['front'])}, "
                        f"rear {fmt_length(res['rear'])} (add a small margin)")
    return issues


# --- Naming checks ---

def check_names(objects, lows=None, highs=None):
    """Low/high naming checks.

    lows / highs: meshes in the Low and High collections, if set. Returns
    (issues, number of base names found).
    """
    issues = []

    def add(obj, severity, msg, fix="", fix_data=""):
        issues.append(Issue('NAMES', severity, obj.name, msg, (obj.name,), fix=fix,
                            fix_data=fix_data))

    # Match & Rename needs Low / High collections to work from.
    match = 'MATCH_NAMES' if lows is not None else ""

    groups = {}  # base -> {"low": [...], "high": [...], "styles": {...}}
    for obj in objects:
        name = obj.name
        if DUP_SUFFIX_RE.search(name):
            add(obj, 'ERROR', "Name ends in a .### duplicate suffix (bakers won't match it)",
                fix='STRIP_DUP_SUFFIX')
            name = DUP_SUFFIX_RE.sub("", name)
        pair = parse_pair(name)
        if not pair:
            add(obj, 'ERROR', "No _low / _high suffix", fix=match)
        else:
            group = groups.setdefault(pair.base, {"low": [], "high": [], "styles": set()})
            group[pair.role].append(obj)
            group["styles"].add(pair.style)
            if lows is not None:
                if pair.role == "low" and obj not in lows:
                    add(obj, 'ERROR', "Named _low but sits in the High collection",
                        fix='MOVE_COLLECTION', fix_data="low")
                if pair.role == "high" and obj not in highs:
                    add(obj, 'ERROR', "Named _high but sits in the Low collection",
                        fix='MOVE_COLLECTION', fix_data="high")
        if obj.data.users > 1:
            partners = [o.name for o in objects if o is not obj and o.data is obj.data]
            if partners:
                add(obj, 'ERROR', f"Shares its mesh with {', '.join(partners[:2])} "
                                  "(edits to one change the other)", fix='SPLIT_MESH')
        elif obj.data.name != obj.name:
            add(obj, 'INFO', f"Mesh data is named '{obj.data.name}'", fix='SYNC_DATA_NAME')

    by_lower = {}
    for base in groups:
        by_lower.setdefault(base.lower(), []).append(base)
    for base, parts in groups.items():
        twins = [b for b in by_lower[base.lower()] if b != base]
        for obj in parts["low"] + parts["high"]:
            if twins:
                add(obj, 'ERROR', f"Differs only in case from '{twins[0]}' (bakers won't match it)",
                    fix=match)
        if parts["high"] and not parts["low"] and not twins:
            for obj in parts["high"]:
                add(obj, 'ERROR', "No matching _low")
        if parts["low"] and not parts["high"] and not twins:
            add(parts["low"][0], 'ERROR', "No matching _high")
        if len(parts["styles"]) > 1 and parts["low"] and parts["high"]:
            styles = " and ".join(sorted(style_label(s) for s in parts["styles"]))
            for obj in parts["low"] + parts["high"]:
                add(obj, 'ERROR', f"Pair mixes {styles} suffixes (bakers match one style)",
                    fix=match)
        if len(parts["low"]) > 1:
            add(parts["low"][1], 'ERROR', f"More than one low named '{base}'", fix=match)
    return issues, len(groups)
