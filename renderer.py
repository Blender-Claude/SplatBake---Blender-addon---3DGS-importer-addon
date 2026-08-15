"""The per-instance splat renderer: GPU buffers, sorting, colour, picking."""

# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Blender-Claude


import math
import bpy
import gpu
import numpy as np
from mathutils import Matrix, Vector

from .shaders import build_shader, build_point_shader


# Why the preview reports its own state: every failure mode here is silent.
# No lamp, wrong display mode, a lamp type that yields nothing - each looks
# identical from the viewport (nothing happens), so the reason is recorded and
# surfaced in the sidebar instead of leaving the user to guess.
PREVIEW_STATUS = "off"


def gather_preview_lights(context, model_inv, limit=4):
    """Scene lights, converted into a model's local space, for the shader.

    The shader works in MODEL space - `center` is a local coordinate and the
    matrix it receives is already model x view. Rather than send another mat4
    and transform millions of splats on the GPU, the handful of lights are
    transformed once here on the CPU.

    Returns (list of (x,y,z,type) tuples, list of (r,g,b,0) tuples), each
    padded to `limit`, plus how many are real.

    NOTE ON VISIBILITY: `visible_get()` is deliberately NOT used. It needs a
    view-layer context, and this runs inside a draw handler where that is
    restricted - it can raise, and when it did the whole gather was abandoned
    and the preview silently reported no lights at all. `hide_viewport` and
    `hide_get()` are plain attributes and safe here.

    Energy uses the SAME conversion the baked material gets from EEVEE, times
    the shared Light Gain (1.20.12). It used to be arbitrary - 0.5x a sun's
    strength, 0.0025x a lamp's watts - chosen to look sane, which meant the
    preview and a bake could never agree no matter how either was tuned. The
    physical conversions are:

        sun          irradiance E = strength        -> radiance E / pi
        point/area   E = P / (4 pi d^2)             -> radiance E / pi
        spot         as point, inside the cone

    The shader divides by d^2 itself, so the point/spot/area constant folded
    in here is 1 / (4 pi^2). What is left is a plain diffuse response, which
    is what the lit bake computes as well - so the same lamp now reads the
    same in both, and Light Gain moves both together.
    """
    gain = 3.0
    try:
        gain = float(getattr(context.scene, "fgs_lit_preview_gain", 3.0))
    except Exception:
        pass
    _SUN_K = 0.3183098861837907          # 1 / pi
    _PT_K = 0.02533029591058444          # 1 / (4 * pi^2)
    global PREVIEW_STATUS
    pos = []
    col = []
    aims = []
    lamps = []
    try:
        for ob in context.scene.objects:
            if ob.type != 'LIGHT':
                continue
            try:
                if ob.hide_viewport or ob.hide_get():
                    continue
            except Exception:
                pass
            lamps.append(ob)
    except Exception as e:
        PREVIEW_STATUS = "could not read scene lights"
        lamps = []

    # With more than `limit` lamps, take the ones that actually matter rather
    # than whichever happen to come first in the scene. Rough contribution:
    # suns are global, everything else falls off with distance.
    try:
        origin = model_inv.inverted().translation
        def _weight(o):
            ld = o.data
            e = abs(float(getattr(ld, "energy", 1.0)))
            if ld.type == 'SUN':
                return 1e9 + e
            d2 = max((o.matrix_world.translation - origin).length_squared, 1e-4)
            return e / d2
        lamps.sort(key=_weight, reverse=True)
    except Exception:
        pass

    for ob in lamps[:limit]:
        try:
            ld = ob.data
            c = tuple(ld.color)
            e = float(getattr(ld, "energy", 1.0))
            aim = (0.0, 0.0, -1.0, -1.0)
            cos_inner = 0.0
            if ld.type == 'SUN':
                # A lamp's local -Z points where it shines, so +Z is the
                # direction TO the light, which is what the shader wants.
                d = ob.matrix_world.to_3x3() @ Vector((0.0, 0.0, 1.0))
                d = (model_inv.to_3x3() @ d).normalized()
                pos.append((d.x, d.y, d.z, 0.0))
                # Sun strength is irradiance, already independent of distance.
                k = e * _SUN_K * gain
            else:
                p = model_inv @ ob.matrix_world.translation
                if ld.type == 'SPOT':
                    # Direction the lamp points, i.e. local -Z.
                    a = ob.matrix_world.to_3x3() @ Vector((0.0, 0.0, -1.0))
                    a = (model_inv.to_3x3() @ a).normalized()
                    size = float(getattr(ld, "spot_size", 1.5))      # full angle
                    blend = float(getattr(ld, "spot_blend", 0.15))
                    cos_outer = math.cos(min(size * 0.5, 3.14159 * 0.5))
                    # Blender's blend widens the soft edge inward from the rim.
                    cos_inner = math.cos(
                        min(size * 0.5 * (1.0 - blend), 3.14159 * 0.5))
                    aim = (a.x, a.y, a.z, cos_outer)
                    pos.append((p.x, p.y, p.z, 2.0))
                else:
                    # POINT and AREA share the inverse-square treatment. An
                    # area lamp is really an emitting surface, so a point
                    # approximation makes it harsher than it should be - the
                    # softness slider is the practical compensation, and the
                    # bake is what to trust for the real falloff.
                    pos.append((p.x, p.y, p.z, 1.0))
                # Divided by distance squared in the shader, so the watts
                # carry the 1/(4 pi) intensity conversion and the 1/pi
                # diffuse response only.
                k = e * _PT_K * gain
            col.append((c[0] * k, c[1] * k, c[2] * k, cos_inner))
            aims.append(aim)
        except Exception:
            continue

    n = len(col)
    while len(pos) < limit:
        pos.append((0.0, 0.0, 1.0, 0.0))
        col.append((0.0, 0.0, 0.0, 0.0))
        aims.append((0.0, 0.0, -1.0, -1.0))
    if n == 0 and PREVIEW_STATUS not in ("could not read scene lights",):
        PREVIEW_STATUS = "no lights in scene"
    return pos, col, aims, n


