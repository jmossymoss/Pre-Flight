# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Finding a texture set's exported maps in a folder (operators/textures.py wires them in).

Handles the file names of Substance 3D Painter's default export presets:

    [$mesh_]$textureSet_<Channel>[_$colorSpace][.$udim]     most presets
    $mesh_<Channel>.$textureSet                             UDIM Legacy (set = tile)
    $project.sp/$textureSet.ts/<channel>                    KeyShot 9+
    <Channel>/$textureSet_<Channel>                         PBR (folders or PSD groups)

The texture set is the material's name. When a file name doesn't carry it, a set
named after an object using the material, or the only unclaimed set when one
material is left, is used instead.
"""

import os
import re

IMAGE_EXTS = {".png", ".tga", ".tif", ".tiff", ".jpg", ".jpeg", ".exr", ".bmp", ".dds",
              ".webp", ".hdr", ".psd"}
MAX_DEPTH = 3  # folder levels searched below the texture folder

# Channel ids. Single-map channels feed one input; packed ones are split (see PACKED).
CHANNELS = {
    'BASE_COLOR': ("basecolor", "color", "colour", "diffuse", "diff", "albedo",
                   "albedotransparency", "basemap", "base", "alb", "colormap",
                   "diffusecolor", "d", "bc", "col"),
    'ROUGHNESS': ("roughness", "rough", "rgh", "roughnessmap", "specularroughness", "r"),
    'GLOSSINESS': ("glossiness", "gloss"),
    'METALLIC': ("metallic", "metalness", "metal", "met", "metalnessmap", "basemetalness",
                 "metalnessmask", "m"),
    'NORMAL': ("normal", "normalmap", "nrm", "nor", "n", "normalopengl", "normaldirectx",
               "normalgl", "normaldx"),
    'NORMAL_GLOSS': ("ddna",),                     # CryEngine / Lumberyard: A = gloss
    'HEIGHT': ("height", "displacement", "displaceheightfield", "disp", "h"),
    'AO': ("ao", "ambientocclusion", "occlusion", "mixedao", "aomixed"),
    'EMISSION': ("emissive", "emission", "emissioncolor", "emissivecolor", "glowcolor",
                 "emit", "e"),
    'OPACITY': ("opacity", "alpha", "transparency", "presence", "o"),
    'ORM': ("occlusionroughnessmetallic", "orm"),  # R AO, G rough, B metal
    'MASK_MAP': ("maskmap",),                      # Unity HDRP: R metal, G AO, A smooth
    'METALLIC_SMOOTHNESS': ("metallicsmoothness",),     # Unity URP: R metal, A smooth
    'SPECULAR_SMOOTHNESS': ("specularsmoothness",),     # Unity URP spec: RGB, A smooth
    'SPECULAR_GLOSSINESS': ("specularglossiness",),     # Non-PBR: RGB spec, A gloss
    'MATERIAL_PARAMS': ("materialparams",),        # Lens Studio: R metal, G rough, B AO
    'SPECULAR': ("specular", "spec", "reflection", "specularfacecolor", "specularcolor"),
    'SPECULAR_LEVEL': ("specularlevel", "speclevel", "specularweight"),
    'SPECULAR_EDGE': ("specularedgecolor",),
    'ANISO_LEVEL': ("anisotropylevel", "anisolevel", "anisotropic", "anisotropy"),
    'ANISO_ANGLE': ("anisotropyangle", "anisoangle", "anisotropicangle",
                    "anisotropyrotation"),
    'TRANSMISSION': ("transmissive", "transmission", "transmissionweight"),
    'SSS': ("sss", "scattering", "subsurfaceweight", "translucency"),
    'SSS_COLOR': ("scatteringcolor", "subsurfacecolor", "scattercolor"),
    'SHEEN': ("sheenopacity", "sheen", "fuzzweight", "sheenweight"),
    'SHEEN_COLOR': ("sheencolor", "fuzzcolor"),
    'SHEEN_ROUGHNESS': ("sheenroughness", "fuzzroughness"),
    'COAT': ("coatopacity", "coat", "coatweight"),
    'COAT_COLOR': ("coatcolor",),
    'COAT_ROUGHNESS': ("coatroughness",),
    'COAT_NORMAL': ("coatnormal",),
    'IOR': ("ior",),
}
_TOKEN_TO_CHANNEL = {t: c for c, tokens in CHANNELS.items() for t in tokens}

# Recognised channels that aren't material inputs (Dota 2's masks, Painter's user and
# mesh-map channels), so they aren't mistaken for part of a set name.
IGNORED = {"detailmask", "diffusemask", "selfillummask", "specmask", "rimmask",
           "basetintmask", "specexp", "blendingmask", "absorptioncolor",
           "transmissioncolor", "coatspecularlevel", "thinfilmweight", "basediffuseroughness",
           "baseweight", "subsurfaceradiusscale", "coatroughnessanisotropy",
           "specularroughnessanisotropy", "coattangent", "tangent", "normalbase",
           "worldspacenormals", "id", "position", "bentnormalsbase", "curvature",
           "thickness", "heightbase", "opacitybase", "view2d", "scatterdistancescale"} | {
    f"user{i}" for i in range(32)}

NON_COLOR = set(CHANNELS) - {'BASE_COLOR', 'EMISSION', 'SPECULAR', 'SPECULAR_EDGE',
                             'SSS_COLOR', 'SHEEN_COLOR', 'COAT_COLOR', 'SPECULAR_SMOOTHNESS',
                             'SPECULAR_GLOSSINESS'}
ORDER = tuple(CHANNELS)

# Painter's default presets: name -> (exact channel names, {token: channel override}).
# Overrides re-map a token the preset uses differently (KeyShot 9+'s 'specular' is a level).
PRESETS = {
    "PBR Metallic Roughness": ("BaseColor Roughness Metallic Normal Height Emissive", {}),
    "PBR Specular Glossiness": ("Diffuse Specular Glossiness Normal Height Emissive", {}),
    "Unreal Engine (Packed)": ("BaseColor OcclusionRoughnessMetallic Normal Emissive", {}),
    "Unity HDRP (Metallic Standard)": ("BaseMap MaskMap Normal Emissive", {}),
    "Unity HDRP (Specular)": ("BaseMap Specular MaskMap Normal Emissive", {}),
    "Unity URP (Metallic Standard)": ("AlbedoTransparency MetallicSmoothness Normal Emission", {}),
    "Unity URP (Specular)": ("AlbedoTransparency SpecularSmoothness Normal Emission", {}),
    "Blender (Principled BSDF)": ("BaseColor Normal Metallic Roughness Emission Alpha Displacement", {}),
    "Arnold (AiStandard)": ("BaseColor Metalness Roughness Normal Height Emissive", {}),
    "Corona": ("Diffuse Reflection Glossiness IOR Normal Height Emissive", {}),
    "CryEngine / Lumberyard": ("diff spec ddna", {}),
    "Dota 2": ("color transparency normal detailmask diffusemask metalnessmask selfillummask "
               "specmask rimmask basetintmask specexp", {}),
    "KeyShot 9+": ("base_color metallic roughness specular anisotropic anisotropic_angle normal "
                   "opacity height emissive", {"specular": 'SPECULAR_LEVEL'}),
    "KeyShot": ("Diffuse Normal Reflection Roughness Metallic", {}),
    "Lens Studio": ("base normal materialParams", {}),
    "Maxwell (Metallic Roughness)": ("BaseColor Roughness Metallic Normal Height AnisoLevel "
                                     "AnisoAngle AO Opacity SSS Emission", {}),
    "Maxwell (Specular Glossiness)": ("Diffuse Specular Glossiness Normal Height AnisoLevel "
                                      "AnisoAngle AO Opacity SSS Emission", {}),
    "Non-PBR Specular Glossiness": ("Diffuse Normal SpecularGlossiness Emissive", {}),
    "Redshift (rsMaterial)": ("Color Roughness Metalness Normal DisplaceHeightField "
                              "EmissionColor", {}),
    "Renderman (pxrDisney)": ("BaseColor Roughness Metallic Normal Displacement Emissive "
                              "Presence", {}),
    "Renderman (pxrSurface)": ("DiffuseColor SpecularFaceColor Normal SpecularRoughness "
                               "GlowColor Presence Displacement", {}),
    "Roblox (MaterialVariant)": ("ALB NRM MET RGH", {}),
    "Roblox (SurfaceAppearance)": ("ColorMap NormalMap MetalnessMap RoughnessMap", {}),
    "Shade 3D": ("Diffuse Normal Roughness Specular", {}),
    "Spark AR Studio": ("Color ORM Emissive Normal", {}),
    "Vray Next (Metallic Roughness)": ("BaseColor Roughness Metallic Height Normal Emissive "
                                       "Transmissive AnisotropyAngle AnisotropyLevel", {}),
    "Vray Next (Specular Glossiness)": ("Diffuse Specular Glossiness Height Normal Emissive "
                                        "Transmissive AnisotropyAngle AnisotropyLevel", {}),
    "glTF PBR Metal Roughness": ("baseColor normal occlusionRoughnessMetallic emissive "
                                 "displacement", {}),
    "USDz (Apple AR)": ("diffuseColor emissiveColor metallic roughness normal occlusion "
                        "opacity", {}),
    "Substance 3D Stager": ("base_color specular_edge_color emission_color anisotropy "
                            "anisotropy_rotation specular_level sheen sheen_roughness "
                            "sheen_color coat coat_color coat_roughness coat_specular_level "
                            "coat_normal ambient_occlusion height opacity roughness metallic "
                            "normal translucency", {}),
    "Document channels + Normal + AO": ("BaseColor Height Roughness Metallic Normal AO "
                                        "Mixed_AO Emissive Opacity", {}),
}
_PRESET_TOKENS = {name: set(spec[0].split()) for name, spec in PRESETS.items()}

UDIM_RE = re.compile(r"^(.*)[._](1\d{3})$")
# Colour-space suffixes (OCIO / ACES projects): "_ACES - ACEScg", "_sRGB", "_Raw"...
COLORSPACE_RE = re.compile(r"(aces|srgb|raw|linear|rec\.?709|rec\.?2020|acescg|utility|"
                           r"texture|display|p3|gamma|scene|non-?color|data|lin|xyz|adobe)",
                           re.IGNORECASE)


# --- File names ---

def _norm(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def channel_of(token):
    """Channel id for a channel name ('BaseColor', 'Normal_DirectX'...), 'IGNORED', or None."""
    t = _norm(token)
    if t in _TOKEN_TO_CHANNEL:
        return _TOKEN_TO_CHANNEL[t]
    return 'IGNORED' if t in IGNORED else None


def _safe(name):
    """Painter replaces characters it can't put in a file name with '_'."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


