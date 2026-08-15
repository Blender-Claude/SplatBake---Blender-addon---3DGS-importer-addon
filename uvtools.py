# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Blender-Claude
# NOTE (1.20.4): currently unused - the experimental solid-surface bake,
# the only caller of this module, is parked in operators.py. Kept intact
# for its return; nothing imports it until then.
"""UV mapping for baked splat meshes.

Self-contained, like lighting.py: nothing else imports from here except the
surface-bake operator, so the rest of the add-on is unaffected.

WHY
---
The solid surface bake stores colour in a *vertex colour* attribute. That is
fine inside Blender, but it has two real limits: the colour resolution is tied
to the mesh density (a coarse surface gets coarse colour, however detailed the
splats were), and most exchange formats drop vertex colours on export.

This module gives the baked mesh a real UV layout and rasterises the splat
colours into an image texture, so the model becomes a normal textured asset:
paintable, exportable to glTF/FBX/OBJ, and sharper than the mesh is dense.

HOW THE TEXTURE IS MADE
-----------------------
Not with Cycles' bake operator - that needs an engine switch, a render pass,
and it fails silently in a dozen ways. Instead the triangles are rasterised
directly in numpy: for every triangle, walk the texels inside its UV
footprint and interpolate the three corner colours barycentrically. It is
deterministic, needs no renderer, and can be tested outside Blender.

Texels no triangle covers are then dilated outward from their filled
neighbours. Without that, bilinear filtering samples the empty gutter along
every island edge and you get dark seams across the model.
"""

import numpy as np

try:
    import bpy
except Exception:                      # allows offline testing of the maths
    bpy = None


# --------------------------------------------------------------- unwrapping

def _view3d_override():
    """Find a real VIEW_3D area to run UV operators against.

    `bpy.ops.uv.smart_project` and `object.mode_set` poll the CONTEXT. When a
    bake is launched from a props dialog, execute() runs with the dialog's
    context - `context.area` is the popup, not the 3D view - and those
    operators fail with "context is incorrect". Overriding onto a genuine
    VIEW_3D area (and its WINDOW region) makes the call behave as if it were
    invoked from the viewport itself.
    """
    if bpy is None:
        return None
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type != 'VIEW_3D':
                continue
            for region in area.regions:
                if region.type == 'WINDOW':
                    return {"window": win, "area": area, "region": region}
    return None


def smart_unwrap(obj, angle_limit=1.15192, island_margin=0.02):
    """Give `obj` a UV layout, trying progressively blunter methods.

    Smart UV Project is the right tool (it cuts by face angle and packs the
    islands), but it is an operator: it needs the object active, in object
    mode, a valid 3D-view context, and it can fail on degenerate geometry.
    Cube projection is the fallback - cruder, but it always produces a usable
    layout.
    """
    if bpy is None:
        raise RuntimeError("Blender required")
    me = obj.data
    if not me.uv_layers:
        me.uv_layers.new(name="UVMap")

    ctx = bpy.context
    prev_active = ctx.view_layer.objects.active
    prev_sel = [o for o in ctx.selected_objects]
    ov = _view3d_override()
    try:
        for o in prev_sel:
            o.select_set(False)
        obj.select_set(True)
        ctx.view_layer.objects.active = obj

        def _run():
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            try:
                bpy.ops.uv.smart_project(angle_limit=angle_limit,
                                         island_margin=island_margin)
                m = "smart"
            except Exception as e:
                print("[SplatBake] smart UV project failed, "
                      "using cube projection:", e)
                bpy.ops.uv.cube_project(cube_size=1.0)
                m = "cube"
            bpy.ops.object.mode_set(mode='OBJECT')
            return m

        if ov is not None and hasattr(ctx, "temp_override"):
            with ctx.temp_override(**ov):
                method = _run()
        else:
            method = _run()
    finally:
        try:
            if obj.mode != 'OBJECT':
                if ov is not None and hasattr(ctx, "temp_override"):
                    with ctx.temp_override(**ov):
                        bpy.ops.object.mode_set(mode='OBJECT')
                else:
                    bpy.ops.object.mode_set(mode='OBJECT')
        except Exception:
            pass
        try:
            ctx.view_layer.objects.active = prev_active
            for o in prev_sel:
                o.select_set(True)
        except Exception:
            pass
    return method


# --------------------------------------------------------------- rasterising