class SplatRenderer:
    _CORNERS = np.array([[-2, -2], [2, -2], [2, 2], [-2, 2]], dtype=np.float32)
    _TRI = np.array([0, 1, 2, 0, 2, 3], dtype=np.int32)
    _COS_THRESH = math.cos(math.radians(1.5))
    _MAX_FRAMES = 25
    _shared_shader = None     # one shader shared by every instance
    _shader_has_lighting = True
    _shader_error = ""
    _shared_point_shader = None

    def __init__(self, data, box_name, rest_inv, share_from=None):
        """`share_from` makes this an INSTANCE of an existing model.

        The vertex buffer holds each splat's position, colour, opacity, scale
        and rotation in the model's own data space - the world transform is a
        shader uniform applied at draw time. So two copies standing in
        different places can point at the same buffer, and the same spherical
        harmonic texture.

        That matters because the buffer is the expensive part: four vertices
        of sixteen floats per splat is 256 bytes, so a duplicate of a 9M-splat
        scene would otherwise cost another 2.3 GB of GPU memory. What stays
        per-copy is small - the transform, the alive mask, and the index
        buffer, which must differ anyway because each copy sorts to its own
        depth order.
        """
        self.N = len(data["xyz"])
        self.centers = data["xyz"]
        mn = self.centers.min(axis=0)
        mx = self.centers.max(axis=0)
        self._aabb = np.array(
            [[x, y, z] for x in (mn[0], mx[0])
                       for y in (mn[1], mx[1])
                       for z in (mn[2], mx[2])], dtype=np.float32)  # 8 corners
        # Outlier-trimmed bounds: sky/floater splats must not inflate framing,
        # the transform handle, or the depth-sort pivot. _aabb above stays on
        # the FULL bounds so frustum culling remains conservative.
        from .splatcore.loaders import robust_bounds
        tlo, thi = robust_bounds(self.centers)
        self.center_local = Vector(((tlo + thi) * 0.5).tolist())  # sort pivot
        self.half = Vector(((thi - tlo) * 0.5).tolist())          # for offsets
        self._tight_corners = np.array(
            [[x, y, z] for x in (tlo[0], thi[0])
                       for y in (tlo[1], thi[1])
                       for z in (tlo[2], thi[2])], dtype=np.float32)
        self.source = data            # kept (by reference) so we can duplicate
        # Per-splat cull radius (world units), for frustum culling. A splat is
        # a disc, not a point: culling by centre alone would pop the sky, whose
        # splats can be hundreds of units across. Computed once here so the
        # per-frame test is a single vectorised comparison.
        try:
            _sc = np.sort(np.maximum(data["scale"].astype(np.float32), 0.0),
                          axis=1)
            self.cull_r = (0.5 * (_sc[:, 2] + _sc[:, 1]) * 3.0).astype(np.float32)
        except Exception:
            self.cull_r = None
        self.box_name = box_name      # proxy Empty drives the transform
        self.rest_inv = rest_inv
        if SplatRenderer._shared_shader is None:
            # Try the lighting-capable shader first; if the GPU cannot afford
            # its extra uniforms, lose the preview rather than the viewer.
            try:
                SplatRenderer._shared_shader = build_shader(lighting=True)
                SplatRenderer._shader_has_lighting = True
            except Exception as e:
                # This fallback hid a real bug once: a shader that failed to
                # compile fell back silently, so the preview just did nothing
                # and looked like a logic error rather than a build error.
                # Keep the message so the next failure is findable.
                SplatRenderer._shader_error = str(e)
                print("[SplatBake] LIGHTING SHADER FAILED TO COMPILE:", e)
                print("[SplatBake] falling back to the unlit shader; "
                      "the lighting preview will do nothing.")
                SplatRenderer._shared_shader = build_shader(lighting=False)
                SplatRenderer._shader_has_lighting = False
        self.shader = SplatRenderer._shared_shader
        self._perm = np.random.default_rng(0).permutation(self.N)
        self._tris_tmpl = None        # (N,6) index template, built lazily
        self._tris_buf = None
        self._sorted_cz = None        # per-splat view depth, ascending
        self._sorted_m = 0
        self._slabs = None            # per-depth-slab batches (multi-model)
        self._slab_edges = None
        self._grid = None             # BucketGrid, built on first cull
        self._grid_failed = False     # never retry a build that threw
        self._vis_buf = None          # reused frustum mask, no realloc
        self._last_mv = None          # skip re-sorts when the view is parked
        self._last_fwd = None
        self._last_density = None
        self._frames = 0
        self.alive = np.ones(self.N, dtype=bool)
        self._visible = np.zeros(self.N, dtype=bool)
        self._force = False
        self.base_rgb = data["rgb"]
        self.has_sh = ("sh" in data and "dc" in data)
        self.dc = data.get("dc")
        self.sh = data.get("sh")
        if share_from is not None and getattr(share_from, "sh_tex", None) is not None:
            self.sh_tex = share_from.sh_tex
            self.sh_k = share_from.sh_k
            self.sh_texels = share_from.sh_texels
        else:
            self.sh_tex = None
            self.sh_k = 0
            self.sh_texels = 0
        self.sh_w = 1
        if self.has_sh:
            self._build_sh_texture()
        self._alive_ver = 0
        self._pts_vbo = None
        self._pts_batch = None
        self._pts_key = None
        self._build_vbo(data, share_from)
        self._sort_precise = True
        self._resort(np.eye(4, dtype=np.float32), 100.0)

    # -- buffers -------------------------------------------------------
    def _build_vbo(self, data, share_from=None):
        N = self.N
        rep = lambda a: np.repeat(a, 4, axis=0)
        fmt = gpu.types.GPUVertFormat()
        fmt.attr_add(id="corner", comp_type='F32', len=2, fetch_mode='FLOAT')
        fmt.attr_add(id="center", comp_type='F32', len=3, fetch_mode='FLOAT')
        fmt.attr_add(id="col", comp_type='F32', len=3, fetch_mode='FLOAT')
        fmt.attr_add(id="opacity", comp_type='F32', len=1, fetch_mode='FLOAT')
        fmt.attr_add(id="scl", comp_type='F32', len=3, fetch_mode='FLOAT')
        fmt.attr_add(id="quat", comp_type='F32', len=4, fetch_mode='FLOAT')

        if share_from is not None and getattr(share_from, "vbo", None) is not None:
            # Instance: point at the donor's buffer rather than uploading a
            # second copy. Keeping a reference also keeps it alive if the
            # original model is later deleted.
            self.vbo = share_from.vbo
            self._shared_with = share_from
        else:
            vbo = gpu.types.GPUVertBuf(fmt, len=4 * N)
            vbo.attr_fill("corner", np.tile(self._CORNERS, (N, 1)))
            vbo.attr_fill("center", rep(data["xyz"]))
            vbo.attr_fill("col", rep(data["rgb"]))
            vbo.attr_fill("opacity",
                          rep(data["opacity"].reshape(-1, 1)).reshape(-1))
            vbo.attr_fill("scl", rep(data["scale"]))
            vbo.attr_fill("quat", rep(data["quat"]))
            self.vbo = vbo
            self._shared_with = None

    def visible_count(self, density):
        return max(1, min(self.N, int(self.N * density / 100.0)))

    def model_matrix(self):
        """The model's world transform, driven by its proxy box Empty."""
        box = bpy.data.objects.get(self.box_name) if self.box_name else None
        if box is not None:
            return box.matrix_world @ self.rest_inv
        return Matrix.Identity(4)

    def visible_in_frustum(self, vp):
        """True if this model's bounding box might be on screen. Conservative:
        culls only when all 8 corners fall outside the same clip plane."""
        mvp = np.array(vp @ self.model_matrix(), dtype=np.float32)
        h = np.concatenate([self._aabb, np.ones((8, 1), np.float32)], axis=1) @ mvp.T
        x, y, z, w = h[:, 0], h[:, 1], h[:, 2], h[:, 3]
        if np.all(x > w) or np.all(x < -w):
            return False
        if np.all(y > w) or np.all(y < -w):
            return False
        if np.all(z > w) or np.all(z < -w):
            return False
        return True

    # -- sorting / colour ---------------------------------------------
    _COARSE_BUCKETS = 256

    def _depth_keys(self, cz, coarse):
        """Sort keys for back-to-front blending. `cz` ascends from far to near.

        PRECISE (camera at rest): 32-bit keys, effectively exact ordering.

        COARSE (camera moving): only 256 depth buckets. Sorting few distinct
        values is dramatically faster - measured 138 ms against 1616 ms on a
        9.35M-splat scene - because the radix histogram stays in cache and
        needs fewer passes. Splats inside one bucket blend in arbitrary order,
        which is invisible while the view is actually moving, and the exact
        sort runs the moment it stops.

        The buckets are spaced by 1/distance, not uniformly. Two splats a
        given distance apart separate on screen in proportion to 1/d^2, so
        uniform buckets would spend most of their precision on distant
        geometry that is sub-pixel anyway. This way the near field - where
        blend order is actually visible - gets the fine buckets.
        """
        if len(cz) == 0:
            return np.zeros(0, np.uint16 if coarse else np.uint32)
        lo = float(cz.min())
        span = float(cz.max()) - lo
        if not coarse:
            return ((cz - lo) * np.float32(4294967040.0 / max(span, 1e-9))
                    ).astype(np.uint32)
        if span <= 1e-9:
            return np.zeros(len(cz), np.uint16)
        # Buckets spaced logarithmically in distance, which makes the depth
        # error a constant FRACTION of distance rather than a constant number
        # of units. Uniform buckets are far too coarse close up, where splats
        # are large on screen and overlap heavily; 1/d spacing overcorrects
        # and leaves the far field badly ordered. Log sits between the two and
        # matches how depth error actually shows up on screen.
        d = (lo + span) - cz                       # 0 near, `span` far
        d += np.float32(span * 0.02)               # keep log() finite
        k = np.log(d, dtype=np.float32)
        k0 = np.float32(np.log(span * 0.02))
        k1 = np.float32(np.log(span * 1.02))
        k -= k0
        k *= np.float32((self._COARSE_BUCKETS - 1) / max(float(k1 - k0), 1e-9))
        # log(d) rises from near to far, but keys must ASCEND far-to-near
        return (np.float32(self._COARSE_BUCKETS - 1) - k).astype(np.uint16)

    # A grid is only worth building when there are enough splats for the
    # per-splat test to hurt. Below this the exact test is already trivial and
    # the build would cost more than it ever saves.
    _GRID_MIN_SPLATS = 250_000

    def _ensure_grid(self):
        """Build the bucket grid on first use, once."""
        if self._grid is not None or self._grid_failed:
            return self._grid
        if self.N < self._GRID_MIN_SPLATS:
            self._grid_failed = True
            return None
        try:
            from .splatcore.spatial import BucketGrid
            self._grid = BucketGrid(self.centers, self.cull_r)
        except Exception as e:
            # A failed grid must never cost the user their viewport: fall back
            # to the exact per-splat test, which is slower but always correct.
            print("[SplatBake] bucket grid unavailable, exact cull:", e)
            self._grid_failed = True
            self._grid = None
        return self._grid

    def frustum_mask(self, mvp):
        """Boolean over ALL splats: which could be on screen.

        Routed through a bucket grid when the model is big enough to warrant
        one (see spatial.py). The grid tests a few thousand bucket centres and
        gathers the result back onto splats with one integer take, instead of
        projecting every splat every frame. Measured on a clustered 9.35M
        scene: 21 ms against 180-280 ms, and no 112 MB temporary per frame.
        The grid is conservative, so it keeps a few splats the exact test
        would drop - cheap, since they only reach the sort - but it never
        drops one the exact test keeps.

        The exact path below is the fallback for small models and for any
        model whose grid failed to build. It is deliberately three contiguous
        1-D passes over the position columns: the obvious version, building an
        (N, 4) clip-space array, is several times slower because every write
        is strided.

        Only x, y and w are needed; z is left to the GPU's near/far clipping.
        A per-splat radius margin is included, because a large splat whose
        CENTRE is off screen can still be visible - a captured sky is a few
        splats hundreds of units across, and culling those by centre makes the
        background flicker at the frame edge.
        """
        grid = self._ensure_grid()
        if grid is not None:
            if self._vis_buf is None or len(self._vis_buf) != self.N:
                self._vis_buf = np.empty(self.N, bool)
            return grid.frustum_mask(mvp, out=self._vis_buf)
        # One BLAS matmul for the three clip components we need, then the
        # comparisons fused with out= so the temporaries stay off the heap.
        M = np.ascontiguousarray(mvp[:3][:, [0, 1, 3]])
        h = self.centers @ M
        h += mvp[3][[0, 1, 3]]
        x, y, w = h[:, 0], h[:, 1], h[:, 2]
        if self.cull_r is not None:
            m = self.cull_r * np.float32(
                0.5 * (abs(float(mvp[0, 0])) + abs(float(mvp[1, 1]))))
        else:
            m = np.float32(0.0)
        lim = np.abs(w)
        lim += m
        keep = np.greater(w, -m)
        np.logical_and(keep, np.less_equal(np.abs(x), lim), out=keep)
        np.logical_and(keep, np.less_equal(np.abs(y), lim), out=keep)
        return keep

    def _resort(self, mv, density, mvp=None, coarse=False):
        K = self.visible_count(density)
        idx = self._perm[:K]
        idx = idx[self.alive[idx]]
        if mvp is not None and len(idx):
            # Cull BEFORE sorting. The sort is the dominant per-frame cost and
            # scales worse than linearly, so removing off-screen splats here is
            # worth far more than the ~3 ms the mask costs.
            try:
                vis = self.frustum_mask(mvp)
                sub = vis[idx]
                if sub.any():
                    idx = idx[sub]
            except Exception as e:
                print("[SplatBake] frustum cull skipped:", e)
        self._visible[:] = False
        self._visible[idx] = True
        if len(idx) == 0:
            self._sorted_cz = None
            self._sorted_m = 0
            self._slabs = None
            self.ibo = gpu.types.GPUIndexBuf(
                type='TRIS', seq=np.zeros((1, 3), np.int32))
            self.batch = gpu.types.GPUBatch(type='TRIS', buf=self.vbo, elem=self.ibo)
            return
        row2 = mv[2]
        cz = (self.centers[idx, 0] * row2[0] + self.centers[idx, 1] * row2[1]
              + self.centers[idx, 2] * row2[2] + row2[3])
        # 32-bit quantised radix sort: numpy's stable sort on unsigned ints is
        # a radix pass (still ~2x a float comparison sort). Scale to the
        # largest float32-exact value BELOW the uint32 ceiling - 4294967295
        # itself rounds UP in float32 and the cast overflows. Effective key
        # resolution is ~2^24 (float32 mantissa): micron-scale on any scene,
        # so near-coplanar detail splats keep their true depth order instead
        # of tying arbitrarily inside a bucket the way 16-bit keys did.
        a = np.argsort(self._depth_keys(cz, coarse), kind='stable')
        order = idx[a]
        self._sorted_cz = cz[a]          # ascending = farthest first
        m = len(order)
        if self._tris_tmpl is None:
            self._tris_tmpl = ((np.arange(self.N, dtype=np.int32) * 4)[:, None]
                               + self._TRI[None, :])          # (N, 6)
            self._tris_buf = np.empty((self.N, 6), np.int32)
        np.take(self._tris_tmpl, order, axis=0, out=self._tris_buf[:m])
        self._sorted_m = m
        self.ibo = gpu.types.GPUIndexBuf(
            type='TRIS', seq=self._tris_buf[:m].reshape(-1, 3))
        self.batch = gpu.types.GPUBatch(type='TRIS', buf=self.vbo, elem=self.ibo)
        self._slabs = None               # invalidated by the new order

    _dummy_sh = None

    @classmethod
    def _dummy_sh_tex(cls):
        if cls._dummy_sh is None:
            buf = gpu.types.Buffer('FLOAT', 4, [0.0, 0.0, 0.0, 0.0])
            cls._dummy_sh = gpu.types.GPUTexture((1, 1), format='RGBA32F',
                                                 data=buf)
        return cls._dummy_sh

    def _build_sh_texture(self, max_k=15):
        """Pack f_rest channel-major into an RGBA32F texture so the vertex
        shader evaluates view-dependent colour live (like the source viewer)."""
        try:
            K0 = int(self.sh.shape[1])
            for k in (15, 8, 3):
                if k > K0 or k > max_k:
                    continue
                texels = (3 * k + 3) // 4
                done = False
                for W in (1024, 2048, 4096):
                    hbase = (self.N + W - 1) // W
                    if hbase * texels <= 8192:
                        done = True
                        break
                if done:
                    break
            else:
                return
            flat = np.ascontiguousarray(
                self.sh[:, :k, :].transpose(0, 2, 1)).reshape(self.N, 3 * k)
            padded = np.zeros((self.N, texels * 4), np.float32)
            padded[:, :3 * k] = flat
            data = np.zeros((hbase * texels, W, 4), np.float32)
            xs = np.arange(self.N) % W
            ys = (np.arange(self.N) // W) * texels
            for t in range(texels):
                data[ys + t, xs, :] = padded[:, t * 4:(t + 1) * 4]
            buf = gpu.types.Buffer('FLOAT', data.size, data.ravel())
            self.sh_tex = gpu.types.GPUTexture((W, hbase * texels),
                                               format='RGBA32F', data=buf)
            self.sh_k = k
            self.sh_texels = texels
            self.sh_w = W
        except Exception as e:
            print("[SplatBake] SH texture failed (base colour only):", e)
            self.sh_tex = None
            self.sh_k = 0

    def _maybe_sort(self, mv_np, density, hq, mvp=None,
                    adaptive=False):
        fwd = -mv_np[2, :3]
        ln = np.linalg.norm(fwd)
        if ln > 0:
            fwd = fwd / ln
        self._frames += 1
        # Blender redraws on every UI event; when the camera has not moved the
        # previous order is still exact, so skip the whole sort (huge win).
        # A culled sort depends on the FULL camera transform, not just its
        # direction, so panning must re-sort even though the facing is
        # unchanged. Without this the culled set goes stale and geometry
        # disappears as you move sideways.
        culling = mvp is not None
        if culling != getattr(self, "_was_culling", None):
            self._was_culling = culling
            self._force = True
        parked = (self._last_mv is not None
                  and density == self._last_density
                  and not self._force
                  and np.allclose(mv_np, self._last_mv, atol=1e-6))
        if parked:
            # The camera has come to rest. If the last sort was the fast
            # approximate one, upgrade it now - so an approximate blend order
            # only ever exists while the view is actually moving, and whatever
            # you settle on to look at is exactly sorted.
            if adaptive and not self._sort_precise:
                self._sort_precise = True
                self._resort(mv_np, density, mvp, coarse=False)
            return
        moved = (self._last_fwd is None) or (density != self._last_density) \
            or self._force
        if not moved:
            dot = float(np.clip(np.dot(fwd, self._last_fwd), -1.0, 1.0))
            moved = (dot < self._COS_THRESH) or (self._frames >= self._MAX_FRAMES)
            if culling and not moved and self._last_mv is not None:
                # Position changed but facing did not: still needs a re-cull.
                moved = not np.allclose(mv_np[:, 3], self._last_mv[:, 3],
                                        atol=1e-4)
        if moved or hq:
            self._force = False
            self._last_fwd = fwd
            self._last_density = density
            self._last_mv = mv_np.copy()
            self._frames = 0
            # While the view is changing, the cheap sort is enough; the block
            # above replaces it with the exact one as soon as it settles.
            coarse = bool(adaptive)
            self._sort_precise = not coarse
            self._resort(mv_np, density, mvp, coarse=coarse)

    # -- editing -------------------------------------------------------
    def alive_count(self):
        return int(self.alive.sum())

    def pick(self, region, rv3d, mx, my, radius=12.0, coarse=False):
        """Front-most splat within radius px of cursor -> (depth, id).

        coarse=True decimates huge clouds to ~200k test points: picking a
        MODEL only needs some splat near the cursor, not the exact one, and
        this keeps every click instant on multi-million-splat scenes. Splat
        deletion keeps coarse=False so the precise id comes back."""
        step = max(1, self.N // 200000) if coarse else 1
        centers = self.centers[::step] if step > 1 else self.centers
        alive = self.alive[::step] if step > 1 else self.alive
        mvp = np.array(rv3d.window_matrix @ rv3d.view_matrix @ self.model_matrix(),
                       dtype=np.float32)
        homog = np.concatenate(
            [centers, np.ones((len(centers), 1), np.float32)], axis=1)
        clip = homog @ mvp.T
        w = clip[:, 3]
        valid = w > 1e-6
        wsafe = np.where(valid, w, 1.0)
        sx = (clip[:, 0] / wsafe * 0.5 + 0.5) * region.width
        sy = (clip[:, 1] / wsafe * 0.5 + 0.5) * region.height
        d2 = (sx - mx) ** 2 + (sy - my) ** 2
        cand = valid & alive & (d2 < radius * radius)
        ids = np.where(cand)[0]
        if len(ids) == 0:
            return None
        hit = int(ids[np.argmin(w[ids])])
        return float(w[hit]), hit * step

    def ray_hits_bounds(self, region, rv3d, mx, my):
        """Distance along the cursor ray to this model's outlier-trimmed
        bounding box, or None if the ray misses. The slab test runs in the
        model's LOCAL frame (ray transformed by the inverse model matrix), so
        any rotation / non-uniform scale on the proxy box is handled, and the
        returned t is the WORLD-space ray parameter - directly comparable
        across models. A camera inside the box returns 0.0 (still a hit)."""
        from bpy_extras import view3d_utils
        o = view3d_utils.region_2d_to_origin_3d(region, rv3d, (mx, my))
        d = view3d_utils.region_2d_to_vector_3d(region, rv3d, (mx, my))
        try:
            Minv = self.model_matrix().inverted()
        except Exception:
            return None
        ol = Minv @ o                      # local origin
        dl = Minv.to_3x3() @ d             # local direction, same t as world
        tc = self._tight_corners
        lo = tc.min(axis=0)
        hi = tc.max(axis=0)
        t0, t1 = 0.0, 1e30
        for a in range(3):
            da = float(dl[a])
            oa = float(ol[a])
            if abs(da) < 1e-12:            # ray parallel to this slab
                if oa < lo[a] or oa > hi[a]:
                    return None
                continue
            ta = (float(lo[a]) - oa) / da
            tb = (float(hi[a]) - oa) / da
            if ta > tb:
                ta, tb = tb, ta
            t0 = max(t0, ta)
            t1 = min(t1, tb)
            if t0 > t1:
                return None
        return t0

    def kill(self, splat_id):
        self.alive[splat_id] = False
        self._force = True
        self._alive_ver += 1

    def revive(self, splat_id):
        self.alive[splat_id] = True
        self._force = True
        self._alive_ver += 1

    def restore_all(self):
        self.alive[:] = True
        self._force = True
        self._alive_ver += 1

    # -- point-cloud modes --------------------------------------------
    def _ensure_points_batch(self, density):
        key = (density, self._alive_ver)
        if self._pts_batch is not None and self._pts_key == key:
            return
        K = self.visible_count(density)
        idx = self._perm[:K]
        idx = idx[self.alive[idx]]
        self._pts_key = key
        if len(idx) == 0:
            self._pts_batch = None
            return
        fmt = gpu.types.GPUVertFormat()
        fmt.attr_add(id="center", comp_type='F32', len=3, fetch_mode='FLOAT')
        fmt.attr_add(id="col", comp_type='F32', len=3, fetch_mode='FLOAT')
        vbo = gpu.types.GPUVertBuf(fmt, len=len(idx))
        vbo.attr_fill("center", np.ascontiguousarray(self.centers[idx]))
        vbo.attr_fill("col", np.ascontiguousarray(self.base_rgb[idx]))
        self._pts_vbo = vbo
        self._pts_batch = gpu.types.GPUBatch(type='POINTS', buf=vbo)

    # -- global (cross-model) ordering ---------------------------------
    def depth_range(self):
        """(near..far) view-depth span of this model's visible splats."""
        if self._sorted_cz is None or self._sorted_m == 0:
            return None
        cz = self._sorted_cz[:self._sorted_m]
        return (float(cz[0]), float(cz[-1]))

    def build_slabs(self, edges):
        """Split this model's already depth-sorted splats into the global
        slabs defined by `edges` (ascending = far -> near). Each slab is a
        contiguous slice of the sorted order, so this is just a searchsorted
        plus one index buffer per non-empty slab."""
        if (self._slabs is not None and self._slab_edges is not None
                and len(self._slab_edges) == len(edges)
                and np.allclose(self._slab_edges, edges)):
            return                        # unchanged: reuse last frame's
        self._slab_edges = np.asarray(edges, np.float32).copy()
        self._slabs = []
        if self._sorted_cz is None or self._sorted_m == 0:
            return
        cz = self._sorted_cz[:self._sorted_m]
        # side='right': a splat sitting exactly ON an edge belongs to the
        # slab that ends there, not the one that starts there. With 'left' a
        # model's furthest splat fell into the next (possibly huge) slab and
        # was drawn far out of order.
        bounds = np.searchsorted(cz, edges, side='right')
        for i in range(len(edges) - 1):
            lo, hi = int(bounds[i]), int(bounds[i + 1])
            if hi <= lo:
                self._slabs.append(None)
                continue
            tris = self._tris_buf[lo:hi].reshape(-1, 3)
            ibo = gpu.types.GPUIndexBuf(type='TRIS', seq=tris)
            self._slabs.append(
                gpu.types.GPUBatch(type='TRIS', buf=self.vbo, elem=ibo))

    def prewarm(self, rv3d, density):
        """Do the expensive one-time work up front: compile the point shader,
        build the point batch, run the first depth sort and upload the index
        buffer. Without this the reveal's first frames pay for all of it and
        stutter -- the animation itself is purely cosmetic, so none of this
        needs to happen 'live'."""
        try:
            if SplatRenderer._shared_point_shader is None:
                SplatRenderer._shared_point_shader = build_point_shader()
            self._ensure_points_batch(density)
            mv = np.array(rv3d.view_matrix @ self.model_matrix(),
                          dtype=np.float32)
            self._resort(mv, density)
            fwd = -mv[2, :3]
            ln = float(np.linalg.norm(fwd))
            self._last_fwd = (fwd / ln) if ln > 1e-9 else fwd
            self._last_mv = mv.copy()
            self._last_density = density
            self._force = False
            self._frames = 0
        except Exception as e:
            print("[SplatBake] pre-warm skipped:", e)

    def _draw_points(self, region, rv3d, p, big, density):
        if SplatRenderer._shared_point_shader is None:
            SplatRenderer._shared_point_shader = build_point_shader()
        self._ensure_points_batch(density)
        if self._pts_batch is None:
            return
        mvp = rv3d.window_matrix @ rv3d.view_matrix @ self.model_matrix()
        size = p.get("point_size", 4.0)
        if not big:
            size = max(1.0, size * 0.4)
        gpu.state.blend_set('NONE')
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.depth_mask_set(True)
        gpu.state.point_size_set(size)
        s = SplatRenderer._shared_point_shader
        s.bind()
        s.uniform_float("mvp", mvp)
        s.uniform_float("exposure", p["exposure"])
        s.uniform_float("saturation", p.get("saturation", 1.0))
        s.uniform_float("gamma", p.get("gamma", 1.0))
        _pt = p.get("tint", (1.0, 1.0, 1.0))
        s.uniform_float("tint_r", _pt[0])
        s.uniform_float("tint_g", _pt[1])
        s.uniform_float("tint_b", _pt[2])
        wc = p.get("wave_c", (0.0, 0.0, 0.0))
        s.uniform_float("wave_r", float(p.get("wave_r", -1.0)))
        s.uniform_float("wave_pr", float(p.get("wave_pr", -1.0)))
        s.uniform_float("wave_x", wc[0])
        s.uniform_float("wave_y", wc[1])
        s.uniform_float("wave_z", wc[2])
        self._pts_batch.draw(s)
        gpu.state.point_size_set(1.0)
        gpu.state.depth_test_set('NONE')
        gpu.state.depth_mask_set(False)

    @staticmethod
    def _lod_factor(d):
        """1.0 within 5 units, ramping to 0.15 at 25+ units from the view."""
        if d <= 5.0:
            return 1.0
        if d >= 25.0:
            return 0.15
        return 1.0 - 0.85 * ((d - 5.0) / 20.0)

    # -- draw ----------------------------------------------------------
    def draw(self, region, rv3d, p, skip_sort=False, batch_override=None):
        mode = p.get("mode", "SPLAT")
        density = p["density"]

        # View-distance level of detail: thin out (and optionally switch to
        # the lightweight point cloud) based on distance from the viewport.
        if p.get("lod"):
            view_pos = p.get("view_pos")
            if view_pos is not None:
                d = (view_pos - (self.model_matrix() @ self.center_local)).length
                density = max(1.0, round(density * self._lod_factor(d)))
                # Distance LOD swaps far models to the point shader, which
                # has no normals and therefore no lighting. On a large scene
                # the model centre is almost always past this threshold, so
                # with the preview on this silently turned the whole capture
                # unlit - the reported "works on small objects, not on big
                # scenes". Lighting preview wins over the LOD swap; it is an
                # explicit, temporary mode and the user asked to see light.
                if (p.get("lod_points") and d > 30.0 and mode == 'SPLAT'
                        and not p.get("lit_preview")):
                    mode = 'POINTS'

        if batch_override is not None:
            # slab draw from the global multi-model path: routing (points
            # mode, LOD point switch, reveal phases) was already decided by
            # the caller -- re-running it here would duplicate point draws
            # once per slab.
            mode = "SPLAT"
            phase = 0
        else:
            phase = int(p.get("wave_phase", 0))
        if batch_override is None and mode == "POINTS":
            # point-cloud mode: stage 1 may sweep the dots in, but nothing
            # ever consumes them (no splat front)
            pp = dict(p)
            pp["wave_r"] = -1.0
            self._draw_points(region, rv3d, pp, big=False, density=density)
            return
        if batch_override is None and phase == 1:
            # stage 1: only the point cloud exists yet
            self._draw_points(region, rv3d, p, big=False, density=density)
            return
        if batch_override is None and phase == 2:
            # stage 2: dots ahead of the splat front, splats growing behind it
            self._draw_points(region, rv3d, p, big=False, density=density)
        model = self.model_matrix()
        modelview = rv3d.view_matrix @ model
        proj = rv3d.window_matrix
        mv_np = np.array(modelview, dtype=np.float32)
        use_sh = bool(p.get("use_sh", True))
        cam_world = rv3d.view_matrix.inverted().translation
        cam_local = model.inverted() @ cam_world     # SH frame = the data frame
        if not skip_sort:
            mvp_np = None
            if p.get("cull_frustum", False):
                mvp_np = np.array(proj @ modelview, dtype=np.float32).T
            self._maybe_sort(mv_np, density, bool(p.get("hq_sort", True)),
                             mvp_np, bool(p.get("adaptive_sort", False)))

        w, h = region.width, region.height
        fx = 0.5 * w * proj[0][0]
        fy = 0.5 * h * proj[1][1]

        gpu.state.blend_set('ALPHA_PREMULT')
        gpu.state.depth_test_set('LESS_EQUAL')   # scene meshes occlude splats
        gpu.state.depth_mask_set(False)          # splats never write depth

        s = self.shader
        s.bind()
        s.uniform_float("view", modelview)
        s.uniform_float("projection", proj)
        s.uniform_float("focal", (fx, fy))
        s.uniform_float("viewport", (float(w), float(h)))
        s.uniform_float("splat_scale", p["splat_scale"])
        s.uniform_float("sharpness", p["sharpness"])
        s.uniform_float("opacity_cutoff", p["opacity_cutoff"])
        s.uniform_float("max_pixels", p["max_pixels"])
        s.uniform_float("antialias", p["antialias"])
        s.uniform_float("min_pixel_size", 2.0)   # web viewer default
        _modes = {"SOFT": 0.0, "V215": 1.0, "PC": 2.0}
        s.uniform_float("pc_kernel",
                        _modes.get(p.get("pc_gaussian", "PC"), 2.0))
        wr = float(p.get("wave_r", -1.0))
        wc = p.get("wave_c", (0.0, 0.0, 0.0))
        s.uniform_float("wave_r", wr)
        s.uniform_float("wave_soft", float(p.get("wave_soft", 1.0)))
        s.uniform_float("wave_x", wc[0])
        s.uniform_float("wave_y", wc[1])
        s.uniform_float("wave_z", wc[2])
        s.uniform_float("exposure", p["exposure"])
        s.uniform_float("saturation", p.get("saturation", 1.0))
        s.uniform_float("gamma", p.get("gamma", 1.0))
        _tint = p.get("tint", (1.0, 1.0, 1.0))
        s.uniform_float("tint_r", _tint[0])
        s.uniform_float("tint_g", _tint[1])
        s.uniform_float("tint_b", _tint[2])
        s.uniform_float("aniso", p.get("aniso", 0.0))
        s.uniform_float("is_persp", 1.0 if rv3d.is_perspective else 0.0)
        # With the view transform on Raw, the buffer behaves like the twin's
        # WebGL canvas: splats accumulate in DISPLAY (sRGB) space -> identical
        # tones. On any other transform, fall back to linearised output.
        s.uniform_float("linearize",
                        0.0 if p.get("view_transform") == 'Raw' else 1.0)
        want = {"OFF": 0, "DEG1": 3, "DEG2": 8, "FULL": 15}.get(
            p.get("sh_quality", "FULL"), 15)
        if want > 0 and self.has_sh:
            eff = min(want, int(self.sh.shape[1]))
            if eff != self.sh_k:
                print(f"[SplatBake] rebuilding SH texture at K={eff}")
                try:
                    self._build_sh_texture(max_k=eff)
                except Exception as e:
                    print("[SplatBake] SH rebuild failed:", e)
        sh_active = use_sh and want > 0 and self.sh_k > 0
        s.uniform_float("sh_on", 1.0 if sh_active else 0.0)
        s.uniform_int("sh_k", self.sh_k)
        s.uniform_int("sh_texels", self.sh_texels)
        s.uniform_int("sh_w", self.sh_w)
        s.uniform_float("cam_x", cam_local.x)
        s.uniform_float("cam_y", cam_local.y)
        s.uniform_float("cam_z", cam_local.z)
        # Live lighting preview. Any failure must not take the viewport with
        # it: on error this falls back to unlit, which is the old behaviour.
        global PREVIEW_STATUS
        lit_mix = 0.0
        from .shaders import MAX_PREVIEW_LIGHTS as _LMAX
        lpos = [(0.0, 0.0, 1.0, 0.0)] * _LMAX
        lcol = [(0.0, 0.0, 0.0, 0.0)] * _LMAX
        laim = [(0.0, 0.0, -1.0, -1.0)] * _LMAX
        lnum = 0
        try:
            scn = bpy.context.scene
            if (bool(getattr(scn, "fgs_lit_preview", False))
                    and SplatRenderer._shader_has_lighting):
                lpos, lcol, laim, lnum = gather_preview_lights(
                    bpy.context, self.model_matrix().inverted(), _LMAX)
                if lnum > 0:
                    lit_mix = float(getattr(scn, "fgs_lit_preview_mix", 1.0))
                    PREVIEW_STATUS = "lighting %d lamp%s" % (
                        lnum, "" if lnum == 1 else "s")
            elif not SplatRenderer._shader_has_lighting:
                PREVIEW_STATUS = ("shader compile failed: %s"
                                  % (SplatRenderer._shader_error or "unknown"))
            else:
                PREVIEW_STATUS = "off"
        except Exception as e:
            PREVIEW_STATUS = "error: %s" % e
            lit_mix = 0.0
        try:
            s.uniform_float("lit_mix", lit_mix)
            s.uniform_float("lit_ambient", float(getattr(
                bpy.context.scene, "fgs_lit_preview_ambient", 0.15)))
            s.uniform_float("lit_wrap", float(getattr(
                bpy.context.scene, "fgs_lit_preview_wrap", 0.5)))
            s.uniform_int("light_count", int(lnum))
            for i in range(len(lpos)):
                s.uniform_float("light_pos%d" % i, lpos[i])
                s.uniform_float("light_col%d" % i, lcol[i])
        except Exception as e:
            PREVIEW_STATUS = "shader has no lighting uniforms (%s)" % e
        s.uniform_sampler("sh_tex",
                          self.sh_tex if self.sh_tex is not None
                          else SplatRenderer._dummy_sh_tex())
        b = batch_override if batch_override is not None else self.batch
        if b is not None:
            b.draw(s)

        gpu.state.blend_set('NONE')
        gpu.state.depth_test_set('NONE')