def _split(stem):
    """Every (prefix, channel token, channel) reading of a file stem.

    'Gun_Body_Normal_DirectX_ACES - ACEScg' -> ('Gun_Body', 'Normal_DirectX', 'NORMAL')...
    Channel names can span '_' (Normal_DirectX, Mixed_AO); anything after the channel
    must look like a colour space.
    """
    parts = stem.split("_")
    out = []
    for i in range(len(parts)):
        for j in range(len(parts), i, -1):
            token = "_".join(parts[i:j])
            channel = channel_of(token)
            if channel is None:
                continue
            tail = "_".join(parts[j:])
            if tail and not (COLORSPACE_RE.search(tail) or " " in tail):
                continue
            out.append(("_".join(parts[:i]), token, channel))
    return out


# --- Texture sets ---

class Map:
    """One channel's file(s): a single image, or a UDIM set."""
    __slots__ = ("channel", "token", "path", "tiles", "files", "mtime")

    def __init__(self, channel, token, path, tiles, files, mtime):
        self.channel, self.token, self.path = channel, token, path
        self.tiles, self.files, self.mtime = tiles, files, mtime

    @property
    def is_udim(self):
        return bool(self.tiles)


class TextureSet:
    """The maps found for one material."""

    def __init__(self, maps, matched_by):
        self.maps = maps              # {channel: Map}
        self.matched_by = matched_by  # 'name', 'object' or 'only set left'
        self.preset, self.overrides = detect_preset({m.token for m in maps.values()})
        for m in maps.values():
            override = self.overrides.get(m.token)
            if override:
                m.channel = override
        self.maps = {m.channel: m for m in maps.values()}


