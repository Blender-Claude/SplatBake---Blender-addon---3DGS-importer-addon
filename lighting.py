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


def build_lit_material(name, soft, color_img=None, roughness=1.0,
                       atlas_uv="SplatCol", emission_mix=0.0,
                       normal_mode=NORMAL_CAMERA, shadow_strength=1.0):
    """Diffuse material for baked splats, lit by the scene.

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

    # Diffuse, not Principled: millions of overlapping discs each adding a
    # specular lobe reads as glitter, and costs more to render for no gain.
    bsdf = nt.nodes.new('ShaderNodeBsdfDiffuse')
    bsdf.location = (-120, 120)
    nt.links.new(base, bsdf.inputs["Color"])

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
        flipn.location = (-330, 420)
        nt.links.new(geo.outputs["Normal"], flipn.inputs[0])
        nt.links.new(sign, flipn.inputs["Scale"])
        nt.links.new(flipn.outputs["Vector"], bsdf.inputs["Normal"])
    try:
        bsdf.inputs["Roughness"].default_value = float(roughness)
    except Exception:
        pass

    # A pure diffuse albedo shows NOTHING until a lamp reaches it, and inside a
    # dense splat cloud most splats are buried behind hundreds of others. That
    # is a cliff: the user ticks the box and the model goes black, with no way
    # to tell a lighting problem from a bake problem. Blending a little of the
    # captured colour back in as emission keeps the model readable and gives a
    # continuous dial from "captured look" to "fully relit" instead of a
    # switch between two extremes.
    shaded = bsdf.outputs["BSDF"]
    if float(emission_mix) > 0.0:
        emis = nt.nodes.new('ShaderNodeEmission')
        emis.location = (-120, 300)
        nt.links.new(base, emis.inputs["Color"])
        mixe = nt.nodes.new('ShaderNodeMixShader')
        mixe.location = (120, 200)
        mixe.inputs[0].default_value = float(min(emission_mix, 1.0))
        nt.links.new(bsdf.outputs["BSDF"], mixe.inputs[1])
        nt.links.new(emis.outputs["Emission"], mixe.inputs[2])
        shaded = mixe.outputs["Shader"]

    if not soft:
        nt.links.new(shaded, out.inputs["Surface"])
        _finish(mat)
        return mat

    uv = nt.nodes.new('ShaderNodeUVMap')
    uv.uv_map = "UVMap"                    # the kernel UVs, not the atlas
    uv.location = (-900, -220)
    dot = nt.nodes.new('ShaderNodeVectorMath')
    dot.operation = 'DOT_PRODUCT'
    dot.location = (-720, -220)
    nt.links.new(uv.outputs["UV"], dot.inputs[0])
    nt.links.new(uv.outputs["UV"], dot.inputs[1])
    # alpha = (exp(-4*(u^2+v^2)) - exp(-4)) / (1 - exp(-4)), clamped at 0
    a4 = _math(nt, 'MULTIPLY', dot.outputs["Value"], 4.0)
    gauss = _math(nt, 'MAXIMUM',
                  _math(nt, 'DIVIDE',
                        _math(nt, 'SUBTRACT',
                              _math(nt, 'EXPONENT',
                                    _math(nt, 'MULTIPLY', a4, -1.0)),
                              _EDGE),
                        1.0 - _EDGE),
                  0.0)
    opac = nt.nodes.new('ShaderNodeAttribute')
    opac.attribute_name = "Opac"
    opac.location = (-720, -420)
    alpha = _math(nt, 'MULTIPLY', gauss, opac.outputs["Fac"])

    transp = nt.nodes.new('ShaderNodeBsdfTransparent')
    transp.location = (-120, -60)
    mix = nt.nodes.new('ShaderNodeMixShader')
    mix.location = (380, 0)
    # Shadow rays can see a THINNER version of the model than camera rays do.
    #
    # This is the knob that makes shadows usable at all. A splat model is a
    # cloud of overlapping discs, so at full opacity every disc sits in the
    # shadow of the dozens in front of it and the model renders black - the
    # failure that forced shadow casting off by default. Scaling alpha down
    # for shadow rays only lets light penetrate the cloud while the model
    # still drops a recognisable shadow onto the floor.
    #
    # It is a cheat, and deliberately so: the honest alternative is volumetric
    # transmittance through the whole cloud, which is exactly what a mesh bake
    # cannot do. Camera rays are untouched, so the model's own appearance is
    # unchanged whatever this is set to.
    alpha_out = alpha
    if float(shadow_strength) < 0.999:
        lp = nt.nodes.new('ShaderNodeLightPath')
        lp.location = (-900, -320)
        drop = _math(nt, 'MULTIPLY', lp.outputs["Is Shadow Ray"],
                     1.0 - float(shadow_strength))
        keep = _math(nt, 'SUBTRACT', 1.0, drop)
        alpha_out = _math(nt, 'MULTIPLY', alpha, keep)

    _lnk(nt, mix.inputs[0], alpha_out)
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


def prepare_object(obj, cast_shadows=False):
    """Object-level settings a lit bake needs.

    SHADOW CASTING IS OFF BY DEFAULT, and that is not an oversight.

    A baked splat model is not a surface, it is a cloud of hundreds of
    thousands of overlapping discs. Let every disc cast a shadow and each one
    lands in the shadow of the dozens stacked in front of it: light reaches
    the outer shell and nothing else, and the model renders as a black blob
    with a faintly lit rim. The emission path already disables shadow casting
    for the same reason - it calls it the opaque-shadow-blob bug - and the lit
    path inherited the problem by turning it back on.

    Self-shadowing inside a capture is also double-counting: the shoot's own
    occlusion is already baked into the colours. What is genuinely wanted is
    for splats to catch scene lamps, which needs no shadow casting at all.

    Turn it on only when the model must drop a shadow onto other geometry, and
    expect the model itself to darken considerably when you do.
    """
    try:
        obj.visible_shadow = bool(cast_shadows)
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
            from .spatial import BucketGrid
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
