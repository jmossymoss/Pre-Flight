# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Load exported texture maps into materials, reload them and flip normal map green."""

import os
import re

import bpy

from ..core import textures
from ..core.checks import is_high
from ..utils import selected_meshes

TAG = "preflight_channel"  # custom property marking the nodes Pre-Flight made

# Principled input -> where its value comes from, best first:
# (channel, output: 'Color' | 'Alpha' | 'Red' | 'Green' | 'Blue', invert)
SOURCES = {
    "Base Color": [('BASE_COLOR', 'Color', False)],
    "Metallic": [('METALLIC', 'Color', False), ('ORM', 'Blue', False),
                 ('MASK_MAP', 'Red', False), ('METALLIC_SMOOTHNESS', 'Red', False),
                 ('MATERIAL_PARAMS', 'Red', False)],
    "Roughness": [('ROUGHNESS', 'Color', False), ('ORM', 'Green', False),
                  ('MATERIAL_PARAMS', 'Green', False), ('MASK_MAP', 'Alpha', True),
                  ('METALLIC_SMOOTHNESS', 'Alpha', True), ('SPECULAR_SMOOTHNESS', 'Alpha', True),
                  ('SPECULAR_GLOSSINESS', 'Alpha', True), ('GLOSSINESS', 'Color', True),
                  ('NORMAL_GLOSS', 'Alpha', True)],
    "Alpha": [('OPACITY', 'Color', False)],
    "Emission Color": [('EMISSION', 'Color', False)],
    "Specular Tint": [('SPECULAR', 'Color', False), ('SPECULAR_SMOOTHNESS', 'Color', False),
                      ('SPECULAR_GLOSSINESS', 'Color', False), ('SPECULAR_EDGE', 'Color', False)],
    "Specular IOR Level": [('SPECULAR_LEVEL', 'Color', False)],
    "Anisotropic": [('ANISO_LEVEL', 'Color', False)],
    "Anisotropic Rotation": [('ANISO_ANGLE', 'Color', False)],
    "Transmission Weight": [('TRANSMISSION', 'Color', False)],
    "Subsurface Weight": [('SSS', 'Color', False)],
    "Sheen Weight": [('SHEEN', 'Color', False)],
    "Sheen Tint": [('SHEEN_COLOR', 'Color', False)],
    "Sheen Roughness": [('SHEEN_ROUGHNESS', 'Color', False)],
    "Coat Weight": [('COAT', 'Color', False)],
    "Coat Tint": [('COAT_COLOR', 'Color', False)],
    "Coat Roughness": [('COAT_ROUGHNESS', 'Color', False)],
}
NORMALS = {"Normal": ('NORMAL', 'NORMAL_GLOSS'), "Coat Normal": ('COAT_NORMAL',)}
# Base colour maps whose alpha is opacity (when there's no separate opacity map).
ALPHA_IN_BASE = {"albedotransparency", "basemap", "colormap"}
PACKED = {'ORM', 'MASK_MAP', 'METALLIC_SMOOTHNESS', 'SPECULAR_SMOOTHNESS',
          'SPECULAR_GLOSSINESS', 'MATERIAL_PARAMS', 'NORMAL_GLOSS'}
AO_SOURCES = [('AO', 'Color'), ('ORM', 'Red'), ('MASK_MAP', 'Green')]
AO_PARTS = ("ao:light", "ao:to_rgb", "ao:ramp", "ao:mix")

_watch = {"mtimes": {}}  # image name -> file mtime last seen by the auto reload watcher


# --- Target materials ---

def target_objects(context):
    """The selected meshes; with nothing selected, every low."""
    objects = selected_meshes(context)
    if not objects:
        high_coll = context.scene.preflight.high_collection
        objects = [o for o in context.scene.objects
                   if o.type == 'MESH' and not is_high(o, high_coll)]
    return objects


def target_materials(objects):
    """(materials, {material name: [object names]}) on `objects`."""
    seen, out, users = set(), [], {}
    for obj in objects:
        for slot in obj.material_slots:
            mat = slot.material
            if mat is None or mat.library:
                continue
            users.setdefault(mat.name, []).append(obj.name)
            if mat.name not in seen:
                seen.add(mat.name)
                out.append(mat)
    return out, users


def texture_folder(context):
    folder = context.scene.preflight.texture_folder
    return bpy.path.abspath(folder) if folder else ""


# --- Node setup ---

