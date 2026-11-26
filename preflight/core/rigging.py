# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jordan Moss

"""Hard surface rigging: mirrored parts, bones on origins, pistons, gears, chains along
curves, and turning a rig into one a game engine accepts.

Parts are rigid, so every binding here is rigid too: a part is parented to one bone, or
skinned with a single vertex group at full weight.
"""

import math
import re
from contextlib import contextmanager

import bmesh
import bpy
from mathutils import Euler, Matrix, Vector

AXES = {'X': 0, 'Y': 1, 'Z': 2}
AXIS_ITEMS = [('X', "X", "Local X"), ('Y', "Y", "Local Y"), ('Z', "Z", "Local Z"),
              ('-X', "-X", "Local -X"), ('-Y', "-Y", "Local -Y"), ('-Z', "-Z", "Local -Z")]
BIND_ITEMS = [
    ('PARENT', "Parent to Bone", "Parent each part to its bone (quick to set up in Blender)"),
    ('WEIGHTS', "Rigid Weights", "Give each part a vertex group at full weight and an "
                                 "Armature modifier (what game engines import)"),
    ('HIERARCHY', "Keep Hierarchy", "Keep the parts' own parenting (Outliner stays nested): "
                                    "each part follows its bone with constraints"),
    ('NONE', "None", "Only make the bones"),
]
FOLLOW_SUFFIX = "_follow"  # helper bone a Keep Hierarchy part copies
FOLLOW_CONSTRAINTS = ("Follow Bone Location", "Follow Bone Rotation")
ENGINE_UNSAFE_NAMES = {"armature", "root"}  # rig object names Unreal imports badly
ROOT_BONE = "root"
CHAIN_PROP = "chain_offset"
CHAIN_ROLL = "preflight_link_roll"  # on a chain mesh: degrees its links are rolled
CHAIN_TAG = "preflight_chain"  # on an instanced chain's empty and links: the empty
CHAIN_LINK = "preflight_chain_link"  # on a chain: the link object it was built from
CHAIN_AXIS = "preflight_chain_axis"  # on a chain: that link's length axis

_SIDE_RE = re.compile(r"(?<![A-Za-z0-9])(L|R|l|r|Left|Right|left|right|LEFT|RIGHT)"
                      r"(?=$|[._\- ]|\d)")
_SIDE_SWAP = {"L": "R", "R": "L", "l": "r", "r": "l", "Left": "Right", "Right": "Left",
              "left": "right", "right": "left", "LEFT": "RIGHT", "RIGHT": "LEFT"}
_PAIR_TAIL_RE = re.compile(r"(_(?i:low|lo|lp|high|hi|hp)(?:_\d+)?)$")


# --- Names ---

def flip_side(name):
    """`name` with its side tokens swapped (L <-> R, Left <-> Right). None if it has none."""
    flipped, count = _SIDE_RE.subn(lambda m: _SIDE_SWAP[m.group(1)], name)
    return flipped if count else None


def with_side(name, side):
    """`name` with a side token added, kept before any _low/_high suffix."""
    tail = _PAIR_TAIL_RE.search(name)
    if tail:
        return f"{name[:tail.start()]}_{side}{tail.group(1)}"
    return f"{name}_{side}"


def side_of(position, axis_index, pivot):
    """'L' or 'R' for a point on the +/- side of the mirror. Across X, +X is left (a
    character facing -Y); across Y, +Y is left (a vehicle facing +X). Z has no sides."""
    if axis_index == 2:
        return None
    return 'L' if position[axis_index] >= pivot[axis_index] else 'R'


def unique_name(name, taken):
    if name not in taken:
        return name
    i = 1
    while f"{name}.{i:03d}" in taken:
        i += 1
    return f"{name}.{i:03d}"


# --- Mirroring ---

def _flip(axis_index):
    flip = Matrix.Identity(4)
    flip[axis_index][axis_index] = -1.0
    return flip


def mirror_matrix(axis_index, pivot):
    """World-space mirror across the plane through `pivot` facing `axis_index`."""
    pivot = Vector(pivot)
    return Matrix.Translation(pivot) @ _flip(axis_index) @ Matrix.Translation(-pivot)


def _depth(obj):
    depth = 0
    while obj.parent is not None:
        obj, depth = obj.parent, depth + 1
    return depth


def _mirror_mesh(mesh, axis_index):
    """Flip `mesh` across its local `axis_index`, keeping faces pointing outwards and
    any custom normals."""
    custom = None
    if mesh.has_custom_normals:
        custom = [Vector(c.vector) for c in mesh.corner_normals]
        for normal in custom:
            normal[axis_index] = -normal[axis_index]
    for vert in mesh.vertices:
        vert.co[axis_index] = -vert.co[axis_index]
    mesh.flip_normals()
    if custom is not None:
        # flip_normals keeps each face's first corner and reverses the rest.
        reordered = list(custom)
        for poly in mesh.polygons:
            start, total = poly.loop_start, poly.loop_total
            for j in range(1, total):
                reordered[start + j] = custom[start + total - j]
        mesh.normals_split_custom_set(reordered)
    mesh.update()


def mirror_objects(objects, axis='X', pivot=(0.0, 0.0, 0.0), name_sides=True):
    """Duplicate `objects` mirrored across `axis` through `pivot`.

    Each copy's origin and orientation are mirrored the way a symmetrised bone is: scale
    stays positive and the mesh itself is flipped, so the copy's pivot works for its own
    bone. Parents inside the selection are rebuilt between the copies, and bone parents
    move to the bone of the other side when the armature has one. Returns
    [(source, copy)], plus the names of objects only copied with a negative scale (not
    meshes or empties).
    """
    axis_index = AXES[axis]
    world = mirror_matrix(axis_index, pivot)
    local = _flip(axis_index)
    pairs, mapping, negative = [], {}, []
    taken = set(bpy.data.objects.keys())
    for src in sorted(objects, key=_depth):
        copy = src.copy()
        if src.data is not None:
            copy.data = src.data.copy()
        if copy.animation_data is not None:
            copy.animation_data_clear()  # an action made for one side is wrong on the other
        for coll in src.users_collection:
            coll.objects.link(copy)

        if src.parent in mapping:
            copy.parent = mapping[src.parent]
        elif src.parent_type == 'BONE' and src.parent is not None:
            other = flip_side(src.parent_bone)
            if other and other in src.parent.data.bones:
                copy.parent_bone = other

        if src.type == 'MESH':
            _mirror_mesh(copy.data, axis_index)
            copy.matrix_world = world @ src.matrix_world @ local
        elif src.type == 'EMPTY' or src.data is None:
            copy.matrix_world = world @ src.matrix_world @ local
        else:
            copy.matrix_world = world @ src.matrix_world  # negative scale, still correct
            negative.append(copy.name)

        name = flip_side(src.name)
        if name is None and name_sides:
            side = side_of(copy.matrix_world.translation, axis_index, pivot)
            if side is not None:
                name = with_side(src.name, side)
                own = with_side(src.name, 'R' if side == 'L' else 'L')
                if own not in taken:
                    taken.discard(src.name)
                    src.name = own
                    taken.add(src.name)
        copy.name = unique_name(name or src.name, taken)
        taken.add(copy.name)
        if copy.data is not None and copy.data.users == 1:
            copy.data.name = copy.name
        mapping[src] = copy
        pairs.append((src, copy))
    return pairs, negative


# --- Armatures ---

@contextmanager
def edit_armature(context, armature):
    """Edit Mode on `armature` for the duration; yields its edit bones."""
    view_layer = context.view_layer
    previous = view_layer.objects.active
    mode = context.object.mode if context.object else 'OBJECT'
    if mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    view_layer.objects.active = armature
    bpy.ops.object.mode_set(mode='EDIT')
    try:
        yield armature.data.edit_bones
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')
        if previous is not None and previous.name in view_layer.objects:
            view_layer.objects.active = previous


@contextmanager
def at_rest(armature):
    """Show `armature` in its rest pose for the duration: parts that follow its bones
    sit where they were modelled, so building or binding against them can't bake the
    current pose into anything."""
    previous = armature.data.pose_position
    if previous != 'REST':
        armature.data.pose_position = 'REST'
        bpy.context.view_layer.update()
    try:
        yield armature
    finally:
        if previous != 'REST':
            armature.data.pose_position = previous
            bpy.context.view_layer.update()


def new_armature(context, name, location=(0.0, 0.0, 0.0)):
    """An empty armature object linked to the active collection."""
    data = bpy.data.armatures.new(name)
    arm = bpy.data.objects.new(name, data)
    arm.location = location
    arm.matrix_world = Matrix.Translation(location)  # current before the next update
    arm.show_in_front = True
    data.display_type = 'STICK'
    context.collection.objects.link(arm)
    return arm


def parts_base(objects):
    """World point under the middle of `objects` (their bounding boxes) on the level of
    their lowest point: where a new rig's origin and root bone go, so the root sits
    under the model instead of at the world origin."""
    corners = [o.matrix_world @ Vector(c) for o in objects
               if o.type not in {'ARMATURE', 'CAMERA', 'LIGHT'} for c in o.bound_box]
    if not corners:
        return Vector((0.0, 0.0, 0.0))
    lo = Vector([min(c[i] for c in corners) for i in range(3)])
    hi = Vector([max(c[i] for c in corners) for i in range(3)])
    return Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z))


def rig_name(objects):
    """A rig name from the parts: '<first part's base>_Rig'."""
    base = objects[0].name if objects else "Mech"
    base = _PAIR_TAIL_RE.sub("", base)
    base = flip_side(base) and _SIDE_RE.sub("", base).strip("._- ") or base
    return f"{base or 'Mech'}_Rig"


def object_axis(obj, axis):
    """World direction of `obj`'s local `axis` ('X' ... '-Z'), normalised."""
    sign = -1.0 if axis.startswith("-") else 1.0
    column = obj.matrix_world.to_3x3().col[AXES[axis[-1]]]
    return (column.normalized() if column.length > 1e-12 else Vector((0, 0, 1))) * sign


def _matrix_axis(matrix, axis):
    """World direction of a world `matrix`'s local `axis`, normalised."""
    sign = -1.0 if axis.startswith("-") else 1.0
    column = matrix.to_3x3().col[AXES[axis[-1]]]
    return (column.normalized() if column.length > 1e-12 else Vector((0, 0, 1))) * sign


def part_size(obj):
    """Largest world-space dimension of `obj` (at least a millimetre)."""
    return max(max(obj.dimensions), 0.001)


def part_axis(obj, axis='AUTO'):
    """The local axis a part's bone runs along. 'AUTO' reads the shape: when two
    dimensions match (within 15%) and the third doesn't, the odd one out is a bar's
    length or a disc's axle; otherwise the longest side; a cube falls back to Z."""
    if axis != 'AUTO':
        return axis
    dims = [max(d, 1e-6) for d in obj.dimensions]
    order = sorted(range(3), key=lambda i: dims[i])
    lo, mid, hi = (dims[i] for i in order)
    def same(a, b):
        return abs(a - b) <= 0.15 * max(a, b)
    if same(lo, mid) and same(mid, hi):
        return 'Z'
    if same(lo, mid):
        return "XYZ"[order[2]]  # a bar: its length
    if same(mid, hi):
        return "XYZ"[order[0]]  # a disc: its axle
    return "XYZ"[order[2]]


def _reach(obj, frame, direction):
    """(forward, backward): how far `obj`'s geometry reaches from its origin along the
    world `direction` and against it, with the object at world `frame`."""
    origin = frame.translation
    along = [(frame @ Vector(corner) - origin).dot(direction) for corner in obj.bound_box]
    return max(max(along), 0.0), max(-min(along), 0.0)


def _bone_span(obj, frame, direction, centre):
    """(direction, reach) for a part's bone: it points to the side the part reaches
    furthest (an arm from its pivot to its far end). A part that reaches about as far
    both ways (a wheel along its axle) points toward `centre`, so the left and right
    sides of a vehicle both face inward."""
    forward, backward = _reach(obj, frame, direction)
    if backward > forward * 1.1 + 1e-6:
        return -direction, backward
    if forward > backward * 1.1 + 1e-6:
        return direction, forward
    if (centre - frame.translation).dot(direction) < -1e-4:
        return -direction, backward
    return direction, forward


def _roll_axis(obj, axis):
    """The local axis a bone's Z (roll) follows: Y for bones along Z, else Z."""
    return object_axis(obj, 'Y' if axis[-1] == 'Z' else 'Z')


def _bone_name(obj):
    return obj.name


def ensure_root(edit_bones, length=1.0):
    """The root bone at the armature origin (made if missing)."""
    root = edit_bones.get(ROOT_BONE)
    if root is None:
        root = edit_bones.new(ROOT_BONE)
        root.head = (0.0, 0.0, 0.0)
        root.tail = (0.0, 0.0, max(length, 0.01))
    return root


LAST_STRIPPED = []  # bones whose drivers the last bones_from_origins removed


def bones_from_origins(context, objects, armature=None, axis='Z', length=1.0,
                       root=True, aim_children=False, bind='PARENT'):
    """One bone per object at its origin, aimed along the object's local `axis` toward
    the side the part reaches furthest (inward to the middle when it's even both ways).

    Bone parents follow the object parents within `objects`; top-level bones go under a
    root bone when `root` is set. `length` is a fraction of how far each part reaches
    along its bone: 1 runs the bone from the origin to the part's far end.
    `aim_children` points a bone with exactly one child part at that child's origin.
    Re-running on the same rig moves the existing bones instead of adding new ones.
    Returns (armature object, {object: bone name}).
    """
    objects = [o for o in objects if o.type not in {'ARMATURE', 'CAMERA', 'LIGHT'}]
    if not objects:
        return armature, {}
    if armature is None:
        armature = new_armature(context, rig_name(objects), parts_base(objects))
    with at_rest(armature):
        return _bones_from_origins(context, objects, armature, axis, length, root,
                                   aim_children, bind)