def detect_preset(tokens):
    """(preset name, overrides) of the Painter preset whose channel names best match
    `tokens`, or (None, {}) when none matches well.

    Spelling is exact: glTF's 'baseColor' isn't Unreal's 'BaseColor'.
    """
    best, score = None, 0.0
    for name, names in _PRESET_TOKENS.items():
        inter = len(tokens & names)
        if not inter:
            continue
        s = inter / len(tokens | names)
        if s > score:
            best, score = name, s
    if best is None or score < 0.5:
        return None, {}
    return best, PRESETS[best][1]


# --- Folder matching ---

def _scan(folder):
    """(relative dir parts, stem, ext, full path) for every image up to MAX_DEPTH deep."""
    out = []
    base_depth = folder.rstrip("\\/").count(os.sep)
    for root, dirs, files in os.walk(folder):
        if root.count(os.sep) - base_depth >= MAX_DEPTH:
            dirs[:] = []
        rel = os.path.relpath(root, folder)
        rel_parts = [] if rel == "." else rel.split(os.sep)
        for entry in files:
            stem, ext = os.path.splitext(entry)
            if ext.lower() in IMAGE_EXTS:
                out.append((rel_parts, stem, ext, os.path.join(root, entry)))
    return out


def _candidates(folder):
    """Every reading of every file: [(set name, token, channel, file key, tile, path)].

    The set name is the texture set (or mesh) the file names; the file key
    (dir, stem without tile, ext) groups a UDIM set's tiles.
    """
    out = []
    for rel_parts, stem, ext, path in _scan(folder):
        tile = None
        m = UDIM_RE.match(stem)
        if m:
            stem, tile = m.group(1), int(m.group(2))
        key = (os.path.dirname(path), stem, ext.lower())
        parent = rel_parts[-1] if rel_parts else ""
        for prefix, token, channel in _split(stem):
            if prefix:
                out.append((prefix, token, channel, key, tile, path))
            elif parent:  # KeyShot 9+: <set>.ts/<channel>; Stager: Material_<x>/<channel>
                out.append((re.sub(r"\.ts$", "", parent), token, channel, key, tile, path))
    return out


