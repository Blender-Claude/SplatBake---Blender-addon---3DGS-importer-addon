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

The winding order (and so the normal's sign) is arbitrary per splat, but both
Cycles and EEVEE flip the shading normal towards the viewer on backfacing
hits, so a Diffuse BSDF is lit correctly from either side. Nothing needs
flipping by hand - doing so would double-flip half the discs.

CAVEAT WORTH KNOWING
--------------------
A photogrammetric capture already has its lighting baked into the colours.
Using those colours as albedo means the original lighting is multiplied by the
new lighting. Shadows and highlights from the shoot stay visible and you light
on top of them. That is unavoidable without a de-lighting pass; keep the new
lamps soft and it reads well.
"""

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


def build_lit_material(name, soft, color_img=None, roughness=1.0,
                       atlas_uv="SplatCol"):
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
    try:
        bsdf.inputs["Roughness"].default_value = float(roughness)
    except Exception:
        pass

    if not soft:
        nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
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
    _lnk(nt, mix.inputs[0], alpha)
    nt.links.new(transp.outputs["BSDF"], mix.inputs[1])
    nt.links.new(bsdf.outputs["BSDF"], mix.inputs[2])
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


def prepare_object(obj):
    """Object-level settings a lit bake needs (the emission bake wants the
    opposite, which is why this lives here rather than in the shared path)."""
    try:
        obj.visible_shadow = True          # lit geometry should cast shadows
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