def _bones_from_origins(context, objects, armature, axis, length, root, aim_children, bind):
    context.view_layer.update()
    to_rig = armature.matrix_world.inverted()
    rig_rot = to_rig.to_3x3()
    ordered = sorted(objects, key=_depth)
    in_set = set(ordered)
    names = {}
    # (At rest.) Parts already bound to bones swing with them when the bones are
    # re-oriented, so remember where everything is and put it back after binding.
    placed = {o: rest_world(o) for o in ordered}  # at rest on whatever rig it's on now
    was = {b.name: b.matrix_local.copy() for b in armature.data.bones}
    origins = [m.translation for m in placed.values()]
    centre = Vector([(min(p[i] for p in origins) + max(p[i] for p in origins)) / 2 for i in range(3)])
    with edit_armature(context, armature) as edit_bones:
        base = ensure_root(edit_bones, max(part_size(o) for o in ordered)) if root else None
        for obj in ordered:
            name = _bone_name(obj)
            existing = edit_bones.get(name)
            bone = existing or edit_bones.new(name)
            own_axis = part_axis(obj, axis)
            frame = placed[obj]
            head = to_rig @ frame.translation
            world_dir, reach = _bone_span(obj, frame, _matrix_axis(frame, own_axis), centre)
            direction = (rig_rot @ world_dir).normalized()
            reach = max(reach, part_size(obj) * 0.25) * rig_rot.median_scale
            bone.head = head
            bone.tail = head + direction * max(reach * length, 0.005)
            bone.align_roll(rig_rot @ _matrix_axis(frame, 'Y' if own_axis[-1] == 'Z' else 'Z'))
            bone.use_connect = False
            bone.use_deform = True
            parent = obj.parent if obj.parent in in_set else None
            if parent in names:
                bone.parent = edit_bones.get(names[parent])
            elif existing is None or bone.parent is None:
                bone.parent = base
            # else: re-running keeps the bone where it is. Once parts are bound their
            # object parent is the rig, so the bone hierarchy is the only record of
            # the part hierarchy (and of a drive control above the body).
            names[obj] = bone.name
        if aim_children:
            for obj in ordered:
                children = [c for c in obj.children if c in in_set]
                if len(children) != 1:
                    continue
                bone = edit_bones[names[obj]]
                target = to_rig @ placed[children[0]].translation
                if (target - bone.head).length > 1e-4:
                    bone.tail = target
                    own_axis = part_axis(obj, axis)
                    bone.align_roll(rig_rot @ _matrix_axis(
                        placed[obj], 'Y' if own_axis[-1] == 'Z' else 'Z'))
        if base is not None:
            base.use_deform = False
    def reoriented(name):
        old, new = was[name].to_3x3(), armature.data.bones[name].matrix_local.to_3x3()
        return any(abs(old[r][c] - new[r][c]) > 1e-4 for r in range(3) for c in range(3))
    turned = [n for n in names.values() if n in was and reoriented(n)]
    global LAST_STRIPPED
    LAST_STRIPPED = strip_bone_drivers(armature, turned)
    context.view_layer.update()
    for obj, matrix in placed.items():
        set_rest_world(obj, matrix)
    context.view_layer.update()
    bind_parts(armature, names, bind)
    for obj, matrix in placed.items():
        set_rest_world(obj, matrix)
    return armature, names


# --- Binding ---

def _keep_world(obj, assign):
    world = obj.matrix_world.copy()
    assign()
    obj.matrix_world = world


def bind_parts(armature, names, bind):
    """Bind each object in {object: bone name} to its bone: 'PARENT', 'WEIGHTS',
    'HIERARCHY' or 'NONE'."""
    if bind == 'NONE':
        return
    if bind == 'HIERARCHY':
        keep_hierarchy(bpy.context, armature, names)
        return
    for obj, bone in names.items():
        if bind == 'WEIGHTS' and obj.type == 'MESH':
            rigid_skin(obj, armature, bone)
        else:
            parent_to_bone(obj, armature, bone)


def keep_hierarchy(context, armature, names):
    """Bind {object: bone name} without touching the objects' parents.

    An object can only have one parent, so instead of parenting to the bone each part
    gets a helper bone (non-deforming, a child of its bone) lying exactly on the object's
    origin and axes, and copies its world location and rotation. The Outliner keeps the
    part hierarchy; scale stays the object's own. Make Game Ready and Make Engine Mesh
    turn these into skinned meshes for engines.
    """
    with at_rest(armature):
        _keep_hierarchy(context, armature, names)


def _keep_hierarchy(context, armature, names):
    to_rig = armature.matrix_world.inverted()
    with edit_armature(context, armature) as edit_bones:
        for obj, bone in names.items():
            helper = edit_bones.get(bone + FOLLOW_SUFFIX) or edit_bones.new(bone + FOLLOW_SUFFIX)
            loc, rot, _scale = obj.matrix_world.decompose()
            axes = to_rig.to_3x3().normalized() @ rot.to_matrix()
            size = max(edit_bones[bone].length * 0.3, 0.01)
            helper.head = to_rig @ loc
            helper.tail = helper.head + axes.col[1] * size  # bone Y = object Y
            helper.align_roll(axes.col[2])                  # bone Z = object Z
            helper.use_deform = False
            helper.use_connect = False
            helper.parent = edit_bones[bone]
    for obj, bone in names.items():
        if obj.parent == armature and obj.parent_type == 'BONE':
            _keep_world(obj, lambda o=obj: setattr(o, "parent", None))
        for con in [c for c in obj.constraints if c.name in FOLLOW_CONSTRAINTS]:
            obj.constraints.remove(con)
        for kind, label in zip(('COPY_LOCATION', 'COPY_ROTATION'), FOLLOW_CONSTRAINTS):
            con = obj.constraints.new(kind)
            con.name = label
            con.target = armature
            con.subtarget = bone + FOLLOW_SUFFIX


def follow_bone(obj):
    """(armature, part bone) for a Keep Hierarchy part, else (None, None)."""
    for con in getattr(obj, "constraints", ()):
        if con.name == FOLLOW_CONSTRAINTS[0] and con.target is not None \
                and con.target.type == 'ARMATURE' and con.subtarget.endswith(FOLLOW_SUFFIX):
            bone = con.subtarget[:-len(FOLLOW_SUFFIX)]
            if bone in con.target.data.bones:
                return con.target, bone
    return None, None


def follows_pose(obj, armature=None):
    """True when `obj` is moved by a posed bone directly (bone parent or Keep Hierarchy),
    not by an Armature modifier."""
    if obj.parent is not None and obj.parent.type == 'ARMATURE' and obj.parent_type == 'BONE':
        return armature is None or obj.parent == armature
    rig, _bone = follow_bone(obj)
    return rig is not None and (armature is None or rig == armature)


def unfollow(obj):
    """Remove Keep Hierarchy constraints from `obj`."""
    for con in [c for c in obj.constraints if c.name in FOLLOW_CONSTRAINTS]:
        obj.constraints.remove(con)


def _bone_rest_frame(armature, bone):
    """World matrix a child of `bone` hangs from at rest: the bone's tail."""
    data = armature.data.bones[bone]
    return armature.matrix_world @ data.matrix_local @ Matrix.Translation((0.0, data.length, 0.0))


def set_rest_world(obj, matrix):
    """Place `obj` at world `matrix` as it stands at its rig's rest pose. For a bone
    child this is computed from the bone's rest matrix directly: Blender's own
    matrix_world setter uses the bone's last posed matrix and would bake that pose in."""
    if obj.parent is not None and obj.parent.type == 'ARMATURE' and obj.parent_type == 'BONE'             and obj.parent_bone in obj.parent.data.bones:
        obj.matrix_parent_inverse = Matrix.Identity(4)
        obj.matrix_basis = _bone_rest_frame(obj.parent, obj.parent_bone).inverted() @ matrix
    else:
        obj.matrix_world = matrix


def rest_world(obj):
    """Where `obj` is at its rig's rest pose (where it was modelled)."""
    armature = obj.parent if obj.parent is not None and obj.parent.type == 'ARMATURE' else None
    if armature is None:
        armature = follow_bone(obj)[0]
    if armature is None:
        return obj.matrix_world.copy()
    with at_rest(armature):
        return obj.matrix_world.copy()


def parent_to_bone(obj, armature, bone):
    """Parent `obj` to `bone`, keeping it where it is at the rig's rest pose (a part is
    modelled at rest; binding against a posed bone would bake that pose into it)."""
    world = rest_world(obj)
    obj.parent = armature
    obj.parent_type = 'BONE'
    obj.parent_bone = bone
    set_rest_world(obj, world)


def armature_modifier(obj, armature):
    """The Armature modifier on `obj` using `armature` (added first in the stack if missing)."""
    for mod in obj.modifiers:
        if mod.type == 'ARMATURE' and mod.object == armature:
            return mod
    mod = obj.modifiers.new("Armature", 'ARMATURE')
    mod.object = armature
    while obj.modifiers[0] != mod:
        bpy.context.view_layer.objects.active = obj
        with bpy.context.temp_override(object=obj):
            bpy.ops.object.modifier_move_up(modifier=mod.name)
    return mod


def rigid_skin(obj, armature, bone, posed=None):
    """Skin the whole of `obj` to `bone` at full weight (for game engines). `posed`
    says `obj` sits where the pose put it (default: it follows a bone directly)."""
    if posed is None:
        posed = follows_pose(obj, armature)
    pose = _pose_delta(armature, bone) if posed and bone in armature.pose.bones \
        else Matrix.Identity(4)
    world = pose.inverted() @ obj.matrix_world
    unfollow(obj)
    for group in list(obj.vertex_groups):
        if group.name in armature.data.bones:
            obj.vertex_groups.remove(group)
    group = obj.vertex_groups.new(name=bone)
    group.add(range(len(obj.data.vertices)), 1.0, 'REPLACE')
    armature_modifier(obj, armature)
    # A part riding on a posed bone goes back to where it is at rest (`world` above):
    # the Armature modifier puts the pose back on, so keeping the posed place would pose
    # it twice. Its children keep their own world placement.
    children = {c: c.matrix_world.copy() for c in obj.children}
    obj.parent = armature
    obj.parent_type = 'OBJECT'
    obj.parent_bone = ""
    obj.matrix_world = world
    for child, matrix in children.items():
        child.matrix_world = matrix


def _pose_delta(armature, bone):
    """The world transform posing `bone` applies to what rides on it."""
    rest = armature.data.bones[bone].matrix_local
    posed = armature.pose.bones[bone].matrix
    return armature.matrix_world @ posed @ rest.inverted() @ armature.matrix_world.inverted()


def skinning_armature(obj):
    """The armature `obj` follows (bone parent or Armature modifier), or None."""
    if obj.parent is not None and obj.parent.type == 'ARMATURE' and obj.parent_type == 'BONE':
        return obj.parent
    rig, _bone = follow_bone(obj)
    if rig is not None:
        return rig
    for mod in getattr(obj, "modifiers", ()):
        if mod.type == 'ARMATURE' and mod.object is not None:
            return mod.object
    return None


def bone_target(obj):
    """(armature, bone name) when `obj` is driven by one bone, else (None, None)."""
    if obj.parent is not None and obj.parent.type == 'ARMATURE':
        if obj.parent_type == 'BONE' and obj.parent_bone in obj.parent.data.bones:
            return obj.parent, obj.parent_bone
    rig, bone = follow_bone(obj)
    if rig is not None:
        return rig, bone
    for mod in getattr(obj, "modifiers", ()):
        if mod.type == 'ARMATURE' and mod.object is not None:
            groups = [g.name for g in obj.vertex_groups if g.name in mod.object.data.bones]
            if len(groups) == 1:
                return mod.object, groups[0]
    return None, None


# --- Hinges ---

def hinge_from_points(points):
    """(centre, axis) of a hinge from world points: two points give the line between
    them; more (an axle ring, a face) give their centre and the normal of their plane."""
    import numpy as np
    pts = np.array([tuple(p) for p in points], dtype=np.float64)
    if len(pts) == 0:
        raise ValueError("Nothing selected")
    centre = pts.mean(axis=0)
    if len(pts) == 1:
        return Vector(centre), Vector((0.0, 0.0, 1.0))
    if len(pts) == 2:
        axis = pts[1] - pts[0]
    else:
        # The plane normal is the direction the points spread least in.
        _, _, vt = np.linalg.svd(pts - centre)
        axis = vt[2]
    axis = Vector(axis)
    if axis.length < 1e-9:
        axis = Vector((0.0, 0.0, 1.0))
    axis.normalize()
    # Point the same way each time: the largest component positive.
    if axis[max(range(3), key=lambda i: abs(axis[i]))] < 0:
        axis.negate()
    return Vector(centre), axis


def selected_points(obj):
    """World positions of the selected vertices of `obj` (Edit Mode); when no vertex is
    flagged selected, those of the selected edges and faces."""
    bm = bmesh.from_edit_mesh(obj.data)
    verts = {v for v in bm.verts if v.select}
    if not verts:
        for elem in (*bm.edges, *bm.faces):
            if elem.select:
                verts.update(elem.verts)
    world = obj.matrix_world
    return [world @ v.co for v in sorted(verts, key=lambda v: v.index)]


def _perpendicular(axis):
    other = Vector((0.0, 0.0, 1.0)) if abs(axis.z) < 0.9 else Vector((1.0, 0.0, 0.0))
    return axis.cross(other).normalized()


def bone_on_hinge(context, armature, obj, centre, axis, length=None, name=None, root=True):
    """A bone on the hinge `centre`, pointing along its `axis` (so the bone's Y is the
    hinge axis). Reuses a bone of the same name. Returns its name."""
    length = length or max(part_size(obj) * 0.5, 0.01)
    to_rig = armature.matrix_world.inverted()
    rig_rot = to_rig.to_3x3()
    name = name or _bone_name(obj)
    with edit_armature(context, armature) as edit_bones:
        root = ensure_root(edit_bones, length * 2) if root else edit_bones.get(ROOT_BONE)
        if root is not None:
            root.use_deform = False
        bone = edit_bones.get(name) or edit_bones.new(name)
        head = to_rig @ centre
        bone.head = head
        bone.tail = head + (rig_rot @ axis).normalized() * length
        bone.align_roll(rig_rot @ _perpendicular(axis))
        bone.use_connect = False
        if bone.parent is None and root is not None and bone != root:
            bone.parent = root
        name = bone.name
    return name


def set_origin(obj, matrix):
    """Move `obj`'s origin to `matrix` (world, unscaled) without moving its mesh or
    its children."""
    children = {c: c.matrix_world.copy() for c in obj.children}
    old = obj.matrix_world.copy()
    if obj.type == 'MESH':
        obj.data.transform(matrix.inverted() @ old)
        obj.data.update()
    loc, rot, _ = matrix.decompose()
    obj.matrix_world = Matrix.LocRotScale(loc, rot, Vector((1.0, 1.0, 1.0)))
    for child, world in children.items():
        child.matrix_world = world


def origin_to_hinge(obj, centre, axis):
    """Put `obj`'s origin on the hinge with its local Z along the hinge axis."""
    z_axis = axis.normalized()
    x_axis = _perpendicular(z_axis)
    y_axis = z_axis.cross(x_axis)
    rot = Matrix((x_axis, y_axis, z_axis)).transposed().to_4x4()
    set_origin(obj, Matrix.Translation(centre) @ rot)


