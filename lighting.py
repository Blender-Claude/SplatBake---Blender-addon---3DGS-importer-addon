"""Light-responsive baking: baked splats that react to the scene's lights.

Self-contained on purpose - nothing else in the add-on imports from here, and
this module touches nothing outside its own material builder, so the existing
emission bake keeps working exactly as before.

THE DIFFERENCE
--------------
The normal bake wires the splat colour into an *Emission* shader. Emission is
its own light source: the model looks identical whether the scene has lamps or
not, and it ignores shadows. That is the right choice when you want the
captured look reproduced verbatim.

With "React to Scene Lights" ticked, the colour becomes the *albedo* of a
Diffuse BSDF instead. Now the renderer has to light it:

  * no lamps (and a black world) -> the model renders black
  * add a lamp -> it lights up, falls off with distance and angle
  * it casts and receives shadows like any other object

NORMALS
-------
Lighting needs a surface direction. Each baked disc spans a splat's two
LARGEST covariance axes, so its face normal is the SMALLEST axis - which is
exactly the standard surface-normal estimate for 3D gaussian splatting. The
discs therefore come out of the bake already carrying usable normals, and flat
shading keeps each one honest.

The sign of that normal needs care, and an earlier version of this file got it
wrong. The reasoning was: the winding order is arbitrary per splat, but both
Cycles and EEVEE flip the shading normal toward the viewer on backfacing hits,
so a Diffuse BSDF ends up lit correctly from either side.

The flip is real. The conclusion does not follow. With arbitrary signs, that
flip sends EVERY disc's shading normal toward the camera - so N.L comes out
near-identical for every splat in the model, the lighting loses all spatial
variation, and the result is one flat tint that barely changes when the lamp
moves. The model looks painted rather than lit.

That reasoning is half right, and the correction matters.

Flipping only the SIGN toward the viewer preserves the angular variation of the
shortest axis - splats still point in different directions, so form still
shades. On a closed object seen from outside, the outward normal already faces
the camera, so the flip and the true orientation agree on every splat you can
actually see. This is also what the reference Cycles implementation of native
splat rendering does (pristinaai/Splat-enabled-blender, gaussian_lit_shader.h):
it takes the shortest axis and flips it toward the ray, with the note that
orbiting reveals the other side via different splats.

So `NORMAL_CAMERA` below is the default: it matches both Blender's native
behaviour and that reference implementation, and it is robust because every
visible splat receives light.

`NORMAL_TRUE` undoes the renderer's flip and shades with the orientation the
bake chose. It is more physically honest - a splat facing away from a lamp
goes dark, so a lamp behind the model gives a real rim - but inside a fuzzy
capture many visible splats face away from the camera, and those render black.
Useful on clean, closed, convex captures; darkening on messy ones.

The bake gives discs a consistent outward winding either way, which costs
nothing and is what makes NORMAL_TRUE meaningful.

THE DOUBLE-LIGHTING PROBLEM, AND WHAT IS DONE ABOUT IT
-----------------------------------------------------
A photogrammetric capture already has its lighting baked into the colours.
Using those colours as albedo means the original lighting is multiplied by the
new lighting: shadows and highlights from the shoot stay visible and you light
on top of them. Earlier versions called this unavoidable. It is not entirely -
`delight()` below removes the low-frequency, direction-dependent part of it.

The idea is that a capture carries its own record of how it was lit. Write
each splat's observed luminance as

    L_i  ~  albedo_i  x  E(N_i)

where E is the irradiance arriving from the direction the splat faces. Albedo
varies from splat to splat with no relation to orientation, but E varies
*smoothly with the normal* - that is what makes one side of an object bright
and the other dim. So fitting a smooth function of the normal to the observed
luminance recovers E and leaves albedo behind as the residual.

Degree-2 spherical harmonics (9 coefficients) are the natural smooth basis:
they are exactly what irradiance from any environment collapses to - a
diffuse surface cannot resolve anything sharper. Fitting 9 coefficients over
millions of splats is massively overdetermined, so the fit cannot absorb real
albedo detail; it can only capture the broad directional trend, which is
precisely the part we want to remove.

What this does NOT fix: cast shadows (they depend on position, not normal) and
baked specular highlights. Those stay. It is a real improvement, not a
solution, so it is a 0-1 slider rather than a switch, defaulting to partial.
"""

# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Blender-Claude


import numpy as np
import bpy

# Quad half-extent 2*sqrt(2) sigma -> gaussian discarded above A = 4, so the
# kernel is normalised to hit exactly zero at the quad edge (no clip line).
_EDGE = 0.0183156389          # exp(-4)


def _lnk(nt, dst, v):
    """Wire a socket into `dst`, or assign a constant."""
    if isinstance(v, bpy.types.NodeSocket):
        nt.links.new(v, dst)
    else:
        dst.default_value = v


def _math(nt, op, a=None, b=None):
    n = nt.nodes.new('ShaderNodeMath')
    n.operation = op
    for sock, v in zip(n.inputs, (a, b)):
        if v is not None:
            _lnk(nt, sock, v)
    return n.outputs[0]


NORMAL_CAMERA = 'CAMERA'
NORMAL_TRUE = 'TRUE'


def _soft_alpha(nt, kernel='NORM'):
    """Kernel-matched soft alpha: the gaussian falloff over the disc UVs times
    the per-splat "Opac" attribute. Shared by the lit material and the shadow
    proxy, so a dropped shadow is shaped by exactly the discs the camera sees.
    """
    uv = nt.nodes.new('ShaderNodeUVMap')
    uv.uv_map = "UVMap"                    # the kernel UVs, not the atlas
    uv.location = (-900, -220)
    dot = nt.nodes.new('ShaderNodeVectorMath')
    dot.operation = 'DOT_PRODUCT'
    dot.location = (-720, -220)
    nt.links.new(uv.outputs["UV"], dot.inputs[0])
    nt.links.new(uv.outputs["UV"], dot.inputs[1])
    # 'NORM' : (exp(-A) - exp(-4)) / (1 - exp(-4)), clamped at 0 - the
    #          viewport's normalised PC/V215 kernel, zero at the quad edge.
    # 'PLAIN': exp(-A) - the viewport's SOFT (classic Blender) kernel.
    a4 = _math(nt, 'MULTIPLY', dot.outputs["Value"], 4.0)
    falloff = _math(nt, 'EXPONENT', _math(nt, 'MULTIPLY', a4, -1.0))
    if kernel == 'PLAIN':
        gauss = falloff
    else:
        gauss = _math(nt, 'MAXIMUM',
                      _math(nt, 'DIVIDE',
                            _math(nt, 'SUBTRACT', falloff, _EDGE),
                            1.0 - _EDGE),
                      0.0)
    opac = nt.nodes.new('ShaderNodeAttribute')
    opac.attribute_name = "Opac"
    opac.location = (-720, -420)
    return _math(nt, 'MULTIPLY', gauss, opac.outputs["Fac"])