def rasterize(uv, tris, cols, size, supersample=1):
    """Rasterise per-vertex colours into a (size, size, 3) float32 image.

    uv    : (V, 2) UV coordinate per vertex, 0..1
    tris  : (T, 3) vertex indices
    cols  : (V, 3) linear colour per vertex
    Returns (image, filled_mask). Pure numpy - no Blender needed.

    Half-texel offsets matter here: texel (i, j) samples at
    ((j + 0.5) / size, (i + 0.5) / size), so the barycentric test has to use
    texel CENTRES. Testing corners shifts the whole texture half a texel and
    puts a visible offset in the colour.
    """
    n = int(size) * int(supersample)
    img = np.zeros((n, n, 3), np.float32)
    filled = np.zeros((n, n), bool)
    if len(tris) == 0:
        return img, filled

    # UV origin is bottom-left; image row 0 is the bottom row, so v maps
    # straight to the row index without flipping.
    px = np.clip(uv[:, 0], 0.0, 1.0) * n - 0.5
    py = np.clip(uv[:, 1], 0.0, 1.0) * n - 0.5

    for t in tris:
        i0, i1, i2 = int(t[0]), int(t[1]), int(t[2])
        x0, y0 = px[i0], py[i0]
        x1, y1 = px[i1], py[i1]
        x2, y2 = px[i2], py[i2]
        den = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if abs(den) < 1e-12:                       # zero-area in UV space
            continue
        lo_x = max(int(np.floor(min(x0, x1, x2))), 0)
        hi_x = min(int(np.ceil(max(x0, x1, x2))), n - 1)
        lo_y = max(int(np.floor(min(y0, y1, y2))), 0)
        hi_y = min(int(np.ceil(max(y0, y1, y2))), n - 1)
        if lo_x > hi_x or lo_y > hi_y:
            continue
        xs = np.arange(lo_x, hi_x + 1, dtype=np.float32)
        ys = np.arange(lo_y, hi_y + 1, dtype=np.float32)
        gx, gy = np.meshgrid(xs, ys)
        w0 = ((y1 - y2) * (gx - x2) + (x2 - x1) * (gy - y2)) / den
        w1 = ((y2 - y0) * (gx - x2) + (x0 - x2) * (gy - y2)) / den
        w2 = 1.0 - w0 - w1
        eps = -1e-6                                # keep shared edges covered
        inside = (w0 >= eps) & (w1 >= eps) & (w2 >= eps)
        if not inside.any():
            continue
        c = (w0[inside, None] * cols[i0]
             + w1[inside, None] * cols[i1]
             + w2[inside, None] * cols[i2])
        sub = img[lo_y:hi_y + 1, lo_x:hi_x + 1]
        sub[inside] = c
        filled[lo_y:hi_y + 1, lo_x:hi_x + 1] |= inside

    if supersample > 1:
        s = int(supersample)
        img = img.reshape(size, s, size, s, 3).mean(axis=(1, 3))
        filled = filled.reshape(size, s, size, s).any(axis=(1, 3))
    return img, filled


def dilate(img, filled, iterations=4):
    """Bleed colour outward into unfilled texels.

    Bilinear filtering samples slightly outside each UV island, so without a
    few texels of padding every island edge shows a dark seam. Each pass
    fills an empty texel with the mean of its filled neighbours.

    All 8 neighbours, not just 4: with 4-connectivity a diagonal gap needs
    twice as many passes to close, which leaves unfilled texels at island
    corners exactly where two edges meet."""
    img = img.copy()
    filled = filled.copy()
    offs = ((1, 0), (-1, 0), (0, 1), (0, -1),
            (1, 1), (1, -1), (-1, 1), (-1, -1))
    for _ in range(max(0, int(iterations))):
        if filled.all():
            break
        acc = np.zeros_like(img)
        cnt = np.zeros(filled.shape, np.float32)
        for dy, dx in offs:
            sh = np.roll(np.where(filled[..., None], img, 0.0), (dy, dx),
                         axis=(0, 1))
            shm = np.roll(filled, (dy, dx), axis=(0, 1)).astype(np.float32)
            acc += sh
            cnt += shm
        new = (~filled) & (cnt > 0)
        if not new.any():
            break
        img[new] = acc[new] / cnt[new][:, None]
        filled |= new
    return img


# --------------------------------------------------------------- Blender glue