def hinge_limits(armature, bones, axis='Y', minimum=-1.0, maximum=1.0, lock_location=True):
    """Make `bones` hinges: free to turn around one local `axis` within the limits,
    other rotation axes (and optionally location) locked."""
    index = AXES[axis]
    for name in bones:
        pose_bone = armature.pose.bones[name]
        pose_bone.rotation_mode = 'XYZ'
        pose_bone.lock_rotation = [i != index for i in range(3)]
        pose_bone.lock_rotation_w = True
        if lock_location:
            pose_bone.lock_location = (True, True, True)
        for con in [c for c in pose_bone.constraints if c.name == "Hinge Limit"]:
            pose_bone.constraints.remove(con)
        con = pose_bone.constraints.new('LIMIT_ROTATION')
        con.name = "Hinge Limit"
        con.owner_space = 'LOCAL'
        for i, key in enumerate("xyz"):
            setattr(con, f"use_limit_{key}", True)
            setattr(con, f"min_{key}", minimum if i == index else 0.0)
            setattr(con, f"max_{key}", maximum if i == index else 0.0)
    return len(bones)


# --- IK ---

IK_PREFIX = "IK_"
POLE_PREFIX = "POLE_"


def _signed_angle(u, v, normal):
    angle = u.angle(v)
    return -angle if u.cross(v).angle(normal) < 1.0 else angle


def _pole_angle(base, tip, pole):
    """The IK pole angle that keeps `base`'s rest orientation (edit bones, rig space)."""
    pole_normal = (tip.tail - base.head).cross(pole - base.head)
    projected = pole_normal.cross(base.tail - base.head)
    return _signed_angle(base.x_axis, projected, base.tail - base.head)


def chain_bones(armature, tip, length):
    """`tip` and its parents, `length` bones in all, base first (stops at the root)."""
    names = []
    bone = armature.data.bones.get(tip)
    while bone is not None and len(names) < length and bone.name != ROOT_BONE:
        names.append(bone.name)
        bone = bone.parent
    return list(reversed(names))


def ik_chained(armature):
    """Names of every bone an IK constraint moves."""
    names = set()
    for pose_bone in armature.pose.bones:
        for con in pose_bone.constraints:
            if con.type == 'IK' and not con.mute:
                names.update(chain_bones(armature, pose_bone.name, con.chain_count or 255))
    return names


def transfer_hinge_limits(armature, names):
    """IK ignores Limit Rotation, so give the chain's hinges IK locks and limits instead."""
    for name in names:
        pose_bone = armature.pose.bones[name]
        hinge = next((c for c in pose_bone.constraints if c.name == "Hinge Limit"), None)
        if hinge is None:
            continue
        for i, key in enumerate("xyz"):
            free = not pose_bone.lock_rotation[i]
            setattr(pose_bone, f"lock_ik_{key}", not free)
            if free:
                setattr(pose_bone, f"use_ik_limit_{key}", True)
                setattr(pose_bone, f"ik_min_{key}", getattr(hinge, f"min_{key}"))
                setattr(pose_bone, f"ik_max_{key}", getattr(hinge, f"max_{key}"))


def ik_chain(context, armature, tip, length=2, pole=True, follow='MOUNT'):
    """IK on `tip` over `length` bones: a target bone at the tip's end and, for chains of
    two or more, a pole bone on the bending side. Hinge Limits on the chain become IK
    locks and limits. Re-running moves the existing target and pole.

    `follow` 'MOUNT' hangs the target and pole off the bone the chain is mounted on (a
    turret, a chassis), so slewing or driving the machine carries the reach with it;
    'ROOT' leaves them in the world, so the tip stays put while the base moves.
    Returns (target bone, pole bone or None, chain bone names).
    """
    names = chain_bones(armature, tip, length)
    if not names:
        raise ValueError(f"No bone '{tip}' to put IK on")
    target_name, pole_name = IK_PREFIX + tip, POLE_PREFIX + tip
    use_pole = pole and len(names) >= 2
    with edit_armature(context, armature) as edit_bones:
        root = edit_bones.get(ROOT_BONE)
        tip_bone = edit_bones[tip]
        base_bone = edit_bones[names[0]]
        if follow == 'MOUNT' and base_bone.parent is not None:
            root = base_bone.parent
        size = max(tip_bone.length * 0.4, 0.02)
        target = edit_bones.get(target_name) or edit_bones.new(target_name)
        target.head = tip_bone.tail
        target.tail = tip_bone.tail + (tip_bone.tail - tip_bone.head).normalized() * size
        target.roll = tip_bone.roll
        target.use_deform = False
        target.parent = root
        pole_head = None
        if use_pole:
            # The pole sits off the middle joint, on the side the chain bends towards.
            mid = tip_bone.head
            line = tip_bone.tail - base_bone.head
            offset = mid - base_bone.head
            side = offset - line * (offset.dot(line) / max(line.length_squared, 1e-12))
            if side.length < 1e-6:
                side = tip_bone.z_axis.copy()
            reach = max(line.length * 0.5, 0.05)
            pole_head = mid + side.normalized() * reach
            pole_bone = edit_bones.get(pole_name) or edit_bones.new(pole_name)
            pole_bone.head = pole_head
            pole_bone.tail = pole_head + Vector((0.0, 0.0, size))
            pole_bone.use_deform = False
            pole_bone.parent = root
            angle = _pole_angle(base_bone, tip_bone, pole_head)
    pose_bone = armature.pose.bones[tip]
    for con in [c for c in pose_bone.constraints if c.type == 'IK']:
        pose_bone.constraints.remove(con)
    con = pose_bone.constraints.new('IK')
    con.name = "IK"
    con.target = armature
    con.subtarget = target_name
    con.chain_count = len(names)
    con.use_tail = True
    if use_pole:
        con.pole_target = armature
        con.pole_subtarget = pole_name
        con.pole_angle = angle
    transfer_hinge_limits(armature, names)
    return target_name, (pole_name if use_pole else None), names


# --- Pistons ---

ANCHOR_SUFFIX = "_pivot"


def piston(context, armature, cylinder, rod, bind='PARENT'):
    """Rig a piston without a dependency cycle.

    Each part gets a non-deforming anchor bone on its pivot (parented like the part is,
    so it rides on whatever the part is mounted to) and a deforming bone that tracks
    the other part's anchor. Neither tracking bone targets the other, so Blender has no
    cycle to warn about and nothing jitters. Returns (cylinder bone, rod bone).
    """
    to_rig = armature.matrix_world.inverted()
    heads = {o: to_rig @ o.matrix_world.translation for o in (cylinder, rod)}
    if (heads[rod] - heads[cylinder]).length < 1e-5:
        raise ValueError("The cylinder and rod pivots are in the same place")
    names, anchors = {}, {}
    with edit_armature(context, armature) as edit_bones:
        root = edit_bones.get(ROOT_BONE)
        for part, other in ((cylinder, rod), (rod, cylinder)):
            name = _bone_name(part)
            span = heads[other] - heads[part]
            length = max(span.length * 0.5, 0.005)
            existing = edit_bones.get(name)
            # The anchor takes over the part's place in the hierarchy.
            anchor = edit_bones.get(name + ANCHOR_SUFFIX) or edit_bones.new(name + ANCHOR_SUFFIX)
            anchor.head = heads[part]
            anchor.tail = heads[part] + span.normalized() * length * 0.5
            anchor.use_deform = False
            anchor.use_connect = False
            if existing is not None and existing.parent is not None and                     existing.parent != anchor:
                anchor.parent = existing.parent
            elif anchor.parent is None:
                anchor.parent = root
            bone = existing or edit_bones.new(name)
            bone.head = heads[part]
            bone.tail = heads[part] + span.normalized() * length
            bone.use_connect = False
            bone.use_deform = True
            bone.parent = anchor
            names[part], anchors[part] = bone.name, anchor.name
    for part, other in ((cylinder, rod), (rod, cylinder)):
        pose_bone = armature.pose.bones[names[part]]
        for con in [c for c in pose_bone.constraints if c.type == 'DAMPED_TRACK']:
            pose_bone.constraints.remove(con)
        con = pose_bone.constraints.new('DAMPED_TRACK')
        con.name = "Piston Track"
        con.target = armature
        con.subtarget = anchors[other]
        con.head_tail = 0.0
    bind_parts(armature, names, bind)
    return names[cylinder], names[rod]


# --- Gears and sprockets ---

def spin_radius(obj, axis='Z'):
    """Radius of `obj` around its local `axis`: half its largest extent across it."""
    index = AXES[axis[-1]]
    dims = obj.dimensions
    return max(max(dims[i] for i in range(3) if i != index) / 2.0, 1e-6)


def _rotation_owner(obj):
    """(id, data path prefix, struct) whose rotation turns `obj`: its bone or itself."""
    armature, bone = bone_target(obj)
    if armature is not None:
        pose_bone = armature.pose.bones[bone]
        return armature, f'pose.bones["{bone}"].', pose_bone
    return obj, "", obj


def rest_to_delta(obj, spin=None):
    """Move an unrigged part's resting rotation into its delta rotation, so its rotation
    channels start at zero and each one turns it about its own local axis.

    Euler channels only turn about the part's own axes when the others are zero: a gear
    stood on edge (X = 90) and then spun on its Z channel swings about the world's Z,
    not its axle. Blender applies the delta rotation first and the rotation on top, so
    with the rest in the delta, a spin on any one channel is about that local axis.
    Parts on a bone are left alone (their bone's channels already rest at zero), and so
    are parts whose rotation is animated or driven already, and parts turned only on
    channel `spin` (the one about to be driven: alone, it is already about its own
    axis). Returns True if it moved.
    """
    if obj is None or bone_target(obj)[0] is not None:
        return False
    if obj.rotation_mode in {'QUATERNION', 'AXIS_ANGLE'}:
        obj.rotation_mode = 'XYZ'  # converts the stored rotation
    if all(abs(v) < 1e-7 for i, v in enumerate(obj.rotation_euler) if i != spin):
        return False
    anim = obj.animation_data
    if anim is not None:
        if any(fc.data_path == "rotation_euler" for fc in anim.drivers):
            return False
        if anim.action is not None and any(
                fc.data_path == "rotation_euler" for fc in _action_fcurves(anim.action)):
            return False
    bpy.context.view_layer.update()
    before = obj.matrix_world.copy()
    old = (obj.rotation_euler.copy(), obj.delta_rotation_euler.copy())
    rest = obj.delta_rotation_euler.to_matrix() @ obj.rotation_euler.to_matrix()
    obj.delta_rotation_euler = rest.to_euler(obj.rotation_mode)
    obj.rotation_euler = (0.0, 0.0, 0.0)
    bpy.context.view_layer.update()
    if any(abs(a - b) > 1e-4 for ra, rb in zip(before, obj.matrix_world) for a, b in zip(ra, rb)):
        obj.rotation_euler, obj.delta_rotation_euler = old  # scaled deltas and the like: leave it
        bpy.context.view_layer.update()
        return False
    return True


def channel_axis(obj, axis):
    """(channel index, sign) of `obj`'s local `axis` in the rotation and location
    channels that move it: the object's own, or its bone's when it's rigged. A bone
    built along a part's Z has its own Y there, so the part's Z spin is the bone's Y."""
    index = AXES[axis[-1]]
    armature, bone = bone_target(obj)
    if armature is None:
        return index, 1.0
    # Compare the part with its bone as they are now: a bone-parented part moves with the
    # pose, a skinned one stays where it was bound (at the bone's rest).
    if follows_pose(obj, armature):
        frame = armature.matrix_world @ armature.pose.bones[bone].matrix
    else:
        frame = armature.matrix_world @ armature.data.bones[bone].matrix_local
    local = frame.to_3x3().inverted() @ obj.matrix_world.to_3x3().col[index]
    best = max(range(3), key=lambda i: abs(local[i]))
    return best, (1.0 if local[best] > 0 else -1.0)


def link_rotation(driver, driven, axis='Z', mode='MESHED', ratio=None):
    """Make `driven` turn with `driver` around their local `axis`.

    'MESHED' gears turn the opposite way; 'CHAINED' sprockets (on one belt or chain)
    turn the same way. The ratio defaults to driver radius / driven radius. When a part
    is bound to a bone, the bone is driven instead of the object.
    """
    src_axis, dst_axis = wheel_axle(driver, axis), wheel_axle(driven, axis)
    rest_to_delta(driver, AXES[src_axis[-1]])
    rest_to_delta(driven, AXES[dst_axis[-1]])
    if ratio is None:
        ratio = spin_radius(driver, src_axis) / spin_radius(driven, dst_axis)
    sign = -1.0 if mode == 'MESHED' else 1.0
    src_index, src_sign = channel_axis(driver, src_axis)
    dst_index, dst_sign = channel_axis(driven, dst_axis)
    src_id, src_prefix, src_struct = _rotation_owner(driver)
    dst_id, dst_prefix, dst_struct = _rotation_owner(driven)
    for struct in (src_struct, dst_struct):
        if struct.rotation_mode in {'QUATERNION', 'AXIS_ANGLE'}:
            struct.rotation_mode = 'XYZ'
    path = f"{dst_prefix}rotation_euler"
    dst_id.driver_remove(path, dst_index)
    fcurve = dst_id.driver_add(path, dst_index)
    drv = fcurve.driver
    drv.type = 'SCRIPTED'
    var = drv.variables.new()
    var.name = "spin"
    var.type = 'SINGLE_PROP'
    var.targets[0].id = src_id
    var.targets[0].data_path = f"{src_prefix}rotation_euler[{src_index}]"
    drv.expression = f"spin * {sign * ratio * src_sign * dst_sign:.6g}"
    return sign * ratio


# --- Wheels ---

WHEEL_PROP = "preflight_wheel_roll"  # on the wheel's owner: {fcurve key: [k, offset, d0]}


def _world_owner(obj):
    """(id, bone name or "") whose world transform is `obj`'s: its bone or itself."""
    armature, bone = bone_target(obj)
    if armature is not None:
        return armature, bone
    return obj, ""


def _world_matrix(owner, bone):
    """Evaluated world matrix of an object or one of its pose bones."""
    deps = bpy.context.evaluated_depsgraph_get()
    ev = owner.evaluated_get(deps)
    if bone:
        return ev.matrix_world @ ev.pose.bones[bone].matrix
    return ev.matrix_world.copy()


def _transform_var(drv, name, owner, bone, kind):
    var = drv.variables.new()
    var.name = name
    var.type = 'TRANSFORMS'
    target = var.targets[0]
    target.id = owner
    if bone:
        target.bone_target = bone
    target.transform_type = kind
    target.transform_space = 'WORLD_SPACE'
    target.rotation_mode = 'AUTO'
    return var


def _clear_wheel_drivers(wheel):
    """Remove Wheel Spin drivers left on `wheel` by an earlier run, on every channel,
    and zero the spin they left behind, so the new run reads the wheel as it rests."""
    owner, prefix, struct = _rotation_owner(wheel)
    path = f"{prefix}rotation_euler"
    rolls = dict(owner.get(WHEEL_PROP, {}))
    anim = owner.animation_data
    for key in [k for k in rolls if k.rsplit("|", 1)[0] == path]:
        index = int(key.rsplit("|", 1)[1])
        if anim is not None and anim.drivers.find(path, index=index) is not None:
            owner.driver_remove(path, index)
        if struct.rotation_mode not in {'QUATERNION', 'AXIS_ANGLE'}:
            struct.rotation_euler[index] = 0.0
        del rolls[key]
    if rolls:
        owner[WHEEL_PROP] = rolls
    elif WHEEL_PROP in owner:
        del owner[WHEEL_PROP]


