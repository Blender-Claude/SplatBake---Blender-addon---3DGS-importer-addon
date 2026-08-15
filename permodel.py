"""Per-model display settings.

By default every loaded model is drawn with the same scene-wide settings. Tick
**Per-Model Settings** and each model carries its own instead, so one splat can
sit in the scene as a point cloud while another stays full gaussian, at
whatever density, size or SH quality suits it.

WHY A PropertyGroup ON THE OBJECT
---------------------------------
The settings live on the handle Empty as a registered PropertyGroup rather
than as loose ID-property dicts. That buys three things for free:

  * a real UI - sliders and enums draw with their own ranges and tooltips;
  * undo - Blender snapshots object properties, so Ctrl+Z covers them;
  * saving - they travel in the .blend with no extra code.

SEEDING
-------
Switching the tickbox on copies the CURRENT scene values onto every model
first, so nothing jumps the moment you enable it: you start from exactly what
you were already looking at and diverge from there.
"""

# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Blender-Claude


import bpy
from bpy.props import (BoolProperty, EnumProperty, FloatProperty,
                       PointerProperty)
from bpy.types import PropertyGroup

# scene property -> per-model property. Only the settings that describe how a
# model LOOKS are per-model; the colour grade, view transform and sort cadence
# stay global because they describe the scene as a whole.
FIELDS = (
    ("fgs_display_mode", "display_mode", "mode"),
    ("fgs_point_size", "point_size", "point_size"),
    ("fgs_splat_scale", "splat_scale", "splat_scale"),
    ("fgs_density", "density", "density"),
    ("fgs_opacity", "opacity", "opacity_cutoff"),
    ("fgs_max_pixels", "max_pixels", None),      # needs the x100 conversion
    ("fgs_sh_quality", "sh_quality", "sh_quality"),
)


def _redraw(self, context):
    try:
        from . import state
        state.tag_redraw(context)
    except Exception:
        pass


class FGS_PG_display(PropertyGroup):
    """One model's own display settings, stored on its handle Empty."""

    display_mode: EnumProperty(
        name="Display", default='POINTS', update=_redraw,
        description="How this model is drawn",
        items=[('SPLAT', "Splats", "Full gaussian rendering"),
               ('POINTS', "Point Cloud", "Scattered dots - fastest, shows "
                "the model's contours at a glance")])
    point_size: FloatProperty(name="Point Size", default=4.0, min=1.0,
                              max=20.0, update=_redraw)
    splat_scale: FloatProperty(name="Splat Size", default=1.0, min=0.0,
                               max=10.0, update=_redraw)
    density: FloatProperty(name="Density", default=100.0, min=1.0, max=100.0,
                           subtype='PERCENTAGE', update=_redraw,
                           description="Fraction of this model's splats drawn")
    opacity: FloatProperty(name="Opacity Cutoff", default=0.0, min=0.0,
                           max=0.5, update=_redraw)
    max_pixels: FloatProperty(name="Max Splat Size", default=10.0, min=0.0,
                              max=10.0, update=_redraw)
    sh_quality: EnumProperty(
        name="View Colour Quality", default='FULL', update=_redraw,
        description="Spherical-harmonics detail for this model",
        items=[('OFF', "Off (base colour)", "No view-dependent colour"),
               ('DEG1', "Low (degree 1)", "3 coefficients"),
               ('DEG2', "Medium (degree 2)", "8 coefficients"),
               ('FULL', "Full (degree 3)", "All 15 coefficients")])


def settings_of(r):
    """The per-model settings block for renderer `r`, or None."""
    box = bpy.data.objects.get(getattr(r, "box_name", "") or "")
    return getattr(box, "splatbake_display", None) if box else None


def seed_from_scene(scene, renderers):
    """Copy the current scene values onto every model, so turning the feature
    on changes nothing until you actually edit something."""
    for r in renderers:
        s = settings_of(r)
        if s is None:
            continue
        for scene_name, own_name, _ in FIELDS:
            try:
                setattr(s, own_name, getattr(scene, scene_name))
            except Exception:
                pass


def apply_to_all(scene, renderers, source):
    """Push one model's settings onto every other model."""
    src = settings_of(source)
    if src is None:
        return 0
    n = 0
    for r in renderers:
        s = settings_of(r)
        if s is None or s is src:
            continue
        for _, own_name, _ in FIELDS:
            try:
                setattr(s, own_name, getattr(src, own_name))
            except Exception:
                pass
        n += 1
    return n


def params_for(r, base):
    """`base` (the scene-wide draw params) with this model's overrides applied.

    Returns `base` itself when there is nothing to override, so the common
    path allocates nothing.
    """
    s = settings_of(r)
    if s is None:
        return base
    p = dict(base)
    for _, own_name, key in FIELDS:
        if key is None:
            continue
        p[key] = getattr(s, own_name)
    # max_pixels carries the same x100 conversion as the scene path
    mp = float(s.max_pixels)
    p["max_pixels"] = 0.0 if mp <= 0.0 else mp * 100.0
    return p


def uniform(renderers):
    """True when every model resolves to the same settings.

    The renderer can depth-sort several models together in one pass, but only
    if they share their draw parameters - so this decides whether that faster,
    better-ordered path is still valid.
    """
    first = None
    for r in renderers:
        s = settings_of(r)
        if s is None:
            return False
        vals = tuple(getattr(s, own) for _, own, _ in FIELDS)
        if first is None:
            first = vals
        elif vals != first:
            return False
    return True


def register():
    bpy.utils.register_class(FGS_PG_display)
    bpy.types.Object.splatbake_display = PointerProperty(type=FGS_PG_display)


def unregister():
    try:
        del bpy.types.Object.splatbake_display
    except Exception:
        pass
    try:
        bpy.utils.unregister_class(FGS_PG_display)
    except Exception:
        pass