def mesh_arrays(obj):
    """Pull (uv_per_vertex, triangles, colours) out of a mesh.

    UVs live per LOOP, colours per POINT, so a vertex shared by two islands
    has two different UVs. Splitting those properly would mean rebuilding the
    mesh; instead the last loop wins, which is correct everywhere except the
    handful of vertices sitting exactly on a seam - and the dilation pass
    covers those."""
    me = obj.data
    me.calc_loop_triangles()
    nv = len(me.vertices)
    uvl = me.uv_layers.active
    if uvl is None:
        raise RuntimeError("mesh has no UV layer")

    loop_uv = np.empty(len(me.loops) * 2, np.float32)
    uvl.data.foreach_get("uv", loop_uv)
    loop_uv = loop_uv.reshape(-1, 2)
    loop_v = np.empty(len(me.loops), np.int32)
    me.loops.foreach_get("vertex_index", loop_v)
    uv = np.zeros((nv, 2), np.float32)
    uv[loop_v] = loop_uv

    tris = np.empty(len(me.loop_triangles) * 3, np.int32)
    me.loop_triangles.foreach_get("vertices", tris)
    tris = tris.reshape(-1, 3)

    cols = np.ones((nv, 3), np.float32)
    ca = me.color_attributes.get("Col")
    if ca is not None:
        buf = np.empty(len(ca.data) * 4, np.float32)
        ca.data.foreach_get("color", buf)
        buf = buf.reshape(-1, 4)[:, :3]
        if getattr(ca, "domain", 'POINT') == 'CORNER':
            # One entry per LOOP. Lengths should match, but a mismatch must
            # degrade to partial colour rather than kill the whole bake.
            m = min(len(buf), len(loop_v))
            cols[loop_v[:m]] = buf[:m]
        else:
            m = min(len(buf), nv)
            cols[:m] = buf[:m]
    return uv, tris, cols


def bake_texture(obj, size=2048, supersample=2, padding=4, name=None):
    """UV-rasterise the mesh's vertex colours into a packed image."""
    if bpy is None:
        raise RuntimeError("Blender required")
    uv, tris, cols = mesh_arrays(obj)
    img_arr, filled = rasterize(uv, tris, cols, int(size), supersample)
    coverage = float(filled.mean())
    img_arr = dilate(img_arr, filled, padding)

    nm = (name or obj.name) + "_tex"
    img = bpy.data.images.new(nm, int(size), int(size), alpha=False,
                              float_buffer=True)
    try:
        # The colours are already scene-linear; sRGB would re-encode them.
        img.colorspace_settings.name = 'Non-Color'
    except Exception:
        pass
    rgba = np.ones((int(size), int(size), 4), np.float32)
    rgba[..., :3] = img_arr
    img.pixels.foreach_set(rgba.ravel())
    try:
        # A name on disk, even a relative one, matters for export: OBJ writes
        # an .mtl that points at a FILE, and an unnamed packed image lands
        # there as a blank or "untitled" reference. Setting it here means
        # File > Export > OBJ / glTF writes something usable straight away.
        img.filepath_raw = "//" + nm + ".png"
        img.file_format = 'PNG'
    except Exception:
        pass
    try:
        img.pack()
    except Exception:
        pass
    return img, coverage


def apply_texture(obj, img, emissive=False):
    """Point the object's material at `img` through its UV layer."""
    if bpy is None:
        raise RuntimeError("Blender required")
    me = obj.data
    mat = me.materials[0] if me.materials else None
    if mat is None:
        mat = bpy.data.materials.new(obj.name + "_mat")
        me.materials.append(mat)
    mat.use_nodes = True
    nt = mat.node_tree
    for nd in list(nt.nodes):
        nt.nodes.remove(nd)
    out = nt.nodes.new('ShaderNodeOutputMaterial')
    out.location = (400, 0)
    uvn = nt.nodes.new('ShaderNodeUVMap')
    uvn.uv_map = me.uv_layers.active.name
    uvn.location = (-600, 0)
    tex = nt.nodes.new('ShaderNodeTexImage')
    tex.image = img
    tex.location = (-400, 0)
    nt.links.new(uvn.outputs["UV"], tex.inputs["Vector"])
    if emissive:
        sh = nt.nodes.new('ShaderNodeEmission')
        nt.links.new(tex.outputs["Color"], sh.inputs["Color"])
        nt.links.new(sh.outputs["Emission"], out.inputs["Surface"])
    else:
        sh = nt.nodes.new('ShaderNodeBsdfPrincipled')
        nt.links.new(tex.outputs["Color"], sh.inputs["Base Color"])
        try:
            sh.inputs["Roughness"].default_value = 0.9
            sh.inputs["Specular IOR Level"].default_value = 0.1
        except Exception:
            pass
        nt.links.new(sh.outputs["BSDF"], out.inputs["Surface"])
    sh.location = (0, 0)
    # Make the image the active node so it is the one shown in the UV editor
    # and used by Blender's own bake, if the user wants to refine it later.
    try:
        nt.nodes.active = tex
    except Exception:
        pass
    return mat