def strip_bone_drivers(armature, bones):
    """Remove rotation drivers from `bones` and zero their rotation: after a bone is
    re-oriented, a driver on its old channel would turn it about the wrong axis.
    Returns the names of bones that had any."""
    anim = armature.animation_data
    stripped = []
    for name in bones:
        prefix = f'pose.bones["{name}"].rotation'
        found = [fc for fc in (anim.drivers if anim else []) if fc.data_path.startswith(prefix)]
        for fc in found:
            anim.drivers.remove(fc)
        pose_bone = armature.pose.bones.get(name)
        if pose_bone is not None:
            pose_bone.rotation_euler = (0.0, 0.0, 0.0)
            pose_bone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        if found:
            stripped.append(name)
    rolls = dict(armature.get(WHEEL_PROP, {}))
    for key in [k for k in rolls if any(k.startswith(f'pose.bones["{n}"]') for n in stripped)]:
        del rolls[key]
    if rolls:
        armature[WHEEL_PROP] = rolls
    elif WHEEL_PROP in armature:
        del armature[WHEEL_PROP]
    return stripped


def _round_axis(obj, bins=24):
    """The local axis a mesh is round about (seen down it, its outline is a circle), or
    None when no axis clearly is. Looked at down each axis, the farthest vertex in each
    slice of angle is the same distance out all the way round only for the axle; a
    cylinder seen from the side is a rectangle (far at the corners, near at the sides)."""
    if obj.type != 'MESH' or len(obj.data.vertices) < 8:
        return None
    import numpy as np
    co = np.empty(len(obj.data.vertices) * 3)
    obj.data.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3) * np.array(tuple(obj.scale))  # the shape as seen, scale counted
    co -= (co.min(axis=0) + co.max(axis=0)) / 2.0
    spread = []
    for axis in range(3):
        i, j = (n for n in range(3) if n != axis)
        r = np.hypot(co[:, i], co[:, j])
        slot = ((np.arctan2(co[:, j], co[:, i]) + math.pi) / math.tau * bins).astype(int) % bins
        far = np.zeros(bins)
        np.maximum.at(far, slot, r)
        spread.append(far.std() / far.mean() if far.mean() > 1e-9 else 1.0)
    best = min(range(3), key=lambda a: spread[a])
    others = [spread[a] for a in range(3) if a != best]
    if spread[best] < 0.06 and min(others) > 2.0 * spread[best] + 0.02:
        return "XYZ"[best]
    return None


def wheel_axle(wheel, axis='AUTO'):
    """The wheel's local axle axis: `axis`, or for 'AUTO' the axis the mesh is round
    about; failing that, the side that isn't like the other two (a disc's thickness, a
    roller's length); else its thinnest side; a wheel as long as it is wide (all sides
    alike) turns about Z, like Blender's cylinders."""
    if axis != 'AUTO':
        return axis
    round_axis = _round_axis(wheel)
    if round_axis is not None:
        return round_axis
    dims = [max(d, 1e-6) for d in wheel.dimensions]
    def same(a, b):
        return abs(a - b) <= 0.05 * max(a, b)
    if same(dims[0], dims[1]) and same(dims[1], dims[2]):
        return 'Z'
    for i in range(3):
        j, k = (n for n in range(3) if n != i)
        if same(dims[j], dims[k]):
            return "XYZ"[i]
    return "XYZ"[min(range(3), key=lambda i: dims[i])]


def _action_fcurves(action):
    """F-curves of `action` in any Blender version (5.x layered actions or the old list)."""
    if not hasattr(action, "layers"):
        return list(getattr(action, "fcurves", ()))
    curves = []
    for layer in action.layers:
        for strip in layer.strips:
            for bag in getattr(strip, "channelbags", ()):
                curves.extend(bag.fcurves)
    return curves


def _euler_struct(struct):
    """Switch `struct` (an object or pose bone) to XYZ Euler rotation, keeping its current
    rotation, unless it has rotation keyframes in another mode. True if it's Euler now."""
    if struct.rotation_mode not in {'QUATERNION', 'AXIS_ANGLE'}:
        return True
    prop = "rotation_quaternion" if struct.rotation_mode == 'QUATERNION' else "rotation_axis_angle"
    path = struct.path_from_id(prop)
    anim = struct.id_data.animation_data
    if anim is not None and anim.action is not None and any(
            fc.data_path == path for fc in _action_fcurves(anim.action)):
        return False
    rotation = struct.matrix_basis.to_euler('XYZ')
    struct.rotation_mode = 'XYZ'
    struct.rotation_euler = rotation
    return True


def _yaw_channels(body):
    """[(object, data path, sign)]: the rotation channels above `body` (its bone, the
    bones it hangs from, the rig object, object parents) that turn it about the world's
    Z. Unlike a heading read from the world matrix, they keep counting past a full turn."""
    found = []
    armature, bone = bone_target(body)
    if armature is not None:
        name = bone
        while name:
            pose_bone, data = armature.pose.bones[name], armature.data.bones[name]
            if _euler_struct(pose_bone):
                rest = (armature.matrix_world @ data.matrix_local).to_3x3()
                c = max(range(3), key=lambda i: abs(rest.col[i].normalized().z))
                up = rest.col[c].normalized().z
                if abs(up) > 0.99:
                    found.append((armature, f'pose.bones["{name}"].rotation_euler[{c}]',
                                  1.0 if up > 0 else -1.0))
            name = data.parent.name if data.parent else None
        obj = armature
    else:
        obj = body
    while obj is not None:
        parent_up = obj.parent.matrix_world.to_3x3().col[2].normalized().z if obj.parent else 1.0
        if abs(parent_up) > 0.99 and _euler_struct(obj) and obj.rotation_mode == 'XYZ':
            found.append((obj, "rotation_euler[2]", 1.0 if parent_up > 0 else -1.0))
        obj = obj.parent
    return found


def _carry_wheels(body, wheels):
    """Hang the wheels' bones (and the chain paths round them) under the body's bone, so
    they go along when the body is driven. Wheels and body that came in as separate
    parts sit side by side under the root; moving the body would leave them behind.
    Returns the wheels moved."""
    rig, body_bone = bone_target(body)
    if rig is None:
        return []
    bones = rig.data.bones
    moved = []
    for wheel in wheels:
        w_rig, w_bone = bone_target(wheel)
        if w_rig is not rig or w_bone == body_bone:
            continue
        b = bones[w_bone]
        if body_bone in {p.name for p in b.parent_recursive}:
            continue  # already rides with the body
        if w_bone in {p.name for p in bones[body_bone].parent_recursive}:
            continue  # the body hangs from the wheel: leave it
        moved.append(w_bone)
    if not moved:
        return []
    above = {p.name for p in bones[body_bone].parent_recursive}
    # Chain paths on a bone above the body (the root): they should ride with the body too.
    paths = {c.target for pb in rig.pose.bones for c in pb.constraints
             if c.type == 'FOLLOW_PATH' and c.target is not None}
    paths = [p for p in paths if p.parent is rig and p.parent_type == 'BONE'
             and p.parent_bone in above]
    rests = {p: rest_world(p) for p in paths}
    with edit_armature(bpy.context, rig) as edit_bones:
        for name in moved:
            edit_bones[name].use_connect = False
            edit_bones[name].parent = edit_bones[body_bone]
    bpy.context.view_layer.update()
    for p in paths:
        p.parent_bone = body_bone
        set_rest_world(p, rests[p])
    return moved


def carry_objects(body, wheels):
    """Parent unrigged wheels to an unrigged body (where they are), so they go along
    when the body is moved. Returns the wheels moved."""
    if bone_target(body)[0] is not None:
        return []
    moved = []
    above = set()
    obj = body
    while obj is not None:
        above.add(obj)
        obj = obj.parent
    for wheel in wheels:
        if bone_target(wheel)[0] is not None or wheel in above:
            continue
        parent = wheel.parent
        while parent is not None and parent is not body:
            parent = parent.parent
        if parent is body:
            continue  # already rides with the body
        world = wheel.matrix_world.copy()
        wheel.parent = body
        wheel.matrix_parent_inverse = body.matrix_world.inverted()
        wheel.matrix_world = world
        moved.append(wheel)
    return moved


def wheel_spin(body, wheels, travel_axis='Y', spin_axis='AUTO', reverse=False, carry=False):
    """Turn each wheel by the distance `body` has moved along its `travel_axis` (on the
    ground, in the world) divided by the wheel's radius, so it rolls without sliding.

    The distance comes from where the body really is, so moving the vehicle by any
    control (its own bone, the root, the rig object or a parent) turns the wheels, and
    the heading follows the body as it turns. Turning also rolls each wheel by its own
    side's share (yaw x its sideways offset), so the two tracks of a skid-steer machine
    counter-rotate when it spins on the spot and the outer one runs faster round a bend.
    The driver sees where the body is, not the path it took, so it is exact for straight
    runs and for spinning about the start point; Bake Wheel Roll adds up each wheel's
    real path for anything else (arcs, loops, reversing).
    A rigged wheel is driven through its bone, which is hung under the body's bone if it
    wasn't already (with any chain path round it), so the wheels go along with the body.
    Returns the radii used.
    """
    for wheel in wheels:
        _clear_wheel_drivers(wheel)
        rest_to_delta(wheel, AXES[wheel_axle(wheel, spin_axis)[-1]])
    _carry_wheels(body, wheels)
    if carry:
        carry_objects(body, wheels)
    bpy.context.view_layer.update()
    owner, bone = _world_owner(body)
    world = body.matrix_world
    travel_dir = object_axis(body, travel_axis)
    flat = Vector((travel_dir.x, travel_dir.y, 0.0))
    if flat.length < 1e-6:
        raise ValueError(f"'{body.name}' travel axis {travel_axis} points straight up")
    flat.normalize()
    heading = math.atan2(flat.y, flat.x)
    yaw_channels = _yaw_channels(body)
    bpy.context.view_layer.update()
    owner_world = _world_matrix(owner, bone)
    yaw0 = owner_world.to_euler().z
    offset = heading - yaw0
    channels0 = sum(sign * idblock.path_resolve(path) for idblock, path, sign in yaw_channels)
    start = owner_world.translation
    rolling = Vector((0.0, 0.0, 1.0)).cross(flat)  # how a wheel on the ground turns
    left = rolling  # Z x forward: the vehicle's left
    to_owner = owner_world.inverted()
    radii = []
    for wheel in wheels:
        axle = wheel_axle(wheel, spin_axis)
        radius = spin_radius(wheel, axle)
        spin, spin_sign = channel_axis(wheel, axle)
        facing = rolling.dot(object_axis(wheel, axle))
        sign = (1.0 if facing > 0 else -1.0) if abs(facing) > 0.3 else -1.0
        if reverse:
            sign = -sign
        k = sign * spin_sign / radius
        centre = wheel.matrix_world.translation
        lateral = (centre - start).dot(left)  # + on the left: it slows turning left
        local = to_owner @ centre
        dst_id, dst_prefix, dst_struct = _rotation_owner(wheel)
        if dst_struct.rotation_mode in {'QUATERNION', 'AXIS_ANGLE'}:
            dst_struct.rotation_mode = 'XYZ'
        path = f"{dst_prefix}rotation_euler"
        dst_id.driver_remove(path, spin)
        drv = dst_id.driver_add(path, spin).driver
        drv.type = 'SCRIPTED'
        _transform_var(drv, "x", owner, bone, 'LOC_X')
        _transform_var(drv, "y", owner, bone, 'LOC_Y')
        _transform_var(drv, "yaw", owner, bone, 'ROT_Z')
        def num(v):
            v = 0.0 if abs(v) < 1e-9 else v
            return f"{v:.6g}"

        def plus(v):  # a constant to add, written without a unary sign
            v = 0.0 if abs(v) < 1e-9 else v
            return f"+{num(v)}" if v >= 0 else f"-{num(-v)}"

        terms = []
        for i, (idblock, data_path, sign) in enumerate(yaw_channels):
            var = drv.variables.new()
            var.name = f"r{i}"
            var.type = 'SINGLE_PROP'
            var.targets[0].id_type = 'OBJECT'
            var.targets[0].id = idblock
            var.targets[0].data_path = data_path
            terms.append(("-" if sign < 0 else ("+" if terms else "")) + f"r{i}")
        # Distance = how far the body has come from its start, along where it now faces
        # (never its absolute position: that would read a phantom distance when the
        # heading turns anywhere but the world origin), less each side's share of the
        # turn. The world matrix gives the heading only within +-180 deg, so the turn
        # counts the rotation channels above the body, S (they go on past a full turn),
        # and uses the world heading only to correct what they miss:
        #   turn = S - S0 + wrap(yaw - yaw0 - (S - S0)),  wrap(a) = atan2(sin a, cos a)
        # Blender keeps only 256 characters of an expression, so it's written compactly.
        along = f"(x{plus(-start.x)})*cos(yaw{plus(offset)})+(y{plus(-start.y)})*sin(yaw{plus(offset)})"
        sum_terms = "".join(terms)
        if sum_terms.startswith("-"):
            sum_terms = "0" + sum_terms  # no unary sign: Blender's fast parser rejects it
        turn = None
        if terms:
            k_wrap = plus(channels0 - yaw0)
            turn = (f"({sum_terms}{plus(-channels0)}+atan2(sin(yaw-({sum_terms}){k_wrap}),"
                    f"cos(yaw-({sum_terms}){k_wrap})))")
            expression = f"({along}-{turn}*{num(lateral)})*{num(k)}"
        if not terms or len(expression) > 250:
            for var in [v for v in drv.variables if v.name.startswith("r")]:
                drv.variables.remove(var)
            turn = f"atan2(sin(yaw{plus(-yaw0)}),cos(yaw{plus(-yaw0)}))"
            expression = f"({along}-{turn}*{num(lateral)})*{num(k)}"
        drv.expression = expression
        rolls = dict(dst_id.get(WHEEL_PROP, {}))
        rolls[f"{path}|{spin}"] = [k, offset, start.x, start.y, lateral, yaw0, *local]
        dst_id[WHEEL_PROP] = rolls
        radii.append(radius)
    return radii


DRIVE_BONE = "drive"