def build_lit_material(name, soft, color_img=None, roughness=1.0,
                       atlas_uv="SplatCol", emission_mix=0.0,
                       normal_mode=NORMAL_CAMERA, ambient=0.15, gain=3.0,
                       kernel='NORM'):
    """Preview-matched relight material for baked splats.

    Computes the same equation as the viewport preview's scene lighting -
    captured colour times (ambient + incoming light) - via a white Diffuse
    BSDF read back through Shader to RGB, so a bake finally responds to
    lamps the way the preview taught the user to expect. EEVEE is the
    target engine for this, as it always was for lit bakes.

    soft      : keep the radial gaussian alpha (times per-splat opacity), so
                discs stay soft-edged instead of hard squares.
    color_img : per-splat colour atlas; when given the albedo is read from it
                through the atlas UV layer, otherwise from the "Col" vertex
                colour attribute. Matches the emission bake either way.
    """
    mat = bpy.data.materials.new(name + "_lit")
    mat.use_nodes = True
    nt = mat.node_tree
    for nd in list(nt.nodes):
        nt.nodes.remove(nd)

    out = nt.nodes.new('ShaderNodeOutputMaterial')
    out.location = (600, 0)

    if color_img is not None:
        uvn = nt.nodes.new('ShaderNodeUVMap')
        uvn.uv_map = atlas_uv
        tex = nt.nodes.new('ShaderNodeTexImage')
        tex.image = color_img
        tex.interpolation = 'Closest'      # one texel per splat, no bleed
        tex.extension = 'EXTEND'
        tex.location = (-500, 120)
        nt.links.new(uvn.outputs["UV"], tex.inputs["Vector"])
        base = tex.outputs["Color"]
    else:
        attr = nt.nodes.new('ShaderNodeAttribute')
        attr.attribute_name = "Col"
        attr.location = (-500, 120)
        base = attr.outputs["Color"]

    # -- the lighting model: the PREVIEW's, not a physical one (1.20.7) ----
    #
    # The physical version (de-lit albedo on a Diffuse BSDF) was correct and
    # useless. The viewport preview relights by multiplying the CAPTURED
    # colour with `ambient + lambert`, energies normalised - in its own
    # words it "shows WHERE light lands, not photometric values"
    # (renderer.gather_preview_lights). Users calibrate on that. Real
    # wattage on a de-lit albedo can never look like it, so bakes read as
    # "the light is not detected" no matter how correct they are.
    # The bake now computes the preview's equation instead:
    #
    #     final = colour * mix(ambient + incoming_light, 1.0, keep)
    #
    # `incoming_light` is a white Diffuse BSDF captured by Shader to RGB -
    # the one node that hands the renderer's own lighting (lamps, world,
    # shadow maps) back as a colour to compose with. Shader to RGB is an
    # EEVEE node by nature; engine_hint already steers lit bakes to EEVEE,
    # and in Cycles this degrades to a dim ambient look instead of breaking.
    white = nt.nodes.new('ShaderNodeBsdfDiffuse')
    white.location = (-620, 320)
    white.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
    try:
        white.inputs["Roughness"].default_value = float(roughness)
    except Exception:
        pass

    # NORMAL_TRUE undoes the renderer's backface flip: N * (1 - 2 * Backfacing)
    # restores the orientation the bake chose. Left alone (NORMAL_CAMERA) the
    # renderer flips the normal toward the viewer, which is what Blender does
    # natively and what the reference Cycles implementation does too.
    if normal_mode == NORMAL_TRUE:
        geo = nt.nodes.new('ShaderNodeNewGeometry')
        geo.location = (-900, 420)
        sign = _math(nt, 'SUBTRACT', 1.0,
                     _math(nt, 'MULTIPLY', geo.outputs["Backfacing"], 2.0))
        flipn = nt.nodes.new('ShaderNodeVectorMath')
        flipn.operation = 'SCALE'
        flipn.location = (-780, 420)
        nt.links.new(geo.outputs["Normal"], flipn.inputs[0])
        nt.links.new(sign, flipn.inputs["Scale"])
        nt.links.new(flipn.outputs["Vector"], white.inputs["Normal"])

    s2rgb = nt.nodes.new('ShaderNodeShaderToRGB')
    s2rgb.location = (-440, 320)
    nt.links.new(white.outputs["BSDF"], s2rgb.inputs["Shader"])

    amb = float(max(ambient, 0.0))
    keep = float(np.clip(emission_mix, 0.0, 1.0))
    # Light Gain: the model shows its captured palette even unlit (ambient +
    # Keep Captured Colour form a constant floor of roughly a quarter of the
    # palette), so a lamp must OUTSHINE that floor before the eye sees any
    # change - while a plain cube starts from black and registers the
    # faintest light. Same physics, different baseline. Boosting the
    # incoming light before it is composed makes the model respond at
    # cube-like distances; 1.0 is the physically-matched setting.
    boost = nt.nodes.new('ShaderNodeVectorMath')
    boost.operation = 'SCALE'
    boost.location = (-350, 320)
    nt.links.new(s2rgb.outputs["Color"], boost.inputs[0])
    boost.inputs["Scale"].default_value = float(max(gain, 0.0))
    term = nt.nodes.new('ShaderNodeVectorMath')   # ambient + gain * light
    term.operation = 'ADD'
    term.location = (-260, 320)
    nt.links.new(boost.outputs["Vector"], term.inputs[0])
    term.inputs[1].default_value = (amb, amb, amb)
    # keep-dial: lerp the light term toward 1.0 so "Keep Captured Colour"
    # still runs from fully relit (0) to the untouched capture (1).
    scaled = nt.nodes.new('ShaderNodeVectorMath')
    scaled.operation = 'SCALE'
    scaled.location = (-100, 320)
    nt.links.new(term.outputs["Vector"], scaled.inputs[0])
    scaled.inputs["Scale"].default_value = 1.0 - keep
    lerped = nt.nodes.new('ShaderNodeVectorMath')
    lerped.operation = 'ADD'
    lerped.location = (60, 320)
    nt.links.new(scaled.outputs["Vector"], lerped.inputs[0])
    lerped.inputs[1].default_value = (keep, keep, keep)
    final_col = nt.nodes.new('ShaderNodeVectorMath')
    final_col.operation = 'MULTIPLY'
    final_col.location = (220, 220)
    nt.links.new(base, final_col.inputs[0])
    nt.links.new(lerped.outputs["Vector"], final_col.inputs[1])

    emis = nt.nodes.new('ShaderNodeEmission')
    emis.location = (380, 160)
    nt.links.new(final_col.outputs["Vector"], emis.inputs["Color"])
    shaded = emis.outputs["Emission"]

    if not soft:
        nt.links.new(shaded, out.inputs["Surface"])
        _finish(mat)
        return mat

    alpha = _soft_alpha(nt, kernel)

    transp = nt.nodes.new('ShaderNodeBsdfTransparent')
    transp.location = (-120, -60)
    mix = nt.nodes.new('ShaderNodeMixShader')
    mix.location = (380, 0)
    # (1.20.5) The Light Path "Is Shadow Ray" alpha-thinning that lived here
    # is gone. EEVEE supports the Light Path node only partially, so in the
    # engine this addon recommends the trick never ran: every disc sat inside
    # its neighbours' virtual shadow maps, and a lit bake with Cast Shadows
    # on rendered BLACK while still dropping a crisp floor shadow. Shadow
    # thinning is now object-level, where EEVEE and Cycles agree: the visible
    # model never casts (prepare_object) and add_shadow_proxy() carries the
    # shadow on a camera-invisible twin instead.
    _lnk(nt, mix.inputs[0], alpha)
    nt.links.new(transp.outputs["BSDF"], mix.inputs[1])
    nt.links.new(shaded, mix.inputs[2])
    nt.links.new(mix.outputs["Shader"], out.inputs["Surface"])
    _finish(mat)
    return mat