def _tagged(nodes, key):
    return next((n for n in nodes if n.get(TAG) == key), None)


def _node(nodes, key, bl_idname, location, hide=True):
    """The node Pre-Flight made for `key`, or a new one."""
    node = _tagged(nodes, key)
    if node is None or node.bl_idname != bl_idname:
        if node is not None:
            nodes.remove(node)
        node = nodes.new(bl_idname)
        node[TAG] = key
        node.location = location
        node.hide = hide
    return node


def _link(tree, out_socket, in_socket):
    for link in list(in_socket.links):
        tree.links.remove(link)
    tree.links.new(out_socket, in_socket)


def _principled(tree):
    """The Principled BSDF feeding the material output, created if missing."""
    nodes = tree.nodes
    out = next((n for n in nodes if n.bl_idname == 'ShaderNodeOutputMaterial'
                and n.is_active_output), None) or next(
        (n for n in nodes if n.bl_idname == 'ShaderNodeOutputMaterial'), None)
    if out is None:
        out = nodes.new('ShaderNodeOutputMaterial')
        out.location = (300, 0)
    surface = out.inputs["Surface"]
    if surface.links and surface.links[0].from_node.bl_idname == 'ShaderNodeBsdfPrincipled':
        return surface.links[0].from_node
    bsdf = next((n for n in nodes if n.bl_idname == 'ShaderNodeBsdfPrincipled'), None)
    if bsdf is None:
        bsdf = nodes.new('ShaderNodeBsdfPrincipled')
        bsdf.location = (out.location.x - 300, out.location.y)
    _link(tree, bsdf.outputs["BSDF"], surface)
    return bsdf


def _udim_path(path, tile):
    """'.../Body_BaseColor.1001.png' -> '.../Body_BaseColor.<UDIM>.png'."""
    folder, name = os.path.split(path)
    return os.path.join(folder, re.sub(rf"(?<=[._]){tile}(?=\.[^.]+$)", "<UDIM>", name, count=1))


def _load_image(m):
    """Load (or reuse and reload) the image for textures.Map `m`.

    A UDIM set loads as one tiled image with every tile found on disk.
    """
    image = bpy.data.images.load(m.path, check_existing=True)
    if m.is_udim:
        if image.source != 'TILED':
            image.source = 'TILED'
        image.filepath = _udim_path(m.path, m.tiles[0])
        for number in m.tiles:
            if image.tiles.get(number) is None:
                image.tiles.new(tile_number=number)
    image.reload()
    if m.channel in textures.NON_COLOR:
        try:
            image.colorspace_settings.name = 'Non-Color'
        except TypeError:
            pass
    if m.channel in PACKED:
        image.alpha_mode = 'CHANNEL_PACKED'  # alpha is data, don't premultiply
    return image


def setup_material(mat, tex_set, flip_green=None):
    """Wire a textures.TextureSet into `mat`. Returns the channels loaded."""
    mat.use_nodes = True
    tree = mat.node_tree
    nodes = tree.nodes
    bsdf = _principled(tree)
    maps = tex_set.maps
    x0, y0 = bsdf.location.x - 900, bsdf.location.y + 300
    images = {}
    for i, channel in enumerate(c for c in textures.ORDER if c in maps):
        node = _node(nodes, f"image:{channel}", 'ShaderNodeTexImage', (x0, y0 - 280 * i),
                     hide=False)
        node.image = _load_image(maps[channel])
        node.label = channel.replace("_", " ").title()
        images[channel] = node

    splits = {}

    def output(channel, part, invert):
        img = images[channel]
        if part in ('Color', 'Alpha'):
            sock = img.outputs[part]
        else:
            sep = splits.get(channel)
            if sep is None:
                sep = splits[channel] = _node(nodes, f"split:{channel}", 'ShaderNodeSeparateColor',
                                              (img.location.x + 300, img.location.y))
                _link(tree, img.outputs["Color"], sep.inputs["Color"])
            sock = sep.outputs[part]
        if invert:  # smoothness / glossiness -> roughness
            inv = _node(nodes, f"invert:{channel}:{part}", 'ShaderNodeMath',
                        (img.location.x + 450, img.location.y - 40))
            inv.operation = 'SUBTRACT'
            inv.inputs[0].default_value = 1.0
            _link(tree, sock, inv.inputs[1])
            sock = inv.outputs[0]
        return sock

    for name, sources in SOURCES.items():
        if name not in bsdf.inputs:
            continue
        for channel, part, invert in sources:
            if channel in images:
                _link(tree, output(channel, part, invert), bsdf.inputs[name])
                break
    base = maps.get('BASE_COLOR')
    if ('OPACITY' not in images and base is not None
            and textures._norm(base.token) in ALPHA_IN_BASE and "Alpha" in bsdf.inputs):
        _link(tree, images['BASE_COLOR'].outputs["Alpha"], bsdf.inputs["Alpha"])

    for name, channels in NORMALS.items():
        channel = next((c for c in channels if c in images), None)
        if channel is None or name not in bsdf.inputs:
            continue
        nmap = _node(nodes, f"normal_map:{name}", 'ShaderNodeNormalMap',
                     (bsdf.location.x - 200, images[channel].location.y))
        nmap[TAG + "_source"] = channel
        if flip_green is None:
            flip_green = bool(mat.get("preflight_flip_green", False))
        _wire_normal(tree, images[channel], nmap, flip_green)
        _link(tree, nmap.outputs["Normal"], bsdf.inputs[name])

    if 'EMISSION' in images:
        strength = bsdf.inputs.get("Emission Strength")
        if strength is not None and strength.default_value == 0 and not strength.links:
            strength.default_value = 1.0
    _wire_ao(tree, bsdf, images, output)
    # Height, IOR and subsurface colour load for reference but stay unplugged: the
    # Principled BSDF has no input for them (height would need displacement).
    return list(images)