def add_drive_bone(context, armature, body_bone, forward):
    """A "drive" control flat on the ground under `body_bone`, pointing `forward`
    (world), between the root and the body: move and turn it to drive the vehicle,
    and leave the root for placing the rig in the scene. Returns its name."""
    to_rig = armature.matrix_world.inverted()
    parts = rig_parts(armature)
    ground = min((min((o.matrix_world @ Vector(c)).z for c in o.bound_box) for o in parts
                  if o.type == 'MESH'), default=0.0)
    flat = Vector((forward.x, forward.y, 0.0))
    flat = flat.normalized() if flat.length > 1e-6 else Vector((0.0, 1.0, 0.0))
    with edit_armature(context, armature) as edit_bones:
        root = ensure_root(edit_bones)
        body = edit_bones[body_bone]
        size = max(body.length, 0.25) * 1.5
        drive = edit_bones.get(DRIVE_BONE) or edit_bones.new(DRIVE_BONE)
        centre = armature.matrix_world @ body.head
        head = Vector((centre.x, centre.y, ground))
        drive.head = to_rig @ head
        drive.tail = to_rig @ (head + flat * size)
        drive.align_roll(to_rig.to_3x3() @ Vector((0.0, 0.0, 1.0)))
        drive.use_deform = False
        drive.parent = root
        if body.parent is None or body.parent.name == ROOT_BONE:
            body.parent = drive
            body.use_connect = False
    return DRIVE_BONE


def wheel_drivers():
    """[(id, data path, index, k, heading offset, (owner, bone), wheel offset in the
    body)] for every wheel made by wheel_spin that still has its driver."""
    found = []
    for owner in bpy.data.objects:
        rolls = owner.get(WHEEL_PROP)
        anim = owner.animation_data
        if not rolls or anim is None:
            continue
        for key, values in rolls.items():
            k, offset = values[0], values[1]
            local = Vector(values[6:9]) if len(values) >= 9 else Vector()
            path, index = key.rsplit("|", 1)
            fcurve = anim.drivers.find(path, index=int(index))
            if fcurve is None:
                continue
            target = fcurve.driver.variables["x"].targets[0]
            found.append((owner, path, int(index), k, offset,
                          (target.id, target.bone_target), local))
    return found


def bake_wheel_roll(context, frame_start, frame_end):
    """Key every Wheel Spin wheel frame by frame from the distance it really covers
    along the vehicle's heading: each wheel follows its own path, so turns, spins on the
    spot, reversing and loops roll right, the outside of a bend further than the inside.
    Then remove those drivers. Returns how many wheels were baked."""
    wheels = wheel_drivers()
    if not wheels:
        return 0
    scene = context.scene
    keep = scene.frame_current
    values = {i: [] for i in range(len(wheels))}
    last, rolled = {}, {}
    steps = 4  # sub-frames: a wheel's path is curved when the vehicle turns
    for frame in range(frame_start, frame_end + 1):
        for sub in range(steps if frame > frame_start else 1):
            subframe = (sub + 1) / steps if frame > frame_start else 0.0
            if subframe >= 1.0:
                scene.frame_set(frame)
            else:
                scene.frame_set(frame - 1, subframe=subframe) if frame > frame_start                     else scene.frame_set(frame)
            for i, (owner, path, index, k, offset, (body, bone), local) in enumerate(wheels):
                m = _world_matrix(body, bone)
                p = m @ local  # this wheel's own point, not the body's
                yaw = m.to_euler().z + offset
                forward = Vector((math.cos(yaw), math.sin(yaw), 0.0))
                if i not in last:
                    rolled[i] = owner.path_resolve(path)[index]  # start where the driver is
                else:
                    p0, f0 = last[i]
                    mid = (f0 + forward).normalized() if (f0 + forward).length > 1e-6 else forward
                    rolled[i] += (p - p0).dot(mid) * k
                last[i] = (p.copy(), forward)
        for i in range(len(wheels)):
            values[i].append((frame, rolled[i]))
    for i, (owner, path, index, *_rest) in enumerate(wheels):
        owner.driver_remove(path, index)
        for frame, value in values[i]:
            owner.path_resolve(path)[index] = value
            owner.keyframe_insert(path, index=index, frame=frame)
        rolls = dict(owner.get(WHEEL_PROP, {}))
        rolls.pop(f"{path}|{index}", None)
        owner[WHEEL_PROP] = rolls
    scene.frame_set(keep)
    return len(wheels)


def clear_drivers(objects, bones=None):
    """Remove every driver on `objects` and their data: gear links, wheel spin, chain
    slides, piston and IK leave none behind. With `bones`, only drivers on those pose
    bones of an armature go (the rest of the rig keeps its drivers).
    Returns how many were removed."""
    removed = 0
    for obj in objects:
        prefixes = None
        if bones is not None and obj.type == 'ARMATURE':
            prefixes = tuple(f'pose.bones["{b}"]' for b in bones)
        for owner in (obj, getattr(obj, "data", None)):
            anim = getattr(owner, "animation_data", None)
            if anim is None:
                continue
            for fcurve in list(anim.drivers):
                if prefixes is not None and not fcurve.data_path.startswith(prefixes):
                    continue
                anim.drivers.remove(fcurve)
                removed += 1
            if owner.get(WHEEL_PROP):
                rolls = {k: v for k, v in owner[WHEEL_PROP].items()
                         if anim.drivers.find(k.rsplit("|", 1)[0], index=int(k.rsplit("|", 1)[1]))}
                if rolls:
                    owner[WHEEL_PROP] = rolls
                else:
                    del owner[WHEEL_PROP]
    return removed


# --- Chains and tracks ---

def is_edge_loop(obj):
    """True for a mesh that is one run of edges (closed or open) with no faces: a track
    path drawn as geometry rather than a curve."""
    if obj is None or obj.type != 'MESH':
        return False
    mesh = obj.data
    if len(mesh.polygons) or not 2 <= len(mesh.edges) <= 20000:
        return False
    counts = {}
    for edge in mesh.edges:
        for v in edge.vertices:
            counts[v] = counts.get(v, 0) + 1
    if len(counts) != len(mesh.vertices) or any(c > 2 for c in counts.values()):
        return False
    return sum(1 for c in counts.values() if c == 1) in (0, 2)


def loop_length(obj):
    """World length of an edge-loop mesh's edges (a path not yet made into a curve)."""
    world = obj.matrix_world
    verts = obj.data.vertices
    return sum(((world @ verts[a].co) - (world @ verts[b].co)).length
               for a, b in (e.vertices for e in obj.data.edges))


def curve_from_loop(obj):
    """A poly curve object following the edge run of mesh `obj`, placed and parented
    like it (the mesh itself is hidden). Returns the curve object."""
    mesh = obj.data
    adjacent = {}
    for edge in mesh.edges:
        a, b = edge.vertices
        adjacent.setdefault(a, []).append(b)
        adjacent.setdefault(b, []).append(a)
    ends = [v for v, n in adjacent.items() if len(n) == 1]
    start = ends[0] if ends else min(adjacent)
    order, previous, current = [start], None, start
    while True:
        following = [n for n in adjacent[current] if n != previous]
        if not following:
            break
        previous, current = current, following[0]
        if current == start:
            break
        order.append(current)
    data = bpy.data.curves.new(f"{obj.name}_path", 'CURVE')
    data.dimensions = '3D'
    spline = data.splines.new('POLY')
    spline.points.add(len(order) - 1)
    for point, index in zip(spline.points, order):
        point.co = (*mesh.vertices[index].co, 1.0)
    spline.use_cyclic_u = not ends
    curve = bpy.data.objects.new(data.name, data)
    for coll in obj.users_collection:
        coll.objects.link(curve)
    curve.parent, curve.parent_type, curve.parent_bone = obj.parent, obj.parent_type, obj.parent_bone
    curve.matrix_parent_inverse = obj.matrix_parent_inverse.copy()
    curve.matrix_world = obj.matrix_world.copy()
    obj.hide_set(True)
    obj.hide_render = True
    return curve


def _hull_2d(points):
    """Convex hull of 2D (x, y) tuples, anticlockwise, no repeated end (monotone chain)."""
    pts = sorted(set(points))
    if len(pts) < 3:
        return pts
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 1e-12:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 1e-12:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _common_bone(parts):
    """(rig, 'BONE', bone) for the deepest bone every part's own bone hangs from (not
    counting the parts' own bones), or None when they aren't all on one rig."""
    targets = {bone_target(p) for p in parts}
    rigs = {rig for rig, _bone in targets}
    if len(rigs) != 1 or None in rigs or any(b is None for _r, b in targets):
        return None
    rig = rigs.pop()
    chains = []
    for _rig, name in targets:
        bone, chain = rig.data.bones[name].parent, []
        while bone is not None:
            chain.append(bone.name)
            bone = bone.parent
        chains.append(chain)
    shared = set(chains[0]).intersection(*chains[1:])
    for name in chains[0]:  # deepest first
        if name in shared:
            return rig, 'BONE', name
    return None


def path_around(wheels, offset=0.0, segments=64, name=None, axis='AUTO'):
    """A closed poly curve wrapped round `wheels` the way a chain or belt goes: arcs
    round the outside of each wheel joined by straight runs (the convex hull of the
    wheel circles; a wheel inside the loop is passed by, like a real chain skips an
    idler it doesn't touch). `offset` moves the path out from the rims (half the
    link's thickness puts links on the teeth, not in them).

    The wheels must turn about parallel axles. The curve lies in the plane through
    the wheels' middles, with its origin at their centre, and gets the wheels' shared
    parent (object or bone) when they all have one. Raises ValueError otherwise.
    """
    wheels = [w for w in wheels if w.type == 'MESH']
    if len(wheels) < 2:
        raise ValueError("Select two or more wheels (sprockets, pulleys, idlers)")
    rest = {w: rest_world(w) for w in wheels}  # where they were modelled, not posed
    axle = _matrix_axis(rest[wheels[0]], wheel_axle(wheels[0], axis))
    for wheel in wheels[1:]:
        if abs(_matrix_axis(rest[wheel], wheel_axle(wheel, axis)).dot(axle)) < 0.99:
            raise ValueError(f"'{wheel.name}' turns on a different axle from "
                             f"'{wheels[0].name}' (Axle {axis}): the wheels must be side "
                             f"by side, or set Axle to the wheels' own axis")
    u = axle.orthogonal().normalized()
    v = axle.cross(u).normalized()
    circles = []
    for wheel in wheels:
        corners = [rest[wheel] @ Vector(c) for c in wheel.bound_box]
        centre = sum(corners, Vector()) / 8.0
        radius = spin_radius(wheel, wheel_axle(wheel, axis)) + offset
        circles.append((centre, max(radius, 1e-4)))
    middle = sum((c for c, _r in circles), Vector()) / len(circles)
    flat = []
    for centre, radius in circles:
        cu, cv = (centre - middle).dot(u), (centre - middle).dot(v)
        for i in range(segments):
            a = math.tau * i / segments
            flat.append((round(cu + math.cos(a) * radius, 7), round(cv + math.sin(a) * radius, 7)))
    hull = _hull_2d(flat)
    frame = Matrix((u, v, axle)).transposed().to_4x4()
    frame.translation = middle
    data = bpy.data.curves.new(name or "WheelPath", 'CURVE')
    data.dimensions = '3D'
    spline = data.splines.new('POLY')
    spline.points.add(len(hull) - 1)
    for point, (x, y) in zip(spline.points, hull):
        point.co = (x, y, 0.0, 1.0)
    spline.use_cyclic_u = True
    curve = bpy.data.objects.new(data.name, data)
    for coll in wheels[0].users_collection:
        coll.objects.link(curve)
    curve.matrix_world = frame
    parents = {(w.parent, w.parent_type, w.parent_bone if w.parent_type == 'BONE' else "")
               for w in wheels}
    if len(parents) > 1:
        # Each wheel on its own (spinning) bone: ride on the deepest bone they all hang
        # from instead, so the path goes along with the vehicle without turning.
        parents = {_common_bone(wheels)} - {None}
    if len(parents) == 1:
        parent, kind, bone = parents.pop()
        if parent is not None:
            curve.parent, curve.parent_type = parent, kind
            if kind == 'BONE':
                curve.parent_bone = bone
            bpy.context.view_layer.update()
            set_rest_world(curve, frame)  # at the rig's rest, not its current pose
    return curve


SPROCKET_WORDS = ("sprocket", "drive", "cog", "pulley", "gear")


def chain_parts(meshes):
    """(link, sprocket, ignored) from the selected meshes. The smallest is the link.
    Of the rest, the sprocket is the one named like one, else the one already on a
    bone, else the largest; any others are left alone."""
    meshes = sorted(meshes, key=part_size)
    link, rest = meshes[0], meshes[1:]
    if not rest:
        return link, None, []
    named = [o for o in rest if any(w in o.name.lower() for w in SPROCKET_WORDS)]
    boned = [o for o in rest if bone_target(o)[0] is not None]
    sprocket = (named or boned or [rest[-1]])[0]
    return link, sprocket, [o for o in rest if o is not sprocket]


def link_pitch(link, axis='X'):
    """How much path one link takes: its bounding box along its local `axis`, in the
    scene (so the object's scale counts)."""
    return max(link.dimensions[AXES[axis[-1]]], 1e-4)


def fit_links(link, curve, axis='X'):
    """(count, spacing, stretch) for links of `link` round `curve`: the whole number of
    links nearest to filling it exactly, the gap that gives between link starts, and how
    far that is off the link's own size (spacing / pitch - 1: + opens small gaps,
    - squeezes them together)."""
    length = curve_length(curve)
    pitch = link_pitch(link, axis)
    count = max(int(round(length / pitch)), 2)
    spacing = length / count
    return count, spacing, spacing / pitch - 1.0


def curve_length(curve_obj):
    """World-space length of the curve's first spline."""
    spline = curve_obj.data.splines[0]
    scale = max(curve_obj.matrix_world.to_scale())
    return spline.calc_length() * scale


def chain_prop(curve_obj):
    """The armature property that slides the chain on `curve_obj` (one per track, so
    two tracks on one rig each have their own)."""
    return f"{CHAIN_PROP}_{curve_obj.name}"


def chain_of(obj):
    """(rig, path curve) when `obj` is a chain made by Chain Along Curve or the path one
    runs on, else None."""
    rig = chain_rig(obj)
    holder = chain_holder(obj)
    if rig is not None and rig is holder:  # built without bones
        for link in chain_links(holder):
            for con in link.constraints:
                if con.type == 'FOLLOW_PATH' and con.target is not None:
                    return rig, con.target
        return None
    if rig is not None and holder is not None:
        names = {o.parent_bone for o in chain_links(holder)}
    elif rig is not None:
        names = {g.name for g in obj.vertex_groups if LINK_BONE.search(g.name)}
    if rig is not None:
        for name in names:
            pb = rig.pose.bones.get(name)
            for con in pb.constraints if pb else ():
                if con.type == 'FOLLOW_PATH' and con.target is not None:
                    return rig, con.target
        return None
    if obj is not None and obj.type == 'CURVE':
        for rig in bpy.data.objects:
            if chain_prop(obj) in rig.keys():
                return rig, obj
    return None