def _finish(mat):
    """Transparency settings. Unlike the emission bake, shadows stay ON here -
    a lit object that cast none would float. Alpha-aware shadows matter too,
    or every soft disc would throw a hard square."""
    for attr, val in (("surface_render_method", 'DITHERED'),
                      ("blend_method", 'HASHED'),
                      ("shadow_method", 'HASHED')):
        try:
            setattr(mat, attr, val)
        except Exception:
            pass
    try:
        mat.use_transparent_shadow = True
    except Exception:
        pass


def build_shadow_material(name, soft, shadow_strength=0.04,
                          kernel='NORM'):
    """What the lamps see instead of the real model.

    Alpha is the same kernel gaussian times per-splat opacity as the visible
    material, multiplied by a plain constant shadow_strength - no Light Path
    tricks - so EEVEE and Cycles cast the same shadow, and Shadow Strength
    means exactly one thing: how dense the dropped shadow is. The surface
    closure is never seen lit (the proxy is camera-invisible); a black
    diffuse keeps it inert if it ever leaks into a reflection."""
    mat = bpy.data.materials.new(name + "_shadow")
    mat.use_nodes = True
    nt = mat.node_tree
    for nd in list(nt.nodes):
        nt.nodes.remove(nd)
    out = nt.nodes.new('ShaderNodeOutputMaterial')
    out.location = (600, 0)
    bsdf = nt.nodes.new('ShaderNodeBsdfDiffuse')
    bsdf.location = (-120, 120)
    bsdf.inputs["Color"].default_value = (0.0, 0.0, 0.0, 1.0)
    transp = nt.nodes.new('ShaderNodeBsdfTransparent')
    transp.location = (-120, -60)
    mix = nt.nodes.new('ShaderNodeMixShader')
    mix.location = (380, 0)
    s = float(np.clip(shadow_strength, 0.0, 1.0))
    alpha = (_math(nt, 'MULTIPLY', _soft_alpha(nt, kernel), s)
             if soft else s)
    _lnk(nt, mix.inputs[0], alpha)
    nt.links.new(transp.outputs["BSDF"], mix.inputs[1])
    nt.links.new(bsdf.outputs["BSDF"], mix.inputs[2])
    nt.links.new(mix.outputs["Shader"], out.inputs["Surface"])
    _finish(mat)
    return mat


