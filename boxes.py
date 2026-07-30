"""Proxy Empties that act as transform handles for splat models.

The Empty is a normal Blender object, so native G / R / S (with X/Y/Z axis
constraints, numeric input, snapping, etc.) all work on it. The renderer reads
the Empty's matrix every frame, so the splats follow it live. The Empty can be
shown (a framing cube) or hidden (display size 0) but stays selectable either
way, so transforms keep working when it's hidden.
"""

import bpy
from mathutils import Matrix, Vector

BOX_NAME = "GaussianSplat_Box"


def spawn_box(context, matrix, show, name=BOX_NAME):
    """Create a cube Empty at the given world matrix, select it, return it."""
    empty = bpy.data.objects.new(name, None)
    empty.empty_display_type = 'CUBE'
    empty.empty_display_size = 1.0 if show else 0.0
    empty.matrix_basis = matrix
    context.collection.objects.link(empty)
    for o in context.selected_objects:
        o.select_set(False)
    empty.select_set(True)
    context.view_layer.objects.active = empty
    return empty


def make_box(context, xyz, show=True, name=BOX_NAME):
    """Frame the cloud with a cube Empty at its centre. Returns
    (name, rest_inv). model_matrix = box.matrix_world @ rest_inv, so the splats
    sit at their data location when the box is at its rest pose."""
    from .loaders import robust_bounds
    # Robust bounds, not raw min/max: a few floater splats would otherwise put
    # the handle's pivot tens of thousands of units away from the scene, so
    # rotating or scaling it would fling the model off screen.
    mn, mx = robust_bounds(xyz)
    center = Vector(((mn + mx) * 0.5).tolist())
    half = Vector([max(float(h), 1e-3) for h in ((mx - mn) * 0.5)])
    rest = Matrix.LocRotScale(center, None, half)
    empty = spawn_box(context, rest, show, name=name)
    return empty.name, rest.inverted()