def _chain_source(rig, curve):
    """(id, data path) of the rotation that drives the chain's slide, or None."""
    anim = rig.animation_data
    for fc in anim.drivers if anim else ():
        if fc.data_path == f'["{chain_prop(curve)}"]':
            for var in fc.driver.variables:
                t = var.targets[0]
                if t.id is not None:
                    return t.id, t.data_path
    return None


def drive_chain_by(rig, curve, sprocket, axis='AUTO'):
    """Make `sprocket`'s turning slide the chain on `curve` (rim speed = chain speed),
    replacing whatever drove it. A sprocket that was following the chain stops, so
    there's no loop."""
    axis = wheel_axle(sprocket, axis)
    rest_to_delta(sprocket, AXES[axis[-1]])
    src_id, src_prefix, src_struct = _rotation_owner(sprocket)
    if src_struct.rotation_mode in {'QUATERNION', 'AXIS_ANGLE'}:
        src_struct.rotation_mode = 'XYZ'
    spin_index, spin_sign = channel_axis(sprocket, axis)
    anim = src_id.animation_data
    for fc in list(anim.drivers) if anim else []:
        if fc.data_path == f"{src_prefix}rotation_euler" and any(
                v.targets[0].id == rig and chain_prop(curve) in v.targets[0].data_path
                for v in fc.driver.variables):
            anim.drivers.remove(fc)
    bpy.context.view_layer.update()
    spin_sign *= _winding(curve, sprocket.matrix_world.translation, object_axis(sprocket, axis))
    prop = chain_prop(curve)
    if prop not in rig:
        rig[prop] = 0.0
    rig.driver_remove(f'["{prop}"]')
    drv = rig.driver_add(f'["{prop}"]').driver
    drv.type = 'SCRIPTED'
    var = drv.variables.new()
    var.name = "spin"
    var.type = 'SINGLE_PROP'
    var.targets[0].id = src_id
    var.targets[0].data_path = f"{src_prefix}rotation_euler[{spin_index}]"
    drv.expression = f"spin * {spin_sign * spin_radius(sprocket, axis) / curve_length(curve):.6g}"


def drive_from_chain(rig, curve, wheel, axis='AUTO'):
    """Make `wheel` turn with the chain on `curve`: by the length of chain that passes
    over it divided by its radius, the way round the chain wraps it. Whatever moves the
    chain (its sprocket, keys on the slide) turns the wheel. Returns the turn per metre
    of chain (signed)."""
    axis = wheel_axle(wheel, axis)
    src = _chain_source(rig, curve)
    dst_id, dst_prefix, dst_struct = _rotation_owner(wheel)
    index, channel_sign = channel_axis(wheel, axis)
    path = f"{dst_prefix}rotation_euler"
    if src is not None and src[0] == dst_id and src[1].startswith(path):
        raise ValueError(f"'{wheel.name}' drives the chain already")
    rest_to_delta(wheel, AXES[axis[-1]])
    if dst_struct.rotation_mode in {'QUATERNION', 'AXIS_ANGLE'}:
        dst_struct.rotation_mode = 'XYZ'
    bpy.context.view_layer.update()
    radius = spin_radius(wheel, axis)
    wind = _winding(curve, wheel.matrix_world.translation, object_axis(wheel, axis))
    per_metre = channel_sign * wind / radius
    k = per_metre * curve_length(curve)
    dst_id.driver_remove(path, index)
    drv = dst_id.driver_add(path, index).driver
    drv.type = 'SCRIPTED'
    var = drv.variables.new()
    var.name = "slide"
    var.type = 'SINGLE_PROP'
    var.targets[0].id = rig
    var.targets[0].data_path = f'["{chain_prop(curve)}"]'
    drv.expression = f"slide * {k:.6g}"
    return per_metre


def _winding(curve_obj, centre, axis):
    """1 if the curve runs the way a positive turn about `axis` (at `centre`) moves a
    belt, -1 if it runs the other way."""
    spline = curve_obj.data.splines[0]
    if spline.type == 'BEZIER':
        local = [p.co for p in spline.bezier_points]
    else:
        local = [p.co.xyz for p in spline.points]
    pts = [curve_obj.matrix_world @ Vector(p) for p in local]
    total = sum(((a - centre).cross(b - a)).dot(axis) for a, b in zip(pts, pts[1:] + pts[:1]))
    return 1.0 if total >= 0 else -1.0


def chain_along_curve(context, link, curve, armature=None, axis='X', count=0, sprocket=None,
                      roll=0.0, instances=False, keep_link=False, bones=True):
    """A chain or track: copies of `link` spaced along `curve`, one bone each.

    Every link gets a bone with a Follow Path constraint at its own place along the
    curve, so the result is an ordinary skinned mesh that exports to game engines (bake
    it with Make Game Ready). The armature's "chain_offset_<curve>" property (0-1 of
    the curve; see chain_prop) slides the links along; with a `sprocket`, its rotation drives it instead. `axis` is
    the link's length axis; `count` 0 fits as many links as the curve holds. `roll`
    (radians) turns each link about that axis, to stand it up or lay it flat on the path.
    With `instances`, each link is its own object sharing the link's mesh, parented to
    its bone (edit the link to change them all), gathered under an empty instead.
    `keep_link` leaves the link itself showing where it is (to edit it there); otherwise
    it's hidden.
    Without `bones` there is no armature: the links are instances that each follow the
    path themselves, and the slide property is on the chain's empty.
    Returns (armature or None, chain mesh object or empty, link count).
    """
    index = AXES[axis[-1]]
    length = curve_length(curve)
    pitch = link_pitch(link, axis)
    count = count or fit_links(link, curve, axis)[0]
    if not bones:
        return _chain_without_bones(link, curve, axis, count, sprocket, roll, keep_link)
    if armature is None:
        armature = new_armature(context, f"{link.name}_Chain_Rig", parts_base([curve]))
    curve.data.use_path = True

    # The links are built at the world origin, length along +Y (the path's forward
    # axis) and up along +Z, which is what Follow Path expects. Once the constraints
    # place them, each link's rest is moved to where it sits on the track (_rest_on_path).
    # A second chain from the same link (the other track) is named after its curve.
    prefix = link.name
    if f"{prefix}_chain" in bpy.data.objects or f"{prefix}_link_000" in armature.data.bones:
        prefix = f"{link.name}_{curve.name}"
    mesh = None if instances else bpy.data.meshes.new(f"{prefix}_chain")
    chain = bpy.data.objects.new(f"{prefix}_chain", mesh)
    if instances:
        chain.empty_display_type = 'PLAIN_AXES'
        chain.empty_display_size = pitch
        chain[CHAIN_TAG] = chain
    for coll in link.users_collection:
        coll.objects.link(chain)
    chain.matrix_world = Matrix.Identity(4)
    to_forward = {0: Matrix.Rotation(math.pi / 2, 4, 'Z'), 1: Matrix.Identity(4),
                  2: Matrix.Rotation(-math.pi / 2, 4, 'X')}[index]
    rot = (Matrix.Rotation(roll, 4, 'Y') @ to_forward
           @ Matrix.Diagonal(link.matrix_world.to_scale()).to_4x4())
    bm = bmesh.new()
    groups = []
    for i in range(0 if instances else count):
        part = bmesh.new()
        part.from_mesh(link.data)
        bmesh.ops.transform(part, matrix=rot, verts=part.verts)
        offset = len(bm.verts)
        tmp = bpy.data.meshes.new("tmp")
        part.to_mesh(tmp)
        bm.from_mesh(tmp)
        bpy.data.meshes.remove(tmp)
        part.free()
        groups.append(range(offset, len(bm.verts)))
    if mesh is not None:
        bm.to_mesh(mesh)
        for mat in link.data.materials:
            mesh.materials.append(mat)
    bm.free()

    to_rig = armature.matrix_world.inverted()
    head = to_rig @ Vector((0.0, 0.0, 0.0))
    tail = to_rig @ Vector((0.0, pitch * 0.5, 0.0))
    up = to_rig.to_3x3() @ Vector((0.0, 0.0, 1.0))
    names = []
    with edit_armature(context, armature) as edit_bones:
        root = edit_bones.get(ROOT_BONE)
        for i in range(count):
            bone = edit_bones.new(f"{prefix}_link_{i:03d}")
            bone.head, bone.tail = head, tail
            bone.align_roll(up)
            bone.parent = root
            names.append(bone.name)

    prop = chain_prop(curve)
    if prop not in armature:
        armature[prop] = 0.0
    for i, name in enumerate(names):
        pose_bone = armature.pose.bones[name]
        # Follow Path adds the bone's own world transform on top of the path's, and the
        # path already rides with the machine: pin the link to the world origin first,
        # or moving the root or the rig object would move the links twice.
        for kind, label in (('LIMIT_ROTATION', "Chain Pin Rotation"),
                            ('LIMIT_LOCATION', "Chain Pin Location")):
            pin = pose_bone.constraints.new(kind)
            pin.name = label
            pin.owner_space = 'WORLD'
            for key in "xyz":
                if kind == 'LIMIT_ROTATION':
                    setattr(pin, f"use_limit_{key}", True)
                else:
                    setattr(pin, f"use_min_{key}", True)
                    setattr(pin, f"use_max_{key}", True)
        con = pose_bone.constraints.new('FOLLOW_PATH')
        con.target = curve
        con.use_fixed_location = True
        con.use_curve_follow = True
        con.forward_axis = 'FORWARD_Y'
        con.up_axis = 'UP_Z'
        fcurve = con.driver_add("offset_factor")
        drv = fcurve.driver
        drv.type = 'SCRIPTED'
        var = drv.variables.new()
        var.name = "slide"
        var.type = 'SINGLE_PROP'
        var.targets[0].id = armature
        var.targets[0].data_path = f'["{prop}"]'
        # x - floor(x) rather than x % 1: '%' isn't a "simple expression", so Blender
        # would run it through Python (slow, and sandboxed when Auto Run is off).
        at = f"({i / count:.6f} + slide)"
        drv.expression = f"{at} - floor{at}"

    if sprocket is not None:
        drive_chain_by(armature, curve, sprocket)

    for name, verts in zip(names, groups):
        chain.vertex_groups.new(name=name).add(list(verts), 1.0, 'REPLACE')
    _rest_on_path(context, armature, None if instances else chain, names, groups)
    if instances:
        for name in names:
            inst = bpy.data.objects.new(name, link.data)
            for coll in link.users_collection:
                coll.objects.link(inst)
            inst.parent, inst.parent_type, inst.parent_bone = armature, 'BONE', name
            set_rest_world(inst, armature.matrix_world @ armature.data.bones[name].matrix_local @ rot)
            inst[CHAIN_TAG] = chain
    else:
        armature_modifier(chain, armature)
    _keep_world(chain, lambda: setattr(chain, "parent", armature))
    chain[CHAIN_ROLL] = math.degrees(roll) % 360.0
    chain[CHAIN_LINK], chain[CHAIN_AXIS] = link, axis
    if not keep_link:
        link.hide_set(True)
        link.hide_render = True
    return armature, chain, count


def _slide_driver(constraint, owner, prop, place):
    """Drive a Follow Path constraint's offset from the chain's slide property, wrapped
    to 0-1, starting at `place` (0-1 round the path)."""
    drv = constraint.driver_add("offset_factor").driver
    drv.type = 'SCRIPTED'
    var = drv.variables.new()
    var.name = "slide"
    var.type = 'SINGLE_PROP'
    var.targets[0].id = owner
    var.targets[0].data_path = f'["{prop}"]'
    at = f"({place:.6f} + slide)"
    drv.expression = f"{at} - floor{at}"


def _chain_without_bones(link, curve, axis, count, sprocket, roll, keep_link, holder=None):
    """chain_along_curve with no armature: see there. With `holder` (an existing chain's
    empty, its links already removed) the links are built under it again, so its slide
    and whatever drives that are kept."""
    index = AXES[axis[-1]]
    curve.data.use_path = True
    prop = chain_prop(curve)
    if holder is None:
        prefix = link.name
        if f"{prefix}_chain" in bpy.data.objects:
            prefix = f"{link.name}_{curve.name}"
        holder = bpy.data.objects.new(f"{prefix}_chain", None)
        holder.empty_display_type = 'PLAIN_AXES'
        holder.empty_display_size = link_pitch(link, axis)
        for coll in link.users_collection:
            coll.objects.link(holder)
        holder.parent = curve  # rides with the path; the links place themselves on it
        holder[CHAIN_TAG] = holder
        holder[prop] = 0.0
    else:
        prefix = holder.name[:-len("_chain")] if holder.name.endswith("_chain") else holder.name
    to_forward = {0: Matrix.Rotation(math.pi / 2, 4, 'Z'), 1: Matrix.Identity(4),
                  2: Matrix.Rotation(-math.pi / 2, 4, 'X')}[index]
    # Follow Path puts the link's own transform on top of its place on the path, so the
    # link's own matrix is how it sits there: length along +Y, rolled, at its scale.
    rot = (Matrix.Rotation(roll, 4, 'Y') @ to_forward
           @ Matrix.Diagonal(link.matrix_world.to_scale()).to_4x4())
    for i in range(count):
        inst = bpy.data.objects.new(f"{prefix}_link_{i:03d}", link.data)
        for coll in link.users_collection:
            coll.objects.link(inst)
        inst.matrix_basis = rot
        inst[CHAIN_TAG] = holder
        con = inst.constraints.new('FOLLOW_PATH')
        con.target = curve
        con.use_fixed_location = True
        con.use_curve_follow = True
        con.forward_axis = 'FORWARD_Y'
        con.up_axis = 'UP_Z'
        _slide_driver(con, holder, prop, i / count)
    if sprocket is not None:
        drive_chain_by(holder, curve, sprocket)
    holder[CHAIN_ROLL] = math.degrees(roll) % 360.0
    holder[CHAIN_LINK], holder[CHAIN_AXIS] = link, axis
    if not keep_link:
        link.hide_set(True)
        link.hide_render = True
    return None, holder, count


LINK_BONE = re.compile(r"_link_\d{3}$")


def chain_holder(obj):
    """The empty an instanced chain's links gather under, for the empty or any link."""
    holder = obj.get(CHAIN_TAG) if obj is not None else None
    return holder if isinstance(holder, bpy.types.Object) else None


def chain_links(holder):
    """An instanced chain's link objects, in order."""
    return sorted((o for o in bpy.data.objects if o is not holder and chain_holder(o) is holder),
                  key=lambda o: (o.parent_bone, o.name))


def chain_rig(obj):
    """What holds the slide of a chain made by chain_along_curve: the armature it rides
    on (given the chain mesh, or an instanced chain's empty or links), or the empty
    itself for a chain built without bones. None if `obj` isn't a chain."""
    holder = chain_holder(obj)
    if holder is not None:
        if any(k.startswith(CHAIN_PROP) for k in holder.keys()):
            return holder  # built without bones
        rig = holder.parent
        return rig if rig is not None and rig.type == 'ARMATURE' else None
    if obj is None or obj.type != 'MESH':
        return None
    for mod in obj.modifiers:
        rig = getattr(mod, "object", None)
        if mod.type == 'ARMATURE' and rig is not None and any(
                LINK_BONE.search(g.name) and g.name in rig.data.bones for g in obj.vertex_groups):
            return rig
    return None