def add_shadow_proxy(context, obj, soft, shadow_strength,
                     kernel='NORM'):
    """Camera-invisible twin of the baked model that carries its shadow.

    Shares the same mesh datablock (zero copy); the shadow material is
    overridden at the OBJECT level so the visible model keeps its lit
    material. Ray visibility does the rest: hidden from camera and light
    bounces, visible to shadow maps only - the object-level mechanism EEVEE
    moved shadow control to in 4.2, and the one Cycles always had. Displayed
    as bounds so it neither z-fights its twin in solid shading nor (since
    4.2 renders bounds-displayed objects) vanishes from rendered shading."""
    proxy = bpy.data.objects.new(obj.name + "_shadow", obj.data)
    context.collection.objects.link(proxy)
    proxy.parent = obj
    # Tag it, so stale twins can be found and purged (see
    # purge_stale_proxies). And unlike 1.20.5, it stays SELECTABLE:
    # deleting a parent does not delete its children in Blender, so an
    # unselectable twin was guaranteed to outlive its model - and then sit
    # in every shadow map, silently blackening every later bake made in the
    # same spot. Found in the field within a day.
    proxy["fgs_shadow_proxy"] = obj.name
    mat = build_shadow_material(obj.name, soft,
                                float(shadow_strength), kernel)
    for slot in proxy.material_slots:
        slot.link = 'OBJECT'
        slot.material = mat
    for attr, val in (("visible_camera", False),
                      ("visible_diffuse", False),
                      ("visible_glossy", False),
                      ("visible_transmission", False),
                      ("visible_volume_scatter", False),
                      ("visible_shadow", True)):
        try:
            setattr(proxy, attr, val)
        except Exception:
            pass
    # Shrink the twin 4% about its own centre. At identical scale every
    # lit-side disc of the model coincides with its caster in the shadow
    # map - self-shadowing by construction, bias notwithstanding. Pulling
    # the caster just inside the cloud puts the model's lamp-facing shell
    # OUTSIDE its own shadow, while the floor shadow shrinks by an
    # invisible 4%. The interior and far side stay shadowed, which is
    # where shadow belongs anyway.
    try:
        from mathutils import Matrix, Vector
        corners = [Vector(c) for c in obj.bound_box]
        centre = sum(corners, Vector()) / 8.0
        proxy.matrix_world = (Matrix.Translation(centre)
                              @ Matrix.Scale(0.96, 4)
                              @ Matrix.Translation(-centre))
    except Exception:
        pass
    try:
        proxy.display_type = 'BOUNDS'
    except Exception:
        pass
    return proxy


def purge_stale_proxies(context):
    """Remove shadow twins whose model is gone.

    Deleting an object does not delete its children, so a baked model can
    die while its camera-invisible twin lives on - invisible in renders,
    easy to miss in the outliner, and still writing itself into every
    shadow map, which silently blackens any later bake in the same spot.
    Called before every lit bake; returns how many were removed so the
    console can say so out loud."""
    gone = 0
    for ob in list(context.scene.objects):
        try:
            if not ob.get("fgs_shadow_proxy"):
                continue
            par = ob.parent
            if par is None or par.name not in context.scene.objects:
                bpy.data.objects.remove(ob, do_unlink=True)
                gone += 1
        except Exception:
            pass
    return gone


