# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""What the rigging tools have built, found again from the scene: listing mechanisms,
removing them and rebuilding chains. Also the versions of the tools that use no bones
(hinges, pistons and a drive control made from objects and empties), and the motion a
Test plays on a part.
"""

import math
import re
from collections import namedtuple

import bpy
from mathutils import Matrix, Vector

from . import rigging
from .rigging import AXES

HINGE_LIMIT = "Hinge Limit"
PISTON_TRACK = "Piston Track"
PISTON_PROP = "preflight_piston"   # on a piston's helper empties: "aim" or "mount"
HINGE_PROP = "preflight_hinge"     # on the empty a parentless hinge part hangs from
DRIVE_PROP = "preflight_drive"     # on a drive control empty

# kind, text shown, names of objects to select, key that finds it again for Remove
Mechanism = namedtuple("Mechanism", "kind label detail objects key")

KIND_ICONS = {
    'GEAR': 'PREFERENCES', 'FOLLOW': 'LINKED', 'CHAIN': 'CURVE_NCURVE',
    'WHEELS': 'DRIVER_ROTATIONAL_DIFFERENCE', 'PISTON': 'CON_STRETCHTO',
    'IK': 'CON_KINEMATIC', 'HINGE': 'CON_ROTLIMIT', 'DRIVE': 'ORIENTATION_GIMBAL',
}
_BONE_PATH = re.compile(r'^pose\.bones\["(.+?)"\]\.')
_RATIO = re.compile(r"\*\s*(-?[\d.eE+-]+)\s*$")


# --- Finding ---

def _bone_of(path):
    match = _BONE_PATH.match(path)
    return match.group(1) if match else ""


def _part_name(owner, path):
    """What the user calls the thing a data path on `owner` moves: the part on that
    bone, else the bone, else the object."""
    bone = _bone_of(path)
    if bone and owner.type == 'ARMATURE':
        part = rigging._bone_part(owner, bone)
        return part.name if part is not None else bone
    return owner.name


def _select_names(owner, path):
    bone = _bone_of(path)
    if bone and owner.type == 'ARMATURE':
        part = rigging._bone_part(owner, bone)
        return (part.name,) if part is not None else (owner.name,)
    return (owner.name,)


def _drivers(obj):
    anim = obj.animation_data
    return list(anim.drivers) if anim is not None else []


def _var(fcurve, name):
    var = fcurve.driver.variables.get(name)
    return var.targets[0] if var is not None and len(fcurve.driver.variables) == 1 else None


def _chain_curve(owner, prop):
    """The path curve whose slide property on `owner` is `prop`."""
    for curve in bpy.data.objects:
        if curve.type == 'CURVE' and rigging.chain_prop(curve) == prop:
            return curve
    return None


def _chain_objects(owner, curve):
    """The chain objects (mesh, or holder and its links) on `owner` that ride `curve`."""
    found = []
    for obj in bpy.data.objects:
        if rigging.chain_rig(obj) is not owner:
            continue
        on = rigging.chain_of(obj)
        if on is not None and on[1] is curve:
            found.append(obj)
    return found


def _ratio(expression):
    match = _RATIO.search(expression)
    try:
        return float(match.group(1)) if match else None
    except ValueError:
        return None


def find(scene):
    """Every mechanism the rigging tools built in `scene`, as Mechanism rows."""
    rows = []
    objects = list(scene.objects)
    bodies = {}
    for obj in objects:
        for fc in _drivers(obj):
            if not fc.data_path.endswith("rotation_euler"):
                continue
            spin, slide = _var(fc, "spin"), _var(fc, "slide")
            key = f"{obj.name}|{fc.data_path}|{fc.array_index}"
            driven = _part_name(obj, fc.data_path)
            if spin is not None and spin.id is not None:
                ratio = _ratio(fc.driver.expression)
                detail = f"x{abs(ratio):.3g}{' reversed' if ratio < 0 else ''}" if ratio else ""
                rows.append(Mechanism(
                    'GEAR', f"{_part_name(spin.id, spin.data_path)} > {driven}", detail,
                    _select_names(obj, fc.data_path) + _select_names(spin.id, spin.data_path),
                    "GEAR|" + key))
            elif slide is not None and slide.id is not None:
                prop = slide.data_path[2:-2]
                curve = _chain_curve(slide.id, prop)
                rows.append(Mechanism(
                    'FOLLOW', f"{curve.name if curve else 'chain'} > {driven}",
                    "turns with the chain", _select_names(obj, fc.data_path), "FOLLOW|" + key))

        # Chains: one slide property per path, on the rig or the chain's own empty.
        for prop in [k for k in obj.keys() if k.startswith(rigging.CHAIN_PROP + "_")]:
            curve = _chain_curve(obj, prop)
            if curve is None:
                continue
            parts = _chain_objects(obj, curve)
            holders = {rigging.chain_holder(o) or o for o in parts}
            links = sum(len(rigging.chain_links(h)) if rigging.chain_holder(h) is h
                        else sum(1 for g in h.vertex_groups if rigging.LINK_BONE.search(g.name))
                        for h in holders)
            source = rigging._chain_source(obj, curve)
            by = f"driven by {_part_name(*source)}" if source else "nothing drives it"
            rows.append(Mechanism('CHAIN', f"Chain on {curve.name}", f"{links} links, {by}",
                                  tuple(h.name for h in holders) or (curve.name,),
                                  f"CHAIN|{obj.name}|{curve.name}"))

        # Wheel Spin: wheels grouped by the body they roll with.
        rolls = obj.get(rigging.WHEEL_PROP)
        if rolls:
            for key in rolls.keys():
                path, index = key.rsplit("|", 1)
                fc = obj.animation_data.drivers.find(path, index=int(index)) \
                    if obj.animation_data else None
                if fc is None or "x" not in fc.driver.variables:
                    continue
                target = fc.driver.variables["x"].targets[0]
                if target.id is None:
                    continue
                body = (target.id.name, target.bone_target)
                bodies.setdefault(body, []).append(_select_names(obj, path)[0])

        if obj.type == 'ARMATURE':
            rows += _armature_rows(obj)
        else:
            rows += _object_rows(obj)
    for (owner, bone), wheels in bodies.items():
        body_obj = bpy.data.objects.get(owner)
        name = owner
        if bone and body_obj is not None:
            part = rigging._bone_part(body_obj, bone)
            name = part.name if part is not None else bone
        rows.append(Mechanism('WHEELS', f"{name} rolls {len(wheels)} wheel(s)",
                              ", ".join(sorted(wheels)[:3]), tuple(wheels),
                              f"WHEELS|{owner}|{bone}"))
    order = ['DRIVE', 'WHEELS', 'CHAIN', 'FOLLOW', 'GEAR', 'PISTON', 'IK', 'HINGE']
    rows.sort(key=lambda r: (order.index(r.kind), r.label))
    return rows


def _degrees(con, index):
    key = "xyz"[index]
    return (math.degrees(getattr(con, f"min_{key}")), math.degrees(getattr(con, f"max_{key}")))


def _limit_axis(con):
    """Index of the axis a Hinge Limit constraint leaves free (the widest range)."""
    return max(range(3), key=lambda i: getattr(con, f"max_{'xyz'[i]}")
               - getattr(con, f"min_{'xyz'[i]}"))


def _armature_rows(rig):
    rows = []
    pistons = set()
    for pb in rig.pose.bones:
        for con in pb.constraints:
            if con.name == HINGE_LIMIT and con.type == 'LIMIT_ROTATION':
                axis = _limit_axis(con)
                low, high = _degrees(con, axis)
                part = rigging._bone_part(rig, pb.name)
                rows.append(Mechanism('HINGE', f"Hinge: {part.name if part else pb.name}",
                                      f"{'XYZ'[axis]} {low:.0f} to {high:.0f} deg",
                                      (part.name if part else rig.name,),
                                      f"HINGE|{rig.name}|{pb.name}"))
            elif con.name == PISTON_TRACK and con.type == 'DAMPED_TRACK':
                other = con.subtarget[:-len(rigging.ANCHOR_SUFFIX)] \
                    if con.subtarget.endswith(rigging.ANCHOR_SUFFIX) else con.subtarget
                pair = tuple(sorted((pb.name, other)))
                if pair in pistons:
                    continue
                pistons.add(pair)
                parts = [rigging._bone_part(rig, n) for n in pair]
                names = tuple(p.name if p else n for p, n in zip(parts, pair))
                rows.append(Mechanism('PISTON', f"Piston: {names[0]} + {names[1]}", "",
                                      tuple(p.name for p in parts if p) or (rig.name,),
                                      f"PISTON|{rig.name}|{pair[0]}|{pair[1]}"))
            elif con.type == 'IK' and con.subtarget.startswith(rigging.IK_PREFIX):
                rows.append(Mechanism('IK', f"IK: {pb.name}", f"{con.chain_count} bone(s)",
                                      (rig.name,), f"IK|{rig.name}|{pb.name}"))
    if rigging.DRIVE_BONE in rig.data.bones:
        rows.append(Mechanism('DRIVE', f"Drive control on {rig.name}", "the 'drive' bone",
                              (rig.name,), f"DRIVE|{rig.name}|"))
    return rows


def _object_rows(obj):
    rows = []
    for con in obj.constraints:
        if con.name == HINGE_LIMIT and con.type == 'LIMIT_ROTATION':
            axis = _limit_axis(con)
            low, high = _degrees(con, axis)
            rows.append(Mechanism('HINGE', f"Hinge: {obj.name}",
                                  f"{'XYZ'[axis]} {low:.0f} to {high:.0f} deg", (obj.name,),
                                  f"HINGE|{obj.name}|"))
    if obj.get(PISTON_PROP) == "aim" and obj.get("preflight_piston_rod"):
        parts = [c.name for c in obj.children if not c.get(PISTON_PROP)]
        other = obj.get("preflight_piston_other")
        if isinstance(other, bpy.types.Object):
            parts += [c.name for c in other.children if not c.get(PISTON_PROP)]
        rows.append(Mechanism('PISTON', "Piston: " + " + ".join(parts), "no bones",
                              tuple(parts), f"PISTON|{obj.name}||"))
    if obj.get(DRIVE_PROP):
        rows.append(Mechanism('DRIVE', f"Drive control: {obj.name}", "an empty: move and turn it",
                              (obj.name,), f"DRIVE|{obj.name}|"))
    return rows


# --- Removing ---

def _remove_driver(owner, path, index):
    anim = owner.animation_data
    fc = anim.drivers.find(path, index=index) if anim is not None else None
    if fc is None:
        return False
    anim.drivers.remove(fc)
    try:
        owner.path_resolve(path)[index] = 0.0
    except (ValueError, TypeError, IndexError):
        pass
    return True


def _forget_wheel(owner, path, index):
    rolls = dict(owner.get(rigging.WHEEL_PROP, {}))
    if rolls.pop(f"{path}|{index}", None) is not None:
        if rolls:
            owner[rigging.WHEEL_PROP] = rolls
        else:
            del owner[rigging.WHEEL_PROP]


def remove_chain(context, owner, curve, keep_slide=False):
    """Remove the chain on `curve` whose slide `owner` holds: its mesh or instances, its
    link bones, and (unless `keep_slide`) the slide property, what drives it and the
    wheels' links to it. The path and the link it was built from stay; the link is
    shown again. Returns the source link (or None when the chain didn't record one)."""
    prop = rigging.chain_prop(curve)
    link = None
    parts = _chain_objects(owner, curve)
    holders = {rigging.chain_holder(o) or o for o in parts}
    bones = set()
    armature = owner if owner.type == 'ARMATURE' else None
    if not keep_slide:  # while the slide's owner is still there to compare against
        for obj in bpy.data.objects:
            for fc in _drivers(obj):
                slide = _var(fc, "slide")
                if slide is not None and slide.id is owner and slide.data_path == f'["{prop}"]':
                    _remove_driver(obj, fc.data_path, fc.array_index)
    for holder in holders:
        source = holder.get(rigging.CHAIN_LINK)
        if isinstance(source, bpy.types.Object):
            link = source
        if rigging.chain_holder(holder) is holder:
            for inst in rigging.chain_links(holder):
                if inst.parent_bone:
                    bones.add(inst.parent_bone)
                bpy.data.objects.remove(inst)
            if armature is None and keep_slide:
                continue  # the empty holds the slide: Rebuild puts new links under it
        else:
            bones.update(g.name for g in holder.vertex_groups
                         if rigging.LINK_BONE.search(g.name))
            mesh = holder.data
            bpy.data.objects.remove(holder)
            if mesh is not None and mesh.users == 0:
                bpy.data.meshes.remove(mesh)
            continue
        bpy.data.objects.remove(holder)
    if armature is not None:
        # Any link bone still following this path goes too (a chain whose mesh was deleted).
        for pb in owner.pose.bones:
            if rigging.LINK_BONE.search(pb.name) and any(
                    c.type == 'FOLLOW_PATH' and c.target is curve for c in pb.constraints):
                bones.add(pb.name)
        if bones:
            anim = owner.animation_data
            for fc in list(anim.drivers) if anim else []:
                if _bone_of(fc.data_path) in bones:
                    anim.drivers.remove(fc)
            with rigging.edit_armature(context, owner) as edit_bones:
                for name in bones:
                    bone = edit_bones.get(name)
                    if bone is not None:
                        edit_bones.remove(bone)
    if not keep_slide and armature is not None:
        try:
            owner.driver_remove(f'["{prop}"]')
        except TypeError:
            pass
        if prop in owner:
            del owner[prop]
    if link is not None:
        link.hide_set(False)
        link.hide_render = False
    return link


def rebuild_chain(context, chain, count=0, instances=None, keep_link=True):
    """Build `chain` again on its path with `count` links (0: as many as fit), keeping
    its roll, what drives it and the wheels that turn with it. `instances` None keeps
    the kind it was. Returns (new chain, link count)."""
    owner = rigging.chain_rig(chain)
    found = rigging.chain_of(chain)
    if owner is None or found is None:
        raise ValueError(f"'{chain.name}' isn't a chain made by Chain Along Curve")
    curve = found[1]
    holder = rigging.chain_holder(chain) or chain
    link = holder.get(rigging.CHAIN_LINK)
    if not isinstance(link, bpy.types.Object):
        raise ValueError(f"'{holder.name}' doesn't know which link it was built from "
                         "(made before 3.5): remove it and build it again")
    axis = holder.get(rigging.CHAIN_AXIS, 'X')
    roll = math.radians(holder.get(rigging.CHAIN_ROLL, 0.0))
    bones = owner.type == 'ARMATURE'
    if instances is None:
        instances = rigging.chain_holder(holder) is holder
    remove_chain(context, owner, curve, keep_slide=True)
    if not bones:
        # The empty holds the slide and what drives it: new links go under the same one.
        made = count or rigging.fit_links(link, curve, axis)[0]
        rigging._chain_without_bones(link, curve, axis, made, None, roll, keep_link,
                                     holder=holder)
        return holder, made
    _rig, new, made = rigging.chain_along_curve(
        context, link, curve, owner, axis=axis, count=count, roll=roll,
        instances=instances, keep_link=keep_link)
    return new, made


def remove(context, key):
    """Remove the mechanism a Mechanism row's key names. Returns a short description."""
    kind, _, rest = key.partition("|")
    fields = rest.split("|")
    owner = bpy.data.objects.get(fields[0])
    if owner is None:
        raise ValueError("That mechanism is gone already")
    if kind in {'GEAR', 'FOLLOW'}:
        path, index = fields[1], int(fields[2])
        if not _remove_driver(owner, path, index):
            raise ValueError("That link is gone already")
        return f"{_part_name(owner, path)} is free again"
    if kind == 'WHEELS':
        body, bone = fields[0], fields[1]
        count = 0
        for holder in bpy.data.objects:
            for wheel_key in list(dict(holder.get(rigging.WHEEL_PROP, {}))):
                path, index = wheel_key.rsplit("|", 1)
                fc = holder.animation_data.drivers.find(path, index=int(index)) \
                    if holder.animation_data else None
                if fc is None or "x" not in fc.driver.variables:
                    continue
                target = fc.driver.variables["x"].targets[0]
                if target.id is not None and target.id.name == body \
                        and target.bone_target == bone:
                    _remove_driver(holder, path, int(index))
                    _forget_wheel(holder, path, int(index))
                    count += 1
        return f"{count} wheel(s) stopped rolling"
    if kind == 'CHAIN':
        curve = bpy.data.objects.get(fields[1])
        if curve is None:
            raise ValueError("That chain's path is gone")
        remove_chain(context, owner, curve)
        return f"Chain on '{curve.name}' removed (the path and the link are kept)"
    if kind == 'HINGE':
        return _remove_hinge(owner, fields[1])
    if kind == 'IK':
        return _remove_ik(context, owner, fields[1])
    if kind == 'PISTON':
        if owner.type == 'ARMATURE':
            for name in fields[1:3]:
                pb = owner.pose.bones.get(name)
                for con in [c for c in (pb.constraints if pb else ()) if c.name == PISTON_TRACK]:
                    pb.constraints.remove(con)
            return "Piston stopped tracking (its bones are kept)"
        return _remove_object_piston(owner)
    if kind == 'DRIVE':
        if owner.type == 'ARMATURE':
            return _remove_drive_bone(context, owner)
        return _dissolve_empty(owner, "Drive control removed")
    raise ValueError(f"Unknown mechanism '{kind}'")


def _remove_hinge(owner, bone):
    struct = owner.pose.bones.get(bone) if bone else owner
    if struct is None:
        raise ValueError("That hinge is gone already")
    for con in [c for c in struct.constraints if c.name == HINGE_LIMIT]:
        struct.constraints.remove(con)
    struct.lock_rotation = (False, False, False)
    struct.lock_location = (False, False, False)
    return f"'{bone or owner.name}' turns freely again"


def _remove_ik(context, rig, tip):
    pb = rig.pose.bones.get(tip)
    helpers = set()
    for con in [c for c in (pb.constraints if pb else ()) if c.type == 'IK']:
        helpers.update(n for n in (con.subtarget, con.pole_subtarget) if n)
        pb.constraints.remove(con)
    helpers &= {rigging.IK_PREFIX + tip, rigging.POLE_PREFIX + tip}
    if helpers:
        with rigging.edit_armature(context, rig) as edit_bones:
            for name in helpers:
                bone = edit_bones.get(name)
                if bone is not None:
                    edit_bones.remove(bone)
    return f"IK removed from '{tip}'"


def _remove_drive_bone(context, rig):
    with rigging.edit_armature(context, rig) as edit_bones:
        drive = edit_bones.get(rigging.DRIVE_BONE)
        if drive is None:
            raise ValueError("That drive control is gone already")
        for child in list(drive.children):
            child.parent = drive.parent
        edit_bones.remove(drive)
    return "Drive control removed"


def _dissolve_empty(empty, message):
    """Delete a helper empty, handing its children to its parent where they are."""
    for child in list(empty.children):
        world = child.matrix_world.copy()
        child.parent = empty.parent
        if empty.parent is not None:
            child.matrix_parent_inverse = empty.parent.matrix_world.inverted()
        child.matrix_world = world
    bpy.data.objects.remove(empty)
    return message


def _remove_object_piston(aim):
    others = [aim.get("preflight_piston_other"), aim.get("preflight_piston_mount")]
    _dissolve_empty(aim, "")
    for helper in others:
        if isinstance(helper, bpy.types.Object) and helper.name in bpy.data.objects:
            _dissolve_empty(helper, "")
    return "Piston removed"


# --- Without bones ---

def _keep_world_parent(obj, parent):
    bpy.context.view_layer.update()
    world = obj.matrix_world.copy()
    obj.parent = parent
    obj.parent_type = 'OBJECT'
    bpy.context.view_layer.update()
    obj.matrix_parent_inverse = parent.matrix_world.inverted() if parent else Matrix.Identity(4)
    obj.matrix_world = world


def _empty(name, world, like, size, display='PLAIN_AXES'):
    empty = bpy.data.objects.new(name, None)
    empty.empty_display_type = display
    empty.empty_display_size = size
    for coll in like.users_collection:
        coll.objects.link(empty)
    empty.matrix_world = world
    return empty


def hinge_frame(obj):
    """Make `obj`'s rotation channels start at zero with its axes where they are now, so
    one channel is one hinge and a Limit Rotation constraint limits just that. The
    resting rotation moves into the parent link; a part with no parent is hung from a
    new empty at its origin. Returns True if anything changed."""
    if rigging.bone_target(obj)[0] is not None:
        return False
    if obj.rotation_mode in {'QUATERNION', 'AXIS_ANGLE'}:
        obj.rotation_mode = 'XYZ'
    if all(abs(v) < 1e-7 for v in obj.rotation_euler) and \
            all(abs(v) < 1e-7 for v in obj.delta_rotation_euler):
        return False
    bpy.context.view_layer.update()
    world = obj.matrix_world.copy()
    if obj.parent is None:
        loc, rot, _scale = world.decompose()
        mount = _empty(f"{obj.name}_hinge", Matrix.Translation(loc) @ rot.to_matrix().to_4x4(),
                       obj, max(max(obj.dimensions), 0.05) * 0.5, 'ARROWS')
        mount[HINGE_PROP] = True
        obj.parent = mount
    obj.rotation_euler = (0.0, 0.0, 0.0)
    obj.delta_rotation_euler = (0.0, 0.0, 0.0)
    # With no parent link the world matrix is parent x own: read the part's own matrix
    # from that (deltas and all), then set the link that puts it back where it was.
    obj.matrix_parent_inverse = Matrix.Identity(4)
    bpy.context.view_layer.update()
    parent_world = obj.parent.matrix_world.copy()
    own = parent_world.inverted() @ obj.matrix_world
    obj.matrix_parent_inverse = parent_world.inverted() @ world @ own.inverted()
    bpy.context.view_layer.update()
    return True


def hinge_limits_object(obj, axis='Z', minimum=-1.0, maximum=1.0, lock_location=True):
    """Make an unrigged part a hinge: it turns about its own local `axis` within the
    limits, with the other axes (and optionally its location) locked."""
    index = AXES[axis]
    hinge_frame(obj)
    obj.rotation_mode = 'XYZ'
    obj.lock_rotation = [i != index for i in range(3)]
    if lock_location:
        obj.lock_location = (True, True, True)
    for con in [c for c in obj.constraints if c.name == HINGE_LIMIT]:
        obj.constraints.remove(con)
    con = obj.constraints.new('LIMIT_ROTATION')
    con.name = HINGE_LIMIT
    con.owner_space = 'LOCAL'
    for i, key in enumerate("xyz"):
        setattr(con, f"use_limit_{key}", True)
        setattr(con, f"min_{key}", minimum if i == index else 0.0)
        setattr(con, f"max_{key}", maximum if i == index else 0.0)
    return con


def _aim_matrix(origin, toward):
    """World matrix at `origin` whose +Y points at `toward`."""
    y = (toward - origin).normalized()
    helper = Vector((0.0, 0.0, 1.0)) if abs(y.z) < 0.95 else Vector((1.0, 0.0, 0.0))
    x = y.cross(helper).normalized()
    z = x.cross(y).normalized()
    mat = Matrix((x, y, z)).transposed().to_4x4()
    mat.translation = origin
    return mat


def piston_objects(context, cylinder, rod):
    """A piston with no bones: each part hangs from an empty on its pivot that points at
    the other. The rod's side also gets a plain mount empty for the cylinder to aim at,
    so neither aiming empty depends on the other's rotation and nothing cycles.
    Returns (cylinder aim, rod aim)."""
    context.view_layer.update()
    for part in (cylinder, rod):  # a re-run starts from the parts' real mounts
        parent = part.parent
        if parent is not None and parent.get(PISTON_PROP) == "aim":
            _remove_object_piston(parent if parent.get("preflight_piston_rod")
                                  else parent.get("preflight_piston_other") or parent)
    context.view_layer.update()
    a, b = cylinder.matrix_world.translation.copy(), rod.matrix_world.translation.copy()
    if (a - b).length < 1e-5:
        raise ValueError("The cylinder and rod pivots are in the same place")
    size = (a - b).length * 0.15
    mount = _empty(f"{rod.name}_mount", Matrix.Translation(b), rod, size * 0.6, 'CUBE')
    cyl_aim = _empty(f"{cylinder.name}_aim", _aim_matrix(a, b), cylinder, size)
    rod_aim = _empty(f"{rod.name}_aim", _aim_matrix(b, a), rod, size)
    mount[PISTON_PROP] = "mount"
    cyl_aim[PISTON_PROP] = rod_aim[PISTON_PROP] = "aim"
    rod_aim["preflight_piston_rod"] = True
    rod_aim["preflight_piston_other"] = cyl_aim
    rod_aim["preflight_piston_mount"] = mount
    cyl_aim["preflight_piston_other"] = rod_aim
    for helper, part in ((mount, rod), (rod_aim, rod), (cyl_aim, cylinder)):
        if part.parent is not None:
            _keep_world_parent(helper, part.parent)
    for aim, target, part in ((cyl_aim, mount, cylinder), (rod_aim, cyl_aim, rod)):
        con = aim.constraints.new('DAMPED_TRACK')
        con.name = PISTON_TRACK
        con.target = target
        con.track_axis = 'TRACK_Y'
        _keep_world_parent(part, aim)
    return cyl_aim, rod_aim


def drive_empty(context, body, forward):
    """A drive control with no bones: an arrow empty on the ground under `body`, pointing
    `forward`, that the body hangs from. Move and turn it to drive. Returns the empty."""
    context.view_layer.update()
    parent = body.parent
    if parent is not None and parent.get(DRIVE_PROP):
        return parent
    corners = [body.matrix_world @ Vector(c) for c in body.bound_box]
    ground = min(c.z for c in corners)
    for child in body.children_recursive:
        if child.type == 'MESH':
            ground = min(ground, min((child.matrix_world @ Vector(c)).z for c in child.bound_box))
    flat = Vector((forward.x, forward.y, 0.0))
    flat = flat.normalized() if flat.length > 1e-6 else Vector((0.0, 1.0, 0.0))
    centre = body.matrix_world.translation
    world = _aim_matrix(Vector((centre.x, centre.y, ground)),
                        Vector((centre.x, centre.y, ground)) + flat)
    empty = _empty(f"{body.name}_drive", world, body, max(max(body.dimensions), 0.2),
                   'ARROWS')  # its Y arrow points the way the vehicle drives
    empty[DRIVE_PROP] = True
    if parent is not None:
        _keep_world_parent(empty, parent)
    _keep_world_parent(body, empty)
    return empty


# --- Test motion ---

Motion = namedtuple("Motion", "struct path index start kind low high label")


def _struct_for(context):
    """(owner id, struct, name): the active pose bone, else the active part's bone, else
    the active object."""
    obj = context.active_object
    if obj is None:
        return None, None, ""
    if context.mode == 'POSE' and context.active_pose_bone is not None:
        return obj, context.active_pose_bone, context.active_pose_bone.name
    rig, bone = rigging.bone_target(obj)
    if rig is not None:
        return rig, rig.pose.bones[bone], obj.name
    return obj, obj, obj.name


def _driver_on(owner, struct, index):
    anim = owner.animation_data
    if anim is None:
        return None
    return anim.drivers.find(struct.path_from_id("rotation_euler"), index=index)


def _resolve_struct(owner, path):
    bone = _bone_of(path)
    if bone and owner.type == 'ARMATURE':
        return owner.pose.bones.get(bone)
    return owner


def motion_for(context, travel_axis='Y', spin_axis='AUTO'):
    """The motion Test plays on the active part: a hinge swings through its limits, a
    body with rolling wheels (or a drive control) travels there and back, anything else
    spins once about its axle. A part that is driven hands over to what drives it.
    Returns a Motion, or raises ValueError."""
    owner, struct, name = _struct_for(context)
    if struct is None:
        raise ValueError("Make the part to test the active object")
    for con in struct.constraints:
        if con.name == HINGE_LIMIT and con.type == 'LIMIT_ROTATION':
            index = _limit_axis(con)
            key = "xyz"[index]
            if struct.rotation_mode in {'QUATERNION', 'AXIS_ANGLE'}:
                struct.rotation_mode = 'XYZ'
            return Motion(struct, "rotation_euler", index, struct.rotation_euler[index],
                          'SWING', getattr(con, f"min_{key}"), getattr(con, f"max_{key}"),
                          f"{name} swings through its limits")
    obj = context.active_object
    is_drive = (struct is not obj and struct.name == rigging.DRIVE_BONE) or \
        bool(obj.get(DRIVE_PROP))
    body_of = {(w[5][0], w[5][1]) for w in rigging.wheel_drivers()}
    world_owner = (owner, struct.name if struct is not owner else "")
    if is_drive or world_owner in body_of:
        if struct is owner:
            direction = rigging.object_axis(obj, 'Y' if obj.get(DRIVE_PROP) else travel_axis)
            parent = obj.parent.matrix_world.to_3x3() if obj.parent else Matrix.Identity(3)
            local = parent.inverted() @ direction
            index = max(range(3), key=lambda i: abs(local[i]))
            sign = 1.0 if local[index] > 0 else -1.0
            size = obj.empty_display_size if obj.type == 'EMPTY' else max(obj.dimensions)
            reach = max(size, 0.5) * 2.0
        else:
            index, sign = 1, 1.0  # a bone moves along its own length
            reach = max(struct.bone.length, 0.25) * 4.0
        start = struct.location[index]
        return Motion(struct, "location", index, start, 'TRAVEL', start,
                      start + sign * reach, f"{name} drives forward and back")
    # Spin: follow a driven part back to whatever turns it.
    index = None
    if struct is owner:
        axle = rigging.wheel_axle(obj, spin_axis)
        index = AXES[axle[-1]]
    else:
        part = rigging._bone_part(owner, struct.name)
        if part is not None:
            index = rigging.channel_axis(part, rigging.wheel_axle(part, spin_axis))[0]
        else:
            index = 1
    for _hop in range(8):
        if struct.rotation_mode in {'QUATERNION', 'AXIS_ANGLE'}:
            struct.rotation_mode = 'XYZ'
        fc = _driver_on(owner, struct, index)
        if fc is None:
            break
        spin, slide = _var(fc, "spin"), _var(fc, "slide")
        if slide is not None and slide.id is not None:
            prop = slide.data_path[2:-2]
            curve = _chain_curve(slide.id, prop)
            source = rigging._chain_source(slide.id, curve) if curve else None
            if source is None:
                return Motion(slide.id, f'["{prop}"]', -1, slide.id[prop], 'SLIDE',
                              0.0, 1.0, f"the chain on {curve.name if curve else 'its path'} "
                                        "goes once round")
            owner = source[0]
            struct = _resolve_struct(owner, source[1])
            index = int(source[1].rsplit("[", 1)[1].rstrip("]"))
        elif spin is not None and spin.id is not None:
            owner = spin.id
            struct = _resolve_struct(owner, spin.data_path)
            index = int(spin.data_path.rsplit("[", 1)[1].rstrip("]"))
        else:
            raise ValueError(f"'{name}' is driven by something Test can't follow")
        if struct is None:
            raise ValueError(f"What drives '{name}' is gone")
        name = _part_name(owner, struct.path_from_id("rotation_euler")) \
            if struct is not owner else owner.name
    start = struct.rotation_euler[index]
    return Motion(struct, "rotation_euler", index, start, 'SPIN', start, start + math.tau,
                  f"{name} turns once")


def apply_motion(motion, t):
    """Pose `motion` at `t` (0-1 through the test). It is back at its start at 1."""
    t = min(max(t, 0.0), 1.0)
    if motion.kind == 'SWING':
        # start -> high -> low -> start
        if t < 0.25:
            value = motion.start + (motion.high - motion.start) * (t / 0.25)
        elif t < 0.75:
            value = motion.high + (motion.low - motion.high) * ((t - 0.25) / 0.5)
        else:
            value = motion.low + (motion.start - motion.low) * ((t - 0.75) / 0.25)
    elif motion.kind == 'TRAVEL':
        value = motion.low + (motion.high - motion.low) * math.sin(math.pi * t)
    else:  # SPIN and SLIDE go once round and stop where they began (t = 1 is the start)
        value = motion.low + (motion.high - motion.low) * (t if t < 1.0 else 0.0)
    if motion.index < 0:
        motion.struct[motion.path[2:-2]] = value
        motion.struct.update_tag()
    else:
        getattr(motion.struct, motion.path)[motion.index] = value


def end_motion(motion):
    """Put the part back where the test found it."""
    if motion.index < 0:
        motion.struct[motion.path[2:-2]] = motion.start
        motion.struct.update_tag()
    else:
        getattr(motion.struct, motion.path)[motion.index] = motion.start