def _name_matches(prefix, name):
    """True if the prefix is `name`, or a mesh name followed by `_name`."""
    p, n = prefix.lower(), name.lower()
    return p == n or p.endswith("_" + n)


def match_folder(folder, materials, object_names=None):
    """{material name: TextureSet} for the maps in `folder`.

    materials: material names.
    object_names: {material name: [names of objects using it]}, for presets that
        name files after the mesh.
    A file goes to the material with the longest matching name, so 'Gun_Body' wins
    'Gun_Body_BaseColor' over 'Body'.
    """
    object_names = object_names or {}
    cands = _candidates(folder)
    variants = {mat: {mat, _safe(mat)} for mat in materials}
    claims = {}  # file key -> (score, material, token, channel)

    def claim(key, score, mat, token, channel):
        old = claims.get(key)
        if old is None or score > old[0]:
            claims[key] = (score, mat, token, channel)

    for prefix, token, channel, key, tile, path in cands:
        if channel == 'IGNORED':
            continue
        for mat in materials:
            names = [(v, 2) for v in variants[mat]] + [(o, 1) for o in object_names.get(mat, ())]
            for name, kind in names:
                if _name_matches(prefix, name):
                    # Set names beat object names, then longer names, then exact matches.
                    claim(key, (kind, len(name), prefix.lower() == name.lower(),
                                len(token)), mat, token, channel)

    tiles = {}  # file key -> {tile: path}
    singles = {}
    for prefix, token, channel, key, tile, path in cands:
        if tile is None:
            singles[key] = path
        else:
            tiles.setdefault(key, {})[tile] = path

    def build(keys_by_channel):
        maps = {}
        for channel, (key, token) in keys_by_channel.items():
            if key in tiles and key not in singles:
                tile_paths = tiles[key]
                numbers = sorted(tile_paths)
                files = [tile_paths[n] for n in numbers]
                maps[channel] = Map(channel, token, tile_paths[numbers[0]], numbers, files,
                                    max(os.path.getmtime(f) for f in files))
            else:
                path = singles[key]
                maps[channel] = Map(channel, token, path, [], [path], os.path.getmtime(path))
        return maps

    def add(per_mat, mat, key, token, channel):
        chans = per_mat.setdefault(mat, {})
        old = chans.get(channel)
        newest = max([os.path.getmtime(p) for p in tiles.get(key, {}).values()] or
                     [os.path.getmtime(singles[key])])
        if old is None or newest > old[2]:  # same channel twice (PNG and TGA): newest wins
            chans[channel] = (key, token, newest)

    per_mat = {}
    for key, (score, mat, token, channel) in claims.items():
        add(per_mat, mat, key, token, channel)
    result = {}
    for mat, chans in per_mat.items():
        by = 'name' if any(claims[k][0][0] == 2 for k, _, _ in chans.values()) else 'object'
        result[mat] = TextureSet(build({c: (k, t) for c, (k, t, _) in chans.items()}), by)

    # One material left without maps and one unclaimed set of files: pair them up.
    left = [m for m in materials if m not in result]
    if len(left) == 1:
        best = {}  # each unclaimed file's best reading: the longest channel name
        for prefix, token, channel, key, tile, path in cands:
            if key in claims or channel == 'IGNORED':
                continue
            if key not in best or len(token) > len(best[key][1]):
                best[key] = (prefix, token, channel)
        groups = {}
        for key, (prefix, token, channel) in best.items():
            groups.setdefault(prefix.lower(), []).append((key, token, channel))
        sets = [g for g in groups.values() if len({c for _, _, c in g}) >= 2]
        if len(sets) == 1:
            per = {}
            for key, token, channel in sets[0]:
                add(per, left[0], key, token, channel)
            chans = per[left[0]]
            result[left[0]] = TextureSet(build({c: (k, t) for c, (k, t, _) in chans.items()}),
                                         'only set left')
    return result


def find_maps(folder, material):
    """{channel: Map} for one material (see match_folder)."""
    found = match_folder(folder, [material]).get(material)
    return found.maps if found else {}