def roll_chain(chain, angle):
    """Turn every link of `chain` by `angle` (radians) about the path, each about its
    own bone's length: the same as building it with that much more roll."""
    rig = chain_rig(chain)
    if rig is None:
        raise ValueError(f"'{chain.name}' isn't a chain made by Chain Along Curve")
    holder = chain_holder(chain)
    if holder is not None:
        links = chain_links(holder)
        turn = Matrix.Rotation(angle, 4, 'Y')  # about the bone's length, through its tail
        for link in links:
            link.matrix_basis = turn @ link.matrix_basis
        holder[CHAIN_ROLL] = (holder.get(CHAIN_ROLL, 0.0) + math.degrees(angle)) % 360.0
        return len(links)
    to_chain = chain.matrix_world.inverted() @ rig.matrix_world
    turn = Matrix.Rotation(angle, 4, 'Y')
    moves = {}
    for g in chain.vertex_groups:
        bone = rig.data.bones.get(g.name)
        if bone is not None and LINK_BONE.search(g.name):
            frame = to_chain @ bone.matrix_local
            moves[g.index] = frame @ turn @ frame.inverted()
    for v in chain.data.vertices:
        for g in v.groups:
            if g.group in moves and g.weight > 0.5:
                v.co = moves[g.group] @ v.co
                break
    chain.data.update()
    chain[CHAIN_ROLL] = (chain.get(CHAIN_ROLL, 0.0) + math.degrees(angle)) % 360.0
    return len(moves)


def _rest_on_path(context, armature, chain, names, groups):
    """Give each link bone (and its piece of the chain mesh) a rest pose where Follow
    Path puts it now, instead of all of them stacked at the world origin. The links are
    pinned to the origin before Follow Path, so their rest doesn't change how they move;
    it only makes Edit Mode readable and gives engines a sensible bind pose."""
    context.view_layer.update()
    deps = context.evaluated_depsgraph_get()
    posed = armature.evaluated_get(deps).pose.bones
    places = {n: armature.matrix_world @ posed[n].matrix for n in names}
    to_rig = armature.matrix_world.inverted()
    with edit_armature(context, armature) as edit_bones:
        for name in names:
            loc, rot, _scale = places[name].decompose()
            bone = edit_bones[name]
            length = bone.length
            axes = rot.to_matrix()
            bone.head = to_rig @ loc
            bone.tail = bone.head + (to_rig.to_3x3() @ axes.col[1]).normalized() * length
            bone.align_roll(to_rig.to_3x3() @ axes.col[2])
    if chain is None:  # instanced links: placed on their bones afterwards
        return
    mesh = chain.data
    to_chain = chain.matrix_world.inverted()
    for name, verts in zip(names, groups):
        move = to_chain @ places[name] @ chain.matrix_world
        for index in verts:
            mesh.vertices[index].co = move @ mesh.vertices[index].co
    mesh.update()


# --- Controls ---

COLLECTIONS = {  # name -> bone colour palette
    "Controls": 'THEME04',
    "Mechanism": 'THEME01',
    "Deform": 'DEFAULT',
}
LIMIT_CONSTRAINTS = {'LIMIT_ROTATION', 'LIMIT_LOCATION', 'LIMIT_SCALE', 'LIMIT_DISTANCE'}
WIDGET_RING = "WGT_PF_Ring"
WIDGET_ROOT = "WGT_PF_Root"


def _widget(name, points, edges=None):
    """A wire shape object (not in any scene) for custom bone shapes. Points without
    `edges` are joined in a closed loop."""
    obj = bpy.data.objects.get(name)
    if obj is not None:
        return obj
    mesh = bpy.data.meshes.new(name)
    count = len(points)
    if edges is None:
        edges = [(i, (i + 1) % count) for i in range(count)]
    mesh.from_pydata(points, edges, [])
    obj = bpy.data.objects.new(name, mesh)
    obj.use_fake_user = True
    return obj


def _loop(n, radius=1.0, start=0.0, sweep=math.tau, plane="XZ"):
    pts = []
    for i in range(n):
        a = start + sweep * i / (n - 1 if sweep < math.tau else n)
        c, s = math.cos(a) * radius, math.sin(a) * radius
        pts.append({"XZ": (c, 0.0, s), "XY": (c, s, 0.0), "YZ": (0.0, c, s)}[plane])
    return pts


def _shape_ring():
    return _loop(32), None


def _shape_square():
    return [(-1.0, 0.0, -1.0), (1.0, 0.0, -1.0), (1.0, 0.0, 1.0), (-1.0, 0.0, 1.0)], None