def _wire_ao(tree, bsdf, images, output):
    """Multiply AO into Base Color only where the surface gets no direct light (EEVEE).

    A white Diffuse BSDF through Shader to RGB measures direct light; a Color Ramp turns
    that into the factor (1 in shadow, 0 in light) of a Multiply of base colour by AO.
    Cycles has no Shader to RGB, so there the AO drops out and Cycles' own occlusion
    applies. An existing ramp is left as is, so it can be tuned.
    """
    nodes = tree.nodes
    source = next(((c, p) for c, p in AO_SOURCES if c in images), None)
    if source is None or 'BASE_COLOR' not in images or "Base Color" not in bsdf.inputs:
        for key in AO_PARTS:
            node = _tagged(nodes, key)
            if node is not None:
                nodes.remove(node)
        return
    x, y = bsdf.location.x - 750, bsdf.location.y + 650
    fresh = _tagged(nodes, "ao:ramp") is None
    light = _node(nodes, "ao:light", 'ShaderNodeBsdfDiffuse', (x, y), hide=False)
    to_rgb = _node(nodes, "ao:to_rgb", 'ShaderNodeShaderToRGB', (x + 200, y), hide=False)
    ramp = _node(nodes, "ao:ramp", 'ShaderNodeValToRGB', (x + 380, y), hide=False)
    mix = _node(nodes, "ao:mix", 'ShaderNodeMix', (x + 680, y - 100), hide=False)
    light.label, ramp.label, mix.label = "Direct Light", "Shadow Mask", "AO in Shadow"
    light.inputs["Color"].default_value = (1, 1, 1, 1)
    if fresh:
        dark, lit = ramp.color_ramp.elements
        dark.position, dark.color = 0.0, (1, 1, 1, 1)   # unlit -> full AO
        lit.position, lit.color = 1.0, (0, 0, 0, 1)     # lit -> none
    mix.data_type, mix.blend_type, mix.clamp_result = 'RGBA', 'MULTIPLY', False
    normal = bsdf.inputs["Normal"].links
    if normal:  # light the mask with the same normal detail as the surface
        _link(tree, normal[0].from_socket, light.inputs["Normal"])
    _link(tree, light.outputs["BSDF"], to_rgb.inputs["Shader"])
    _link(tree, to_rgb.outputs["Color"], ramp.inputs["Fac"])
    _link(tree, ramp.outputs["Color"], mix.inputs[0])
    sockets = {s.identifier: s for s in mix.inputs}
    _link(tree, images['BASE_COLOR'].outputs["Color"], sockets["A_Color"])
    _link(tree, output(source[0], source[1], False), sockets["B_Color"])
    _link(tree, next(s for s in mix.outputs if s.identifier == "Result_Color"),
          bsdf.inputs["Base Color"])


