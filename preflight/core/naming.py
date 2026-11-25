# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Low / high naming, matching and renaming."""

import re

import bpy
import numpy as np
from mathutils import Vector

# Suffix styles bakers are set up with: (id, low, high, label).
SUFFIX_STYLES = (
    ('LOW_HIGH', "low", "high", "_low / _high"),
    ('LO_HI', "lo", "hi", "_lo / _hi"),
    ('LP_HP', "lp", "hp", "_lp / _hp"),
    ('L_H', "l", "h", "_l / _h"),
)
_ROLE = {"low": "low", "lo": "low", "lp": "low", "l": "low",
         "high": "high", "hi": "high", "hp": "high", "h": "high"}

# Multi-letter suffixes match in any case (_LP, _High); one-letter ones only in
# lower case, since _L / _R mark left and right sides in Blender.
_TOKEN = r"(?:(?i:low|lo|lp|high|hi|hp)|l|h)"
SUFFIX_RE = re.compile(r"(?:[_\-. ](?i:low|lo|lp|high|hi|hp)|_[lh])(_\d+)?$")
PAIR_RE = re.compile(rf"^(.*)_({_TOKEN})(_\d+)?$")
LOW_TOKEN_RE = re.compile(r"(?:[_\-. ](?i:low|lo|lp)|_l)((?:[._]\d+)?)$")
HIGH_TOKEN_RE = re.compile(r"(?:[_\-. ](?i:high|hi|hp)|_h)((?:[._]\d+)?)$")
DUP_SUFFIX_RE = re.compile(r"\.\d{3,}$")


# --- Parsing ---

class Pair:
    """A parsed <base>_<low|high token>[_n] name."""
    __slots__ = ("base", "token", "index")

    def __init__(self, base, token, index):
        self.base, self.token, self.index = base, token, index

    @property
    def role(self):
        """'low' or 'high', whatever the suffix style."""
        return _ROLE[self.token.lower()]

    @property
    def style(self):
        """The suffix style id ('LOW_HIGH', 'LP_HP', ...)."""
        token = self.token.lower()
        return next(s[0] for s in SUFFIX_STYLES if token in (s[1], s[2]))


def parse_pair(name):
    """Pair for 'X_low', 'X_HP_2', 'Bolt_l'...; None without a low/high suffix."""
    m = PAIR_RE.match(name)
    return Pair(m.group(1), m.group(2), m.group(3) or "") if m else None


def pair_role(name):
    """'low', 'high' or None."""
    pair = parse_pair(name)
    return pair.role if pair else None


def suffixes(style):
    """(low suffix, high suffix) for a style id, e.g. ('lp', 'hp')."""
    s = next((s for s in SUFFIX_STYLES if s[0] == style), SUFFIX_STYLES[0])
    return s[1], s[2]


def style_label(style):
    """Display label for a style id, e.g. '_lp / _hp'."""
    return next((s[3] for s in SUFFIX_STYLES if s[0] == style), SUFFIX_STYLES[0][3])


def base_name(name):
    """Name without its .### duplicate suffix and low/high suffix."""
    name = re.sub(r"\.\d{3}$", "", name)
    return SUFFIX_RE.sub("", name) or name


def pair_key(name, token_re):
    """Name with its low/high token replaced by a placeholder, or None.

    'Cube_01_low.003' and 'Cube_01_high.003' both give 'Cube_01_#.003'.
    """
    return token_re.sub(r"_#\1", name) if token_re.search(name) else None


def pair_groups(objects):
    """Group objects named <base>_low / <base>_high[_n] (any suffix style) by base."""
    groups = {}
    for obj in objects:
        pair = parse_pair(obj.name)
        if pair:
            groups.setdefault(pair.base, []).append(obj)
    return groups