def describe_lit_environment(context, params):
    """One honest line about everything that decides whether a lit bake can
    visibly respond to light, printed with every lit bake. Three rounds of
    "still dark" taught us the scene state IS the story: sticky operator
    values, a viewport left in Material Preview, an orphaned shadow twin -
    none of them visible in the bake report until now. Returns
    (console_line, [warnings for the status bar])."""
    lines, warns = [], []
    eng = str(getattr(context.scene.render, "engine", "?"))
    lines.append("engine=" + eng)
    if 'EEVEE' not in eng.upper():
        warns.append("lit shading targets EEVEE; in " + eng
                     + " it degrades to a dim ambient look")
    try:
        lines.append("view_transform="
                     + str(context.scene.view_settings.view_transform))
    except Exception:
        pass
    shading = []
    try:
        for a in context.screen.areas:
            if a.type == 'VIEW_3D':
                sp = a.spaces.active
                if sp is not None:
                    shading.append(str(sp.shading.type))
    except Exception:
        pass
    lines.append("viewports=" + (",".join(shading) if shading else "?"))
    if shading and 'RENDERED' not in shading:
        warns.append("no viewport is in Rendered shading - scene lamps are "
                     "not shown in Solid or Material Preview")
    lamps = []
    try:
        for ob in context.scene.objects:
            if ob.type == 'LIGHT':
                lamps.append("%s:%s %gW" % (ob.name, ob.data.type,
                                            float(getattr(ob.data, "energy",
                                                          0.0))))
    except Exception:
        pass
    lines.append("lamps=[" + ", ".join(lamps) + "]")
    twins = sum(1 for ob in context.scene.objects
                if ob.get("fgs_shadow_proxy"))
    lines.append("shadow_twins=%d" % twins)
    lines.append("params={" + params + "}")
    return "; ".join(lines), warns


def prepare_object(obj):
    """Object-level settings a lit bake needs.

    THE VISIBLE MODEL NEVER CASTS SHADOWS - no longer even optionally.

    A baked splat model is a cloud of hundreds of thousands of overlapping
    discs. Let every disc cast and each one lands in the shadow of the dozens
    stacked in front of it: light reaches the outer shell and nothing else,
    and the model renders as a black blob with a faintly lit rim. The old
    escape hatch - thinning the alpha for shadow rays with the Light Path
    node - turned out to run only in Cycles: EEVEE supports that node only
    partially, so in the engine this addon actually recommends, the blob was
    back in full (found the hard way, fixed in 1.20.5). Wanting a shadow is
    still legitimate, so it moved to where both engines agree - the object
    level: add_shadow_proxy() puts a camera-invisible twin in the shadow maps
    instead, and this object stays out of them entirely. Self-shadowing
    inside a capture was double-counting anyway: the shoot's own occlusion is
    already baked into the colours.
    """
    try:
        obj.visible_shadow = False
    except Exception:
        pass
    try:
        for poly in obj.data.polygons:     # flat: each disc is one plane
            poly.use_smooth = False
    except Exception:
        pass
    return obj


def scene_light_hint(context):
    """A short warning when the scene cannot light anything, so a black bake
    is explained instead of looking like a failure."""
    lamps = [o for o in context.scene.objects if o.type == 'LIGHT']
    world = context.scene.world
    bg = 0.0
    if world is not None:
        try:
            if world.use_nodes:
                for n in world.node_tree.nodes:
                    if n.type == 'BACKGROUND':
                        c = n.inputs["Color"].default_value
                        s = n.inputs["Strength"].default_value
                        bg = max(bg, float(s) * max(c[0], c[1], c[2]))
            else:
                bg = max(world.color[:])
        except Exception:
            pass
    if not lamps and bg <= 1e-4:
        return "no lights and a black world - the bake will render black"
    if not lamps:
        return "no lamps; only world lighting will show"
    return None


# ---------------------------------------------------------------------
# De-lighting
# ---------------------------------------------------------------------

# Rec.709 luminance. Splat colours reaching here are scene-linear.
_LUM = np.array([0.2126, 0.7152, 0.0722], np.float32)
# The fitted shading is clamped before dividing. Without this, a splat whose
# normal points into a direction the fit made very dark gets divided by
# almost zero and explodes into a firefly - one bright pixel that survives
# every denoiser. The floor matters far more than the ceiling.
_E_MIN, _E_MAX = 0.45, 2.2