def _shape_diamond():
    return [(1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (-1.0, 0.0, 0.0), (0.0, 0.0, -1.0)], None


def _shape_rotate():
    """Three quarters of a ring with an arrowhead: turns about the axis."""
    arc = _loop(24, 1.0, 0.0, math.tau * 0.75)
    end = arc[-1]
    head = [(end[0] + 0.25, 0.0, end[2] + 0.12), (end[0] + 0.25, 0.0, end[2] - 0.12)]
    pts = arc + head
    n = len(arc)
    edges = [(i, i + 1) for i in range(n - 1)] + [(n - 1, n), (n - 1, n + 1)]
    return pts, edges


def _shape_arrows():
    """A four-way move arrow lying in the shape's plane."""
    pts, edges = [], []
    for c, s in ((1, 0), (0, 1), (-1, 0), (0, -1)):
        base = len(pts)
        tip = (c * 1.0, 0.0, s * 1.0)
        pts += [(0.0, 0.0, 0.0), tip,
                (c * 0.75 - s * 0.15, 0.0, s * 0.75 + c * 0.15),
                (c * 0.75 + s * 0.15, 0.0, s * 0.75 - c * 0.15)]
        edges += [(base, base + 1), (base + 1, base + 2), (base + 1, base + 3)]
    return pts, edges


def _shape_cube():
    pts = [(x, y, z) for x in (-1.0, 1.0) for y in (-1.0, 1.0) for z in (-1.0, 1.0)]
    edges = [(a, b) for a in range(8) for b in range(a + 1, 8)
             if sum(pts[a][i] != pts[b][i] for i in range(3)) == 1]
    return pts, edges


def _shape_sphere():
    pts, edges = [], []
    for plane in ("XZ", "XY", "YZ"):
        base = len(pts)
        ring = _loop(24, plane=plane)
        pts += ring
        edges += [(base + i, base + (i + 1) % 24) for i in range(24)]
    return pts, edges


def _shape_cross():
    pts = [(-1.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, -1.0), (0.0, 0.0, 1.0)]
    return pts, [(0, 1), (2, 3)]


# id -> (label, description, builder). Planar shapes lie across their axis: the axis
# they face along is the one you pick (Y is the bone's length).
CONTROL_SHAPES = {
    'RING': ("Ring", "A circle around the axis: turn controls", _shape_ring),
    'ROTATE': ("Rotate", "Three-quarter ring with an arrow: one hinge", _shape_rotate),
    'SQUARE': ("Square", "A square across the axis: roots and bodies", _shape_square),
    'ARROWS': ("Arrows", "Four-way arrows: move controls such as Drive", _shape_arrows),
    'DIAMOND': ("Diamond", "A diamond across the axis: poles and small controls", _shape_diamond),
    'CROSS': ("Cross", "A plus sign: minimal markers", _shape_cross),
    'CUBE': ("Cube", "A wire cube: IK targets", _shape_cube),
    'SPHERE': ("Sphere", "Three rings: free-rotating controls", _shape_sphere),
}
SHAPE_ITEMS = [(k, v[0], v[1]) for k, v in CONTROL_SHAPES.items()]
CUSTOM_SHAPE_PROP = "preflight_shape_set"  # on a pose bone: Set Up Controls leaves it be


def control_widget(shape, axis='Y', scale=1.0, line=False):
    """The widget object for `shape`. With `line`, it also draws the bone itself (a
    custom shape replaces the bone's own drawing): a stick from head to tail, worked
    back through the shape's `axis` rotation and `scale` so it lands on the bone."""
    pts, edges = CONTROL_SHAPES[shape][2]()
    if not line:
        return _widget(f"WGT_PF_{shape.title()}", pts, edges)
    reach = 1.0 / max(scale, 1e-6)
    name = f"WGT_PF_{shape.title()}_Bone{axis}{reach:.4g}"
    if bpy.data.objects.get(name) is None:
        if edges is None:
            edges = [(i, (i + 1) % len(pts)) for i in range(len(pts))]
        tail = Euler(_FACE[axis]).to_matrix().inverted() @ Vector((0.0, reach, 0.0))
        n = len(pts)
        pts = list(pts) + [(0.0, 0.0, 0.0), tuple(tail)]
        edges = list(edges) + [(n, n + 1)]
    return _widget(name, pts, edges)


def is_control_widget(obj, shape):
    """True when `obj` is a Pre-Flight widget of `shape` (with or without the bone line)."""
    name = f"WGT_PF_{shape.title()}"
    return obj is not None and (obj.name == name or obj.name.startswith(name + "_Bone"))


def widgets():
    """(ring, square) widgets, kept for older callers."""
    return control_widget('RING'), control_widget('SQUARE')


def _axis_toward(armature, pose_bone, direction):
    """The bone's local axis ('X', 'Y' or 'Z') pointing most along world `direction`."""
    rest = (armature.matrix_world @ pose_bone.bone.matrix_local).to_3x3()
    return "XYZ"[max(range(3), key=lambda i: abs(rest.col[i].normalized().dot(direction)))]


def hinge_axis(pose_bone):
    """The one rotation axis a hinge-limited bone may turn about, else None."""
    if not any(c.type == 'LIMIT_ROTATION' for c in pose_bone.constraints):
        return None
    free = [k for i, k in enumerate("XYZ") if not pose_bone.lock_rotation[i]]
    return free[0] if len(free) == 1 else None


def _bone_part(armature, bone):
    """The object bound to `bone` (parented, skinned or Keep Hierarchy), else None."""
    for obj in rig_parts(armature):
        if bone_target(obj) == (armature, bone):
            return obj
    return None


def part_extent(armature, pose_bone, axis):
    """(reach, radius): how far the part `pose_bone` moves reaches along the bone's
    local `axis` from the bone's head, and how far it spreads around that axis. None
    when no part is bound to the bone."""
    obj = _bone_part(armature, pose_bone.name)
    if obj is None:
        return None
    if follows_pose(obj, armature):
        frame = armature.matrix_world @ pose_bone.matrix
    else:
        frame = armature.matrix_world @ pose_bone.bone.matrix_local
    to_bone = frame.inverted() @ obj.matrix_world
    i = AXES[axis]
    a, b = (j for j in range(3) if j != i)
    pts = [to_bone @ Vector(c) for c in obj.bound_box]
    return (max(abs(p[i]) for p in pts),
            max(math.hypot(p[a], p[b]) for p in pts))


_AROUND_BONE = {'SPHERE', 'CUBE', 'CROSS', 'DIAMOND'}  # sized to the part's thickness


def control_size(armature, pose_bone, shape, axis):
    """Auto size (radius) for `shape` facing `axis`: just clear of the part around that
    axis (a sphere, cube, cross or diamond: around the bone). Falls back on the bone."""
    around = 'Y' if shape in _AROUND_BONE else axis
    extent = part_extent(armature, pose_bone, around)
    if extent is None or extent[1] < 1e-4:
        return max(pose_bone.bone.length * 0.3, 0.01)
    return max(extent[1] * 1.25, 0.01)


def auto_control(armature, pose_bone, role=None):
    """(shape, axis, size) Set Up Controls gives a bone: a hinge gets a Rotate shape
    about its hinge; a long link a ring around its length; anything else lies flat
    (across whichever of its axes points most up). Each is sized just clear of its part."""
    role = role or bone_role(armature, pose_bone, _driven_bones(armature))
    up = Vector((0.0, 0.0, 1.0))
    flat = _axis_toward(armature, pose_bone, up)
    radius = control_size(armature, pose_bone, 'RING', flat)
    name = pose_bone.name
    if role == 'ROOT':
        size = max((part_size(o) for o in rig_parts(armature)), default=1.0) * 0.6
        return 'SQUARE', flat, max(size, pose_bone.bone.length)
    if name == DRIVE_BONE:
        return 'ARROWS', flat, max(radius, pose_bone.bone.length * 0.6)
    if name.startswith(IK_PREFIX):
        return 'CUBE', 'Y', max(pose_bone.bone.length * 0.5, 0.05)
    if name.startswith(POLE_PREFIX):
        return 'DIAMOND', _axis_toward(armature, pose_bone, up), max(pose_bone.bone.length * 0.5, 0.05)
    hinge = hinge_axis(pose_bone)
    along = part_extent(armature, pose_bone, 'Y')
    long_link = along is not None and along[0] > 2.5 * along[1]
    if hinge is not None:
        # A link swinging on a hinge: the arrow hugs the link, not its whole sweep.
        around = 'Y' if long_link else hinge
        return 'ROTATE', hinge, control_size(armature, pose_bone, 'ROTATE', around)
    if long_link:
        return 'RING', 'Y', control_size(armature, pose_bone, 'RING', 'Y')
    return 'RING', flat, radius


# Rotation (Euler) that turns a shape facing +Y to face the bone's X, Y or Z.
_FACE = {'X': (0.0, 0.0, -math.pi / 2), 'Y': (0.0, 0.0, 0.0), 'Z': (math.pi / 2, 0.0, 0.0)}


def set_control(pose_bone, shape, axis, size, custom=False, line=True):
    """Give `pose_bone` the control `shape` facing along its local `axis`, `size`
    (radius) in world units. `line` also draws the bone, which a custom shape would
    otherwise hide. `custom` marks it as hand-set, so Set Up Controls keeps it."""
    scale = max(size, 1e-4) / max(pose_bone.bone.length, 1e-6)
    pose_bone.custom_shape = control_widget(shape, axis, scale, line)
    pose_bone.use_custom_shape_bone_size = True
    pose_bone.custom_shape_scale_xyz = (scale, scale, scale)
    pose_bone.custom_shape_rotation_euler = _FACE[axis]
    pose_bone.custom_shape_translation = (0.0, 0.0, 0.0)
    if custom:
        pose_bone[CUSTOM_SHAPE_PROP] = True
    elif CUSTOM_SHAPE_PROP in pose_bone:
        del pose_bone[CUSTOM_SHAPE_PROP]


def _driven_bones(armature):
    names = set()
    if armature.animation_data is not None:
        for fcurve in armature.animation_data.drivers:
            match = re.match(r'pose\.bones\["(.+?)"\]', fcurve.data_path)
            if match:
                names.add(match.group(1))
    return names


def bone_role(armature, pose_bone, driven):
    """'ROOT', 'MECHANISM' (moved by constraints or drivers, or a helper) or 'CONTROL'."""
    if pose_bone.name == ROOT_BONE:
        return 'ROOT'
    if pose_bone.name.startswith((IK_PREFIX, POLE_PREFIX)) or pose_bone.name == DRIVE_BONE:
        return 'IK'
    moved = any(c.type not in LIMIT_CONSTRAINTS for c in pose_bone.constraints)
    if moved or pose_bone.name in driven or not pose_bone.bone.use_deform:
        return 'MECHANISM'
    return 'CONTROL'


def setup_controls(armature, line=True):
    """Sort the bones into coloured Controls / Mechanism / Deform collections and give
    each control a shape (auto_control: flat on the ground, or turning about its hinge)
    sized to its part. Shapes set by hand with set_control are kept.
    Returns {collection: bone count}."""
    data = armature.data
    collections = {}
    for name, palette in COLLECTIONS.items():
        collections[name] = data.collections.get(name) or data.collections.new(name)
    driven = _driven_bones(armature) | ik_chained(armature)
    counts = dict.fromkeys(COLLECTIONS, 0)
    for pose_bone in armature.pose.bones:
        bone = pose_bone.bone
        for coll in collections.values():
            coll.unassign(bone)
        role = bone_role(armature, pose_bone, driven)
        group = "Mechanism" if role == 'MECHANISM' else "Controls"
        collections[group].assign(bone)
        counts[group] += 1
        if bone.use_deform:
            collections["Deform"].assign(bone)
            counts["Deform"] += 1
        bone.color.palette = COLLECTIONS[group]
        if role != 'MECHANISM' and not pose_bone.get(CUSTOM_SHAPE_PROP):
            set_control(pose_bone, *auto_control(armature, pose_bone, role), line=line)
    data.display_type = 'OCTAHEDRAL'
    return counts


# --- Game export ---

def root_bones(armature):
    return [b for b in armature.data.bones if b.parent is None]


def add_root(context, armature):
    """Parent every top-level bone under one root bone at the armature origin."""
    with edit_armature(context, armature) as edit_bones:
        tops = [b for b in edit_bones if b.parent is None and b.name != ROOT_BONE]
        size = max((b.length for b in edit_bones), default=1.0)
        root = ensure_root(edit_bones, size)
        root.use_deform = False
        for bone in tops:
            bone.parent = root
            bone.use_connect = False


def safe_rig_name(armature):
    """A name engines import cleanly when the rig is called Armature or Root."""
    stem = re.sub(r"\.\d+$", "", armature.name)
    if stem.lower() not in ENGINE_UNSAFE_NAMES:
        return armature.name
    parts = [o for o in armature.children if o.type == 'MESH']
    return rig_name(parts) if parts else "Mech_Rig"


def bone_parented_meshes(armature):
    """Meshes that follow a bone without skinning: bone parents and Keep Hierarchy."""
    found = [o for o in armature.children
             if o.type == 'MESH' and o.parent_type == 'BONE' and o.parent_bone]
    found += [o for o in bpy.data.objects if o.type == 'MESH' and follow_bone(o)[0] == armature]
    return found


def skinned_meshes(armature):
    return [o for o in bpy.data.objects if o.type == 'MESH'
            and any(m.type == 'ARMATURE' and m.object == armature for m in o.modifiers)]


def weight_problems(obj, armature):
    """(unweighted vertex count, vertex count with weight split over several bones)."""
    bones = {g.index for g in obj.vertex_groups if g.name in armature.data.bones}
    unweighted = split = 0
    for vert in obj.data.vertices:
        used = sum(1 for g in vert.groups if g.group in bones and g.weight > 1e-4)
        if used == 0:
            unweighted += 1
        elif used > 1:
            split += 1
    return unweighted, split


def make_rigid(obj, armature):
    """Give every vertex of `obj` full weight to its strongest bone. Returns verts changed."""
    bones = {g.index: g for g in obj.vertex_groups if g.name in armature.data.bones}
    changed = 0
    removals = {}
    for vert in obj.data.vertices:
        weights = [(g.weight, g.group) for g in vert.groups if g.group in bones]
        if not weights:
            continue
        best = max(weights)[1]
        if len(weights) > 1 or max(weights)[0] < 1.0 - 1e-6:
            changed += 1
        for _, group in weights:
            if group != best:
                removals.setdefault(group, []).append(vert.index)
        bones[best].add([vert.index], 1.0, 'REPLACE')
    for group, verts in removals.items():
        bones[group].remove(verts)
    return changed


def has_rig_motion(armature):
    """Constraints or drivers on the pose: motion an engine won't see until baked."""
    constrained = any(pb.constraints for pb in armature.pose.bones)
    driven = bool(armature.animation_data and armature.animation_data.drivers)
    return constrained or driven


def bake_motion(context, armature, frame_start, frame_end):
    """Bake the pose (constraints and drivers) to keyframes, then remove them."""
    view_layer = context.view_layer
    previous = view_layer.objects.active
    for obj in context.selected_objects:
        obj.select_set(False)
    armature.select_set(True)
    view_layer.objects.active = armature
    bpy.ops.object.mode_set(mode='POSE')
    for pose_bone in armature.pose.bones:
        if hasattr(pose_bone, "select"):  # Blender 5: selection lives on the pose bone
            pose_bone.select = True
        else:
            pose_bone.bone.select = True
    bpy.ops.nla.bake(frame_start=frame_start, frame_end=frame_end, only_selected=False,
                     visual_keying=True, clear_constraints=True, bake_types={'POSE'})
    bpy.ops.object.mode_set(mode='OBJECT')
    if armature.animation_data is not None:  # baked: drivers would fight the keys
        for fcurve in list(armature.animation_data.drivers):
            armature.animation_data.drivers.remove(fcurve)
    if previous is not None and previous.name in view_layer.objects:
        view_layer.objects.active = previous


def make_game_ready(context, armature, join=False, bake=False, frame_range=None):
    """Prepare `armature` and its parts for a game engine. Returns a list of what changed."""
    done = []
    if len(root_bones(armature)) > 1:
        add_root(context, armature)
        done.append("one root bone")
    new_name = safe_rig_name(armature)
    if new_name != armature.name:
        armature.name = new_name
        done.append(f"rig renamed {new_name}")
    if is_scaled(armature):
        apply_rig_scale(context, armature)
        done.append("rig scale applied")
    flipped = [o for o in rig_parts(armature) if fix_negative_scale(o)]
    if flipped:
        done.append(f"{len(flipped)} negative scale(s) made positive")
    scaled = [o for o in rig_parts(armature) if is_scaled(o) and apply_object_scale(o)]
    if scaled:
        done.append(f"{len(scaled)} part scale(s) applied")
    parented = bone_parented_meshes(armature)
    for obj in parented:
        rigid_skin(obj, armature, bone_target(obj)[1])
    if parented:
        done.append(f"{len(parented)} part(s) skinned rigidly")
    rigid = sum(make_rigid(o, armature) > 0 for o in skinned_meshes(armature))
    if rigid:
        done.append(f"{rigid} mesh(es) made rigid")
    if bake and has_rig_motion(armature):
        start, end = frame_range or (context.scene.frame_start, context.scene.frame_end)
        rolled = bake_wheel_roll(context, start, end)
        if rolled:
            done.append(f"{rolled} wheel(s) rolled by distance")
        bake_motion(context, armature, start, end)
        done.append(f"motion baked ({start}-{end})")
    if join:
        meshes = skinned_meshes(armature)
        if len(meshes) > 1:
            view_layer = context.view_layer
            for obj in context.selected_objects:
                obj.select_set(False)
            for obj in meshes:
                obj.select_set(True)
            view_layer.objects.active = meshes[0]
            bpy.ops.object.join()
            meshes[0].name = f"{armature.name.removesuffix('_Rig')}_Mesh"
            done.append(f"{len(meshes)} parts joined")
    return done


# --- Scale ---

def rig_parts(armature):
    """Objects that follow `armature`: bone children and skinned meshes."""
    parts = {o for o in armature.children}
    parts.update(skinned_meshes(armature))
    parts.update(o for o in bpy.data.objects if follow_bone(o)[0] == armature)
    return sorted(parts, key=lambda o: o.name)


def is_scaled(obj):
    return any(abs(v - 1.0) > 1e-4 for v in obj.scale)


def is_negative(obj):
    return any(v < 0.0 for v in obj.scale)


def apply_object_scale(obj):
    """Apply `obj`'s scale to its mesh, keeping it (and its children) in place.
    Negative scale is applied by flipping the mesh, so faces keep pointing outwards."""
    if obj.type != 'MESH':
        return False
    children = {c: c.matrix_world.copy() for c in obj.children}
    world = obj.matrix_world.copy()
    scale = Vector(obj.scale)
    for index in range(3):
        if scale[index] < 0.0:
            _mirror_mesh(obj.data, index)  # a flip changes winding: let the helper fix it
            scale[index] = -scale[index]
    obj.data.transform(Matrix.Diagonal(scale).to_4x4())
    obj.data.update()
    obj.scale = (1.0, 1.0, 1.0)
    loc, rot, _ = world.decompose()
    obj.matrix_world = Matrix.LocRotScale(loc, rot, Vector((1.0, 1.0, 1.0)))
    for child, matrix in children.items():
        child.matrix_world = matrix
    return True


def fix_negative_scale(obj):
    """Make `obj`'s scale positive by flipping its mesh instead (it stays where it is)."""
    if obj.type != 'MESH' or not is_negative(obj):
        return False
    children = {c: c.matrix_world.copy() for c in obj.children}
    for index in range(3):
        if obj.scale[index] < 0.0:
            _mirror_mesh(obj.data, index)
            obj.scale[index] = -obj.scale[index]
    for child, matrix in children.items():
        child.matrix_world = matrix
    return True


def apply_rig_scale(context, armature):
    """Apply the rig's scale to its bones without moving the parts that follow them.

    Blender's Apply Scale shifts objects parented to bones; here every part's world
    placement is put back afterwards. Bone location keyframes are not rescaled.
    """
    parts = {o: o.matrix_world.copy() for o in rig_parts(armature)}
    view_layer = context.view_layer
    previous = view_layer.objects.active
    selected = list(context.selected_objects)
    for obj in selected:
        obj.select_set(False)
    armature.select_set(True)
    view_layer.objects.active = armature
    with context.temp_override(active_object=armature, selected_editable_objects=[armature],
                               selected_objects=[armature]):
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    for obj, matrix in parts.items():
        obj.matrix_world = matrix
    # Parts scaled along with the rig keep that size, applied into their meshes.
    for obj in parts:
        if is_scaled(obj):
            apply_object_scale(obj)
    armature.select_set(False)
    for obj in selected:
        if obj.name in view_layer.objects:
            obj.select_set(True)
    if previous is not None and previous.name in view_layer.objects:
        view_layer.objects.active = previous


# --- Cycles ---

def dependency_cycles(armature):
    """Bones in dependency loops: a bone depends on its parent and on the bones its
    constraints target in the same rig. Returns a list of loops (lists of bone names)."""
    depends = {}
    for pose_bone in armature.pose.bones:
        edges = set()
        if pose_bone.parent is not None:
            edges.add(pose_bone.parent.name)
        for con in pose_bone.constraints:
            if getattr(con, "target", None) == armature and getattr(con, "subtarget", ""):
                edges.add(con.subtarget)
            if getattr(con, "pole_target", None) == armature and getattr(con, "pole_subtarget", ""):
                edges.add(con.pole_subtarget)
            for target in getattr(con, "targets", ()):  # Armature constraint and similar
                if target.target == armature and target.subtarget:
                    edges.add(target.subtarget)
        depends[pose_bone.name] = edges & set(armature.pose.bones.keys())

    loops, state, stack = [], {}, []

    def visit(name):
        state[name] = 1
        stack.append(name)
        for other in depends.get(name, ()):
            if state.get(other) == 1:
                loop = stack[stack.index(other):]
                if sorted(loop) not in [sorted(x) for x in loops]:
                    loops.append(list(loop))
            elif state.get(other) is None:
                visit(other)
        stack.pop()
        state[name] = 2

    for name in depends:
        if state.get(name) is None:
            visit(name)
    return loops


# --- Checks ---

def check_rig(armature):
    """Problems an engine (or the rig itself) will hit, as (severity, message, fix, objects)."""
    found = []
    roots = root_bones(armature)
    if len(roots) > 1:
        found.append(('ERROR', f"{len(roots)} root bones: game engines want a single root",
                      'ADD_ROOT', ()))
    if safe_rig_name(armature) != armature.name:
        found.append(('WARNING', f"Rig named '{armature.name}': Unreal imports this name "
                                 "badly", 'RENAME_RIG', ()))
    if is_scaled(armature):
        found.append(('WARNING', "The rig is scaled: engines import its bones at the wrong "
                                 "size", 'APPLY_RIG_SCALE', ()))
    parts = rig_parts(armature)
    negative = [o for o in parts if o.type == 'MESH' and is_negative(o)]
    if negative:
        found.append(('WARNING', f"{len(negative)} part(s) with a negative scale: normals "
                                 "flip in engines", 'FIX_NEGATIVE',
                      tuple(o.name for o in negative)))
    scaled = [o for o in parts if o.type == 'MESH' and is_scaled(o) and not is_negative(o)]
    if scaled:
        found.append(('WARNING', f"{len(scaled)} part(s) with unapplied scale",
                      'APPLY_PART_SCALE', tuple(o.name for o in scaled)))
    for loop in dependency_cycles(armature):
        found.append(('ERROR', f"Dependency cycle: {' -> '.join(loop)} (the rig will "
                               "jitter)", '', ()))
    unit = armature.users_scene[0].unit_settings.scale_length if armature.users_scene else 1.0
    if abs(unit - 1.0) > 1e-6:
        found.append(('INFO', f"Scene unit scale {unit:g}: export with matching FBX "
                              "scale settings", '', ()))
    parented = bone_parented_meshes(armature)
    if parented:
        found.append(('INFO', f"{len(parented)} part(s) parented to bones: engines need them "
                              "skinned", 'RIGID_SKIN', tuple(o.name for o in parented)))
    for obj in skinned_meshes(armature):
        unweighted, split = weight_problems(obj, armature)
        if unweighted:
            found.append(('ERROR', f"{obj.name}: {unweighted} vertices with no bone weight",
                          '', (obj.name,)))
        if split:
            found.append(('WARNING', f"{obj.name}: {split} vertices split between bones "
                                     "(rigid parts will bend)", 'MAKE_RIGID', (obj.name,)))
    if has_rig_motion(armature):
        found.append(('INFO', "Constraints or drivers move the rig: bake before export",
                      'BAKE_RIG', ()))
    return found