def natural_key(name):
    """Sort key putting 'Part.002' before 'Part.010' and 'Part_low' before 'Part_low.001'."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


# --- Matching ---

def world_bounds(obj):
    """World-space (min, max) corners, padded so flat objects still have a volume."""
    pts = np.array([obj.matrix_world @ Vector(c) for c in obj.bound_box])
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    pad = max(np.linalg.norm(hi - lo) * 0.01, 1e-4)
    return lo - pad, hi + pad


def bounds_overlap(a, b):
    """Intersection over union of two (min, max) boxes."""
    lo, hi = np.maximum(a[0], b[0]), np.minimum(a[1], b[1])
    inter = np.prod(np.clip(hi - lo, 0, None))

    def volume(box):
        return np.prod(box[1] - box[0])

    return inter / (volume(a) + volume(b) - inter)


def match_low_high(lows, highs):
    """Assign every high to a low. Returns ({low: [highs]}, unmatched highs, matched by name).

    1. Names that already correspond win ('X_low.003' <-> 'X_high.003',
       'X_low' <-> 'X_high_2').
    2. The rest go by world-bounds overlap. Near-ties (parts stacked in the same
       spot) go to the low with the fewest highs so far, then the nearest.
    """
    pairs = {lo: [] for lo in lows}
    by_key = {}
    for lo in lows:
        key = pair_key(lo.name, LOW_TOKEN_RE)
        if key:
            by_key.setdefault(key, []).append(lo)

    remaining = []
    by_name = 0
    for hi in highs:
        key = pair_key(hi.name, HIGH_TOKEN_RE)
        match = None
        if key:
            for k in (key, re.sub(r"_\d+$", "", key)):  # X_high_2 is a floater of X_low
                if len(by_key.get(k, ())) == 1:
                    match = by_key[k][0]
                    break
        if match:
            pairs[match].append(hi)
            by_name += 1
        else:
            remaining.append(hi)

    low_bounds = {lo: world_bounds(lo) for lo in lows}

    def centre(bounds):
        return (bounds[0] + bounds[1]) / 2

    unmatched = []
    scored = []
    for hi in remaining:
        hb = world_bounds(hi)
        scores = {lo: bounds_overlap(low_bounds[lo], hb) for lo in lows}
        best = max(scores.values(), default=0)
        scored.append((best, hi, hb, scores))
    for best, hi, hb, scores in sorted(scored, key=lambda t: -t[0]):
        if best <= 0:
            unmatched.append(hi)
            continue
        tied = [lo for lo, sc in scores.items() if sc >= best * 0.98]
        pick = min(tied, key=lambda lo: (
            len(pairs[lo]), float(np.linalg.norm(centre(low_bounds[lo]) - centre(hb)))))
        pairs[pick].append(hi)
    return pairs, unmatched, by_name


def face_count(obj, depsgraph):
    """Face count after modifiers, so a Subdivision-based high still reads as high."""
    return len(obj.evaluated_get(depsgraph).data.polygons)


# --- Renaming ---

def free_base(base, objects, taken=(), style='LOW_HIGH'):
    """A base name whose <base>_low / <base>_high names are free.

    Names held by `objects` (or their meshes) count as free. Anything else is never
    renamed; the pair moves to <base>_01, <base>_02... instead, so low and high
    keep a matching base. `taken` holds lower-case bases already used.
    """
    owned = set(objects) | {o.data for o in objects if o.data}

    def free(name):
        for coll in (bpy.data.objects, bpy.data.meshes):
            holder = coll.get(name)
            if holder is not None and holder not in owned:
                return False
        return True

    candidate, k = base, 1
    while candidate.lower() in taken or not all(
            free(f"{candidate}_{sfx}") for sfx in suffixes(style)):
        candidate, k = numbered(base, k), k + 1
    return candidate


def numbered(base, k):
    """Base advanced by k: Cube_01 -> Cube_02, Cube_03...; Gun -> Gun_01, Gun_02..."""
    m = re.search(r"(\d+)$", base)
    if m:
        digits = m.group(1)
        return base[:m.start()] + str(int(digits) + k).zfill(len(digits))
    return f"{base}_{k:02d}"


def pair_names(base, low, highs, style='LOW_HIGH'):
    """[(object, new name)] for a low and its highs (the first high unnumbered)."""
    lo_sfx, hi_sfx = suffixes(style)
    return [(low, f"{base}_{lo_sfx}")] + [
        (hi, f"{base}_{hi_sfx}" + (f"_{i}" if i else "")) for i, hi in enumerate(highs)]


def apply_names(targets):
    """Rename [(object, name)] and their single-user meshes without colliding mid-batch."""
    for i, (obj, _) in enumerate(targets):
        obj.name = f"__preflight_tmp_{i}"
        if obj.data and obj.data.users == 1:
            obj.data.name = f"__preflight_tmp_{i}"
    for obj, name in targets:
        obj.name = name
        if obj.data and obj.data.users == 1:
            obj.data.name = name  # some bakers match on mesh data names


def unshare_pair_data(lows, highs):
    """Give highs that share a mesh with a low (Alt+D duplicates) their own copy.

    Returns the number of highs fixed.
    """
    low_data = {lo.data for lo in lows}
    fixed = 0
    for hi in highs:
        if hi.data in low_data:
            hi.data = hi.data.copy()
            fixed += 1
    return fixed