def splat_normals(quat, scale):
    """Per-splat unit normal: the SMALLEST covariance axis, in model space.

    Matches the disc geometry exactly - a baked disc spans the two largest
    axes, so its face normal is the smallest one. Trained splats flatten onto
    the surface they captured, which is what makes this the standard normal
    estimate for 3D gaussian splatting.
    """
    n = len(quat)
    w, x, y, z = quat[:, 0], quat[:, 1], quat[:, 2], quat[:, 3]
    R = np.empty((n, 3, 3), np.float32)
    R[:, 0, 0] = 1 - 2 * (y * y + z * z)
    R[:, 0, 1] = 2 * (x * y - w * z)
    R[:, 0, 2] = 2 * (x * z + w * y)
    R[:, 1, 0] = 2 * (x * y + w * z)
    R[:, 1, 1] = 1 - 2 * (x * x + z * z)
    R[:, 1, 2] = 2 * (y * z - w * x)
    R[:, 2, 0] = 2 * (x * z - w * y)
    R[:, 2, 1] = 2 * (y * z + w * x)
    R[:, 2, 2] = 1 - 2 * (x * x + y * y)
    smallest = np.argmin(np.maximum(scale.astype(np.float32), 0.0), axis=1)
    nrm = R[np.arange(n), :, smallest]
    ln = np.linalg.norm(nrm, axis=1, keepdims=True)
    return (nrm / np.maximum(ln, 1e-8)).astype(np.float32)


def _sh2_basis(N):
    """Real degree-2 SH basis, (n, 9). Constants folded into the fit, so only
    the angular shape matters."""
    x, y, z = N[:, 0], N[:, 1], N[:, 2]
    o = np.ones(len(N), np.float32)
    return np.stack([o, x, y, z, x * y, y * z, z * x,
                     3.0 * z * z - 1.0, x * x - y * y], axis=1).astype(np.float32)


def estimate_shading(col, normals):
    """Fit smooth irradiance E(N) to observed luminance. Returns (n,) float32,
    normalised to mean 1 and clamped."""
    lum = col.astype(np.float32) @ _LUM
    Y = _sh2_basis(normals)
    # Normal equations: 9x9, so the solve is free regardless of splat count.
    # lstsq on the full (n, 9) would be needlessly heavy at these sizes.
    A = Y.T @ Y
    A[np.diag_indices_from(A)] += 1e-3          # ridge: normals are often
    #                                             clustered, leaving A near
    #                                             singular on flat captures
    coef = np.linalg.solve(A, Y.T @ lum)
    E = Y @ coef
    m = float(np.mean(E))
    if not np.isfinite(m) or abs(m) < 1e-6:
        return np.ones(len(col), np.float32)
    E = E / m
    return np.clip(E, _E_MIN, _E_MAX).astype(np.float32)


def delight(col, normals, strength=1.0):
    """Divide the captured colour by the fitted directional shading.

    `strength` 0 leaves the colour untouched; 1 removes the full fitted trend.
    Overall brightness is preserved - the point is to flatten the lighting
    across the model, not to darken or brighten it - so a model that looked
    right unlit still looks right before the new lamps are added.
    """
    strength = float(np.clip(strength, 0.0, 1.0))
    if strength <= 0.0 or len(col) == 0:
        return col
    try:
        E = estimate_shading(col, normals)
    except Exception as e:
        print("[SplatBake] de-lighting skipped:", e)
        return col
    out = col.astype(np.float32) / (E ** strength)[:, None]
    before = float(np.mean(col.astype(np.float32) @ _LUM))
    after = float(np.mean(out @ _LUM))
    if after > 1e-8:
        out *= np.float32(before / after)
    return np.clip(out, 0.0, None, out=out)


# Albedo above 1 is unphysical - a surface cannot reflect more light than it
# receives - and with many overlapping semi-transparent discs it compounds into
# a glow. The reference Cycles implementation clamps splat colour to [0, 1]
# before building the diffuse closure, and de-lighting can push values past 1
# when it divides by a small fitted shading term, so the same clamp is applied
# here on the lit path only. Emission bakes are radiance, not albedo, and are
# left alone.
def clamp_albedo(col):
    return np.clip(np.asarray(col, np.float32), 0.0, 1.0)


# ---------------------------------------------------------------------
# Surface normals
# ---------------------------------------------------------------------

