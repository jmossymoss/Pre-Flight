# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Boolean cutters and other modifier dependencies: finding, copying, rewiring."""

import bpy

from .naming import base_name


# --- Boolean cutters ---

def boolean_operands(obj):
    """Objects and collections an object's Boolean modifiers cut with."""
    objs, colls = [], []
    for mod in obj.modifiers:
        if mod.type != 'BOOLEAN':
            continue
        if getattr(mod, "operand_type", 'OBJECT') == 'COLLECTION':
            if mod.collection:
                colls.append(mod.collection)
        elif mod.object:
            objs.append(mod.object)
    return objs, colls


def collect_cutters(objects):
    """Every cutter object and cutter collection the objects use, following nested booleans."""
    cut_objs, cut_colls = [], []
    seen = set(objects)
    stack = list(objects)
    while stack:
        objs, colls = boolean_operands(stack.pop())
        for coll in colls:
            if coll not in cut_colls:
                cut_colls.append(coll)
            objs += list(coll.all_objects)
        for cutter in objs:
            if cutter not in seen:
                seen.add(cutter)
                cut_objs.append(cutter)
                stack.append(cutter)
    return cut_objs, cut_colls


def cutter_users():
    """{cutter object or collection: objects whose booleans use it directly}."""
    users = {}
    for obj in bpy.data.objects:
        objs, colls = boolean_operands(obj)
        for coll in colls:
            users.setdefault(coll, set()).add(obj)
            objs += list(coll.all_objects)
        for cutter in objs:
            users.setdefault(cutter, set()).add(obj)
    return users


def all_cutters():
    """Every object in the file used as a boolean cutter."""
    return {k for k in cutter_users() if isinstance(k, bpy.types.Object)}


def collection_meshes(collection):
    """Meshes in a collection, leaving out boolean cutters."""
    if not collection:
        return []
    cutters = all_cutters()
    return [o for o in collection.all_objects if o.type == 'MESH' and o not in cutters]


# --- Copying and rewiring ---

def _find_layer_collection(layer_coll, coll):
    if layer_coll.collection == coll:
        return layer_coll
    for child in layer_coll.children:
        found = _find_layer_collection(child, coll)
        if found:
            return found
    return None


def copy_visibility(src, dst):
    """Copy per-view-layer visibility, which ID.copy() and linking don't carry over.

    Covers the eye icon for objects, and the eye and exclude checkbox for collections.
    """
    for scene in bpy.data.scenes:
        for view_layer in scene.view_layers:
            if isinstance(src, bpy.types.Object):
                if src.name in view_layer.objects:
                    try:  # the copy may not be in this view layer's collections
                        dst.hide_set(src.hide_get(view_layer=view_layer), view_layer=view_layer)
                    except RuntimeError:
                        pass
            else:
                a = _find_layer_collection(view_layer.layer_collection, src)
                b = _find_layer_collection(view_layer.layer_collection, dst)
                if a and b:
                    b.exclude = a.exclude
                    b.hide_viewport = a.hide_viewport


def copy_cutters(cut_objs, cut_colls, label, mapping):
    """Duplicate cutters and cutter collections, recording original -> copy in `mapping`."""
    for coll in cut_colls:
        new = bpy.data.collections.new(f"{coll.name}_{label}")
        new.hide_viewport, new.hide_render = coll.hide_viewport, coll.hide_render
        parents = [c for c in bpy.data.collections if c.children.get(coll.name) == coll]
        parents += [sc.collection for sc in bpy.data.scenes
                    if sc.collection.children.get(coll.name) == coll]
        for parent in parents or [bpy.context.scene.collection]:
            parent.children.link(new)
        copy_visibility(coll, new)
        mapping[coll] = new
    for cutter in cut_objs:
        dup = cutter.copy()
        if cutter.data:
            dup.data = cutter.data.copy()
        dup.name = f"{base_name(cutter.name)}_{label}_cutter"
        homes = [mapping[c] for c in cutter.users_collection if c in mapping]
        for coll in homes or cutter.users_collection:
            coll.objects.link(dup)
        copy_visibility(cutter, dup)
        mapping[cutter] = dup


def _remap_struct(struct, mapping):
    for prop in struct.bl_rna.properties:
        if prop.type != 'POINTER' or prop.is_readonly:
            continue
        try:
            value = getattr(struct, prop.identifier)
        except AttributeError:
            continue
        if isinstance(value, bpy.types.ID) and value in mapping:
            setattr(struct, prop.identifier, mapping[value])


def _remap_node_inputs(mod, mapping):
    """Point Geometry Nodes object/collection inputs at their copies."""
    inputs = getattr(getattr(mod, "properties", None), "inputs", None)
    if inputs is not None:  # Blender 5.x: modifier.properties.inputs.<socket>.value
        for prop in inputs.bl_rna.properties:
            if prop.type == 'POINTER' and prop.identifier != "rna_type":
                socket = getattr(inputs, prop.identifier)
                value = getattr(socket, "value", None)
                if isinstance(value, bpy.types.ID) and value in mapping:
                    socket.value = mapping[value]
        return
    for key in list(mod.keys()):  # Blender 4.x: ID properties on the modifier
        value = mod[key]
        if isinstance(value, bpy.types.ID) and value in mapping:
            mod[key] = mapping[value]


def remap_references(obj, mapping):
    """Point an object's modifiers, Geometry Nodes inputs, constraints and parent at copies."""
    for mod in obj.modifiers:
        _remap_struct(mod, mapping)  # boolean object/collection, mirror object, array offset...
        if mod.type == 'NODES':
            _remap_node_inputs(mod, mapping)
    for con in obj.constraints:
        _remap_struct(con, mapping)
    if obj.parent in mapping:
        world = obj.matrix_world.copy()
        obj.parent = mapping[obj.parent]
        obj.matrix_world = world