def _wire_normal(tree, img, nmap, flip):
    """Image -> Normal Map node, straight or with green inverted (DirectX <-> OpenGL)."""
    nodes = tree.nodes
    key = nmap[TAG]
    if not flip:
        for part in ("flip_split", "flip_invert", "flip_combine"):
            node = _tagged(nodes, f"{key}:{part}")
            if node is not None:
                nodes.remove(node)
        _link(tree, img.outputs["Color"], nmap.inputs["Color"])
        return
    x, y = img.location.x + 300, img.location.y - 120
    sep = _node(nodes, f"{key}:flip_split", 'ShaderNodeSeparateColor', (x, y))
    inv = _node(nodes, f"{key}:flip_invert", 'ShaderNodeMath', (x + 150, y))
    comb = _node(nodes, f"{key}:flip_combine", 'ShaderNodeCombineColor', (x + 300, y))
    inv.operation = 'SUBTRACT'
    inv.inputs[0].default_value = 1.0
    inv.label = "Flip Green"
    _link(tree, img.outputs["Color"], sep.inputs["Color"])
    _link(tree, sep.outputs["Red"], comb.inputs["Red"])
    _link(tree, sep.outputs["Green"], inv.inputs[1])
    _link(tree, inv.outputs[0], comb.inputs["Green"])
    _link(tree, sep.outputs["Blue"], comb.inputs["Blue"])
    _link(tree, comb.outputs["Color"], nmap.inputs["Color"])


def normal_setups(mat):
    """(normal map node, image node) pairs that Pre-Flight set up in `mat`."""
    if mat.node_tree is None:
        return []
    nodes = mat.node_tree.nodes
    out = []
    for nmap in nodes:
        key = nmap.get(TAG, "")
        if nmap.bl_idname == 'ShaderNodeNormalMap' and key.startswith("normal_map:"):
            img = _tagged(nodes, f"image:{nmap.get(TAG + '_source', 'NORMAL')}")
            if img is not None:
                out.append((nmap, img))
    return out


def set_flip_green(mat, flip):
    """Flip or unflip the green channel of Pre-Flight's normal maps in `mat`."""
    setups = normal_setups(mat)
    for nmap, img in setups:
        _wire_normal(mat.node_tree, img, nmap, flip)
    mat["preflight_flip_green"] = flip
    return len(setups)


# --- Reload ---

def preflight_images():
    """Images used by nodes Pre-Flight set up, in any material."""
    out = set()
    for mat in bpy.data.materials:
        if mat.node_tree is None or mat.library:
            continue
        for node in mat.node_tree.nodes:
            if node.bl_idname == 'ShaderNodeTexImage' and node.get(TAG) and node.image:
                out.add(node.image)
    return out


def _image_mtime(image):
    """Newest modification time of the image's file(s), or None if missing."""
    path = bpy.path.abspath(image.filepath, library=image.library)
    if image.source == 'TILED':
        paths = [path.replace("<UDIM>", str(t.number)) for t in image.tiles]
    else:
        paths = [path]
    times = [os.path.getmtime(p) for p in paths if os.path.isfile(p)]
    return max(times) if times else None


def reload_images(images):
    count = 0
    for image in images:
        if image.filepath and image.source in {'FILE', 'TILED'}:
            image.reload()
            _watch["mtimes"][image.name] = _image_mtime(image)
            count += 1
    return count


def _auto_reload_tick():
    wm = bpy.context.window_manager
    if not getattr(getattr(wm, "preflight", None), "auto_reload_textures", False):
        return None  # turned off: stop the timer
    changed = []
    for image in preflight_images():
        mtime = _image_mtime(image)
        old = _watch["mtimes"].get(image.name)
        if old is None:
            _watch["mtimes"][image.name] = mtime
        elif mtime is not None and mtime > old:
            changed.append(image)
    if changed:
        reload_images(changed)
        for window in wm.windows:
            for area in window.screen.areas:
                if area.type in {'VIEW_3D', 'IMAGE_EDITOR', 'NODE_EDITOR'}:
                    area.tag_redraw()
    return _interval()


def _interval():
    from .. import preferences
    return preferences.get().auto_reload_interval


def _prime():
    """Record the files' current times, so only later exports count as changes."""
    _watch["mtimes"] = {image.name: _image_mtime(image) for image in preflight_images()}


def auto_reload_update(self, context):
    if self.auto_reload_textures:
        _prime()
        if not bpy.app.timers.is_registered(_auto_reload_tick):
            bpy.app.timers.register(_auto_reload_tick, first_interval=_interval())
    elif bpy.app.timers.is_registered(_auto_reload_tick):
        bpy.app.timers.unregister(_auto_reload_tick)