def surface_normals(centers, quat, scale, grid=None, passes=2):
    """Per-splat normals regularised into a coherent surface field.

    WHY THE RAW NORMALS ARE NOT ENOUGH
    ----------------------------------
    A splat's normal is taken as its shortest covariance axis. That is the
    standard estimate and it is fine for splats that trained flat against a
    surface - but a real 3DGS capture is full of near-isotropic blobs, and for
    a blob the "shortest axis" is numerical noise. Its direction is arbitrary.

    That is what makes per-disc relighting fail in practice. Shade hundreds of
    thousands of overlapping semi-transparent discs whose normals point in
    random directions and the alpha composite AVERAGES the shading: roughly
    half the discs face any given lamp, so every pixel converges to the same
    mid grey times albedo. The model comes out evenly tinted and barely
    responds when the lamp moves - not because the lighting is not being
    computed, but because it is being computed hundreds of times per pixel
    with normals that cancel out.

    The literature reaches the same conclusion from the other direction:
    relightable 3DGS methods all regularise geometry (normal priors, SDF
    constraints, mesh extraction) before they attempt to relight, precisely
    because raw Gaussian orientations are not a surface.

    WHAT THIS DOES
    --------------
    Orient every normal consistently, then average each splat's normal with
    its spatial neighbours. Noise is uncorrelated between neighbours and
    cancels under averaging; genuine surface orientation is shared between
    them and survives. A couple of passes turns a field of random directions
    into something that behaves like a surface normal, which is what a lamp
    needs in order to shade form rather than tint uniformly.

    Falls back to the raw axes if the grid cannot be built - worse lighting,
    but never a failed bake.
    """
    nrm = splat_normals(quat, scale)
    centers = np.ascontiguousarray(centers, np.float32)
    try:
        if grid is None:
            from .splatcore.spatial import BucketGrid
            grid = BucketGrid(centers)
        order, starts = grid.bucket_order()
    except Exception as e:
        print("[SplatBake] normal smoothing unavailable:", e)
        return nrm

    # Consistent orientation first. Averaging normals that disagree in SIGN
    # cancels them to zero, so this is not optional - it is what makes the
    # averaging meaningful at all.
    c = centers.mean(axis=0)
    radial = centers - c
    flip = np.einsum('ij,ij->i', nrm, radial) < 0.0
    nrm[flip] = -nrm[flip]

    b = grid.n_buckets
    bof = grid.bucket_of
    for _ in range(max(1, int(passes))):
        acc = np.empty((b, 3), np.float32)
        for k in range(3):
            acc[:, k] = np.bincount(bof, weights=nrm[:, k], minlength=b)
        ln = np.linalg.norm(acc, axis=1, keepdims=True)
        acc /= np.maximum(ln, 1e-8)
        # Blend toward the neighbourhood mean rather than replacing outright,
        # so genuinely flat splats that already agree with their neighbours
        # keep their own (more accurate) orientation.
        nrm = nrm * np.float32(0.35) + acc[bof] * np.float32(0.65)
        ln = np.linalg.norm(nrm, axis=1, keepdims=True)
        nrm /= np.maximum(ln, 1e-8)
    return nrm.astype(np.float32)


# ---------------------------------------------------------------------
# Render engine
# ---------------------------------------------------------------------

def engine_hint(context):
    """Warn when the render engine will make a lit bake unusable.

    A lit splat bake is thousands of stacked semi-transparent discs, and each
    camera ray has to walk through all of them. In Cycles that means hundreds
    of transparent bounces per ray, on top of full path tracing - and if the
    machine has no supported GPU, on the CPU. The viewport then converges so
    slowly that a correct bake still looks black or noisy, which is
    indistinguishable from a broken one.

    EEVEE rasterises instead, handles the alpha in one pass, runs on
    integrated graphics, and is effectively instant. For relighting splats it
    is not a downgrade - it is the right tool, and the same conclusion other
    splat relighting addons have reached.
    """
    try:
        eng = context.scene.render.engine
    except Exception:
        return ""
    if 'CYCLES' not in str(eng).upper():
        return ""
    dev = 'CPU'
    try:
        cyc = context.scene.cycles
        dev = str(getattr(cyc, "device", "CPU")).upper()
    except Exception:
        pass
    if dev == 'CPU':
        return ("Cycles on CPU: a lit splat bake may never converge. "
                "Switch to EEVEE.")
    return "Cycles is slow for stacked transparency here; EEVEE is far faster."