# --- Low / High collections ---

def ensure_pair_collections(context):
    """The scene's (Low, High) collections, created if needed.

    Prefers the collections set in the panel, then existing local 'Low' / 'High'
    collections, then makes new ones under the scene collection.
    """
    props = context.scene.preflight
    scene_root = context.scene.collection
    for attr, name in (("low_collection", "Low"), ("high_collection", "High")):
        if getattr(props, attr) is None:
            coll = bpy.data.collections.get(name)
            if coll is None or coll.library:
                coll = bpy.data.collections.new(name)
            if coll not in scene_root.children_recursive:
                scene_root.children.link(coll)
            setattr(props, attr, coll)
    return props.low_collection, props.high_collection


def move_to_collection(obj, coll):
    """Move an object into a collection, out of any others."""
    if coll not in obj.users_collection:
        coll.objects.link(obj)
    for other in list(obj.users_collection):
        if other != coll:
            other.objects.unlink(obj)


def has_ancestor_in(obj, group):
    """True if any parent up the chain is in `group`."""
    parent = obj.parent
    while parent is not None:
        if parent in group:
            return True
        parent = parent.parent
    return False


# --- Modifier dependencies ---

def _referenced_ids(struct):
    for prop in struct.bl_rna.properties:
        if prop.type == 'POINTER' and prop.identifier != "rna_type":
            try:
                value = getattr(struct, prop.identifier)
            except AttributeError:
                continue
            if isinstance(value, bpy.types.ID):
                yield value


def _node_input_ids(mod):
    inputs = getattr(getattr(mod, "properties", None), "inputs", None)
    if inputs is not None:  # Blender 5.x
        for prop in inputs.bl_rna.properties:
            if prop.type == 'POINTER' and prop.identifier != "rna_type":
                value = getattr(getattr(inputs, prop.identifier), "value", None)
                if isinstance(value, bpy.types.ID):
                    yield value
    else:
        for key in mod.keys():
            if isinstance(mod[key], bpy.types.ID):
                yield mod[key]


def object_dependencies(objects):
    """Objects that `objects` need to evaluate correctly, followed recursively.

    Includes boolean cutters (also inside collection booleans), mirror objects,
    array offsets and Geometry Nodes object/collection inputs.
    """
    seen, deps = set(objects), []
    stack = list(objects)
    while stack:
        obj = stack.pop()
        found = []
        for mod in obj.modifiers:
            ids = list(_referenced_ids(mod))
            if mod.type == 'NODES':
                ids += list(_node_input_ids(mod))
            for id_block in ids:
                if isinstance(id_block, bpy.types.Object):
                    found.append(id_block)
                elif isinstance(id_block, bpy.types.Collection):
                    found += list(id_block.all_objects)
        for dep in found:
            if dep not in seen:
                seen.add(dep)
                deps.append(dep)
                stack.append(dep)
    return deps


def _modifier_ids(obj):
    for mod in obj.modifiers:
        yield from _referenced_ids(mod)
        if mod.type == 'NODES':
            yield from _node_input_ids(mod)


def dependency_users():
    """{object or collection: objects whose modifiers reference it}.

    A referenced collection's member objects count as referenced too.
    """
    users = {}
    for obj in bpy.data.objects:
        for id_block in _modifier_ids(obj):
            if isinstance(id_block, bpy.types.Object):
                users.setdefault(id_block, set()).add(obj)
            elif isinstance(id_block, bpy.types.Collection):
                users.setdefault(id_block, set()).add(obj)
                for member in id_block.all_objects:
                    users.setdefault(member, set()).add(obj)
    return users


def separate_dependencies(objects, label, parent_to=None, users=None):
    """Copy the cutters and helpers `objects` share with anything else, and rewire
    only `objects` (and their unshared dependencies) to the copies.

    parent_to: optional fn(copy) -> object to parent each top-level copy to.
    users: a precomputed dependency_users() result.
    Returns the copied objects.
    """
    users = users if users is not None else dependency_users()
    deps = object_dependencies(objects)
    inside = set(objects) | set(deps)
    colls = []
    for obj in inside:
        for id_block in _modifier_ids(obj):
            if isinstance(id_block, bpy.types.Collection) and id_block not in colls:
                colls.append(id_block)

    shared_colls = [c for c in colls if users.get(c, set()) - inside]
    in_shared = {o for c in shared_colls for o in c.all_objects}
    shared_objs = [d for d in deps if (users.get(d, set()) - inside) or d in in_shared]
    # Whatever a copied cutter depends on is copied too, or the copy would still
    # reach the other side's originals (e.g. a cutter's own nested cutter).
    downstream = set(object_dependencies(shared_objs))
    shared_objs += [d for d in deps if d in downstream and d not in shared_objs]
    for coll in colls:
        if coll not in shared_colls and set(coll.all_objects) & set(shared_objs):
            shared_colls.append(coll)
    if not shared_objs and not shared_colls:
        return []

    mapping = {}
    copy_cutters(shared_objs, shared_colls, label, mapping)
    copies = [mapping[o] for o in shared_objs]
    for obj in list(objects) + [d for d in deps if d not in mapping] + copies:
        remap_references(obj, mapping)

    if parent_to:
        copy_set = set(copies)
        for dup in copies:
            if dup.parent in copy_set or dup.parent in objects:
                continue
            target = parent_to(dup)
            if target is not None:
                world = dup.matrix_world.copy()
                dup.parent = target
                dup.matrix_parent_inverse = target.matrix_world.inverted()
                dup.matrix_world = world
    return copies