def resume_auto_reload():
    """Restart the watcher after the add-on (re)loads if Auto Reload is still on."""
    wm = bpy.context.window_manager
    settings = getattr(wm, "preflight", None) if wm else None
    if settings is not None and settings.auto_reload_textures:
        auto_reload_update(settings, bpy.context)
    return None


def unregister_timer():
    if bpy.app.timers.is_registered(resume_auto_reload):
        bpy.app.timers.unregister(resume_auto_reload)
    if bpy.app.timers.is_registered(_auto_reload_tick):
        bpy.app.timers.unregister(_auto_reload_tick)


# --- Operators ---

def _names(items, n=3):
    items = list(items)
    return ", ".join(items[:n]) + ("..." if len(items) > n else "")


class PREFLIGHT_OT_load_textures(bpy.types.Operator):
    """Find each material's exported maps in the texture folder (any of Painter's
default export presets: <material>_BaseColor, _OcclusionRoughnessMetallic, _MaskMap,
UDIM tiles...) and wire them into its Principled BSDF. Run again to pick up new
maps: Pre-Flight's nodes are reused, the rest of the node tree is left alone"""
    bl_idname = "preflight.load_textures"
    bl_label = "Load Textures"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(context.scene.preflight.texture_folder)

    def execute(self, context):
        folder = texture_folder(context)
        if not os.path.isdir(folder):
            self.report({'WARNING'}, f"Texture folder not found: {folder}")
            return {'CANCELLED'}
        materials, users = target_materials(target_objects(context))
        if not materials:
            self.report({'WARNING'}, "No materials on the selected meshes (or the lows)")
            return {'CANCELLED'}
        found = textures.match_folder(folder, [m.name for m in materials], users)
        loaded, maps_total, udims, guessed = 0, 0, 0, []
        for mat in materials:
            tex_set = found.get(mat.name)
            if tex_set is None:
                continue
            maps_total += len(setup_material(mat, tex_set))
            udims += sum(m.is_udim for m in tex_set.maps.values())
            loaded += 1
            if tex_set.matched_by != 'name':
                guessed.append(mat.name)
        _prime()
        missing = [m.name for m in materials if m.name not in found]
        if not loaded:
            self.report({'WARNING'}, f"No maps named after {_names(missing)} in {folder}")
            return {'CANCELLED'}
        msg = f"Loaded {maps_total} map(s) into {loaded} material(s)"
        if udims:
            msg += f", {udims} as UDIM sets"
        if guessed:
            msg += f"; matched by object name or elimination: {_names(guessed)}"
        if missing:
            msg += f"; none found for {_names(missing)}"
        self.report({'WARNING'} if missing else {'INFO'}, msg)
        return {'FINISHED'}


class PREFLIGHT_OT_reload_textures(bpy.types.Operator):
    """Reload the images Load Textures set up from disk (after exporting again)"""
    bl_idname = "preflight.reload_textures"
    bl_label = "Reload Textures"
    bl_options = {'REGISTER'}

    def execute(self, context):
        count = reload_images(preflight_images())
        if not count:
            self.report({'WARNING'}, "No textures loaded by Pre-Flight yet: use Load Textures")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Reloaded {count} texture(s)")
        return {'FINISHED'}


class PREFLIGHT_OT_flip_normal_green(bpy.types.Operator):
    """Flip the green channel of the loaded normal maps on the selected meshes' materials
(all lows' with nothing selected): converts DirectX (Y-) normal maps to OpenGL (Y+),
which Blender uses. Click again to flip back"""
    bl_idname = "preflight.flip_normal_green"
    bl_label = "Flip Normal Green"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        materials, _ = target_materials(target_objects(context))
        materials = [m for m in materials if normal_setups(m)]
        if not materials:
            self.report({'WARNING'}, "No normal maps loaded by Pre-Flight on these materials")
            return {'CANCELLED'}
        # All materials end up the same way: flipped unless all already are.
        flip = not all(m.get("preflight_flip_green", False) for m in materials)
        for mat in materials:
            set_flip_green(mat, flip)
        self.report({'INFO'}, f"Normal green {'flipped' if flip else 'restored'} on "
                              f"{len(materials)} material(s)")
        return {'FINISHED'}


classes = (PREFLIGHT_OT_load_textures, PREFLIGHT_OT_reload_textures,
           PREFLIGHT_OT_flip_normal_green)
