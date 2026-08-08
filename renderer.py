"""The per-instance splat renderer: GPU buffers, sorting, colour, picking."""

import math
import bpy
import gpu
import numpy as np
from mathutils import Matrix, Vector

from .shaders import build_shader, build_point_shader


class SplatRenderer:
    _CORNERS = np.array([[-2, -2], [2, -2], [2, 2], [-2, 2]], dtype=np.float32)
    _TRI = np.array([0, 1, 2, 0, 2, 3], dtype=np.int32)
    _COS_THRESH = math.cos(math.radians(1.5))
    _MAX_FRAMES = 25
    _shared_shader = None     # one shader shared by every instance
    _shared_point_shader = None

    def __init__(self, data, box_name, rest_inv):
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
        from .loaders import robust_bounds
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
            SplatRenderer._shared_shader = build_shader()
        self.shader = SplatRenderer._shared_shader
        self._perm = np.random.default_rng(0).permutation(self.N)
        self._tris_tmpl = None        # (N,6) index template, built lazily
        self._tris_buf = None
        self._sorted_cz = None        # per-splat view depth, ascending
        self._sorted_m = 0
        self._slabs = None            # per-depth-slab batches (multi-model)
        self._slab_edges = None
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
        self._build_vbo(data)
        self._sort_precise = True
        self._resort(np.eye(4, dtype=np.float32), 100.0)

    # -- buffers -------------------------------------------------------
    def _build_vbo(self, data):
        N = self.N
        rep = lambda a: np.repeat(a, 4, axis=0)
        fmt = gpu.types.GPUVertFormat()
        fmt.attr_add(id="corner", comp_type='F32', len=2, fetch_mode='FLOAT')
        fmt.attr_add(id="center", comp_type='F32', len=3, fetch_mode='FLOAT')
        fmt.attr_add(id="col", comp_type='F32', len=3, fetch_mode='FLOAT')
        fmt.attr_add(id="opacity", comp_type='F32', len=1, fetch_mode='FLOAT')
        fmt.attr_add(id="scl", comp_type='F32', len=3, fetch_mode='FLOAT')
        fmt.attr_add(id="quat", comp_type='F32', len=4, fetch_mode='FLOAT')

        vbo = gpu.types.GPUVertBuf(fmt, len=4 * N)
        vbo.attr_fill("corner", np.tile(self._CORNERS, (N, 1)))
        vbo.attr_fill("center", rep(data["xyz"]))
        vbo.attr_fill("col", rep(data["rgb"]))
        vbo.attr_fill("opacity", rep(data["opacity"].reshape(-1, 1)).reshape(-1))
        vbo.attr_fill("scl", rep(data["scale"]))
        vbo.attr_fill("quat", rep(data["quat"]))
        self.vbo = vbo

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

    def frustum_mask(self, mvp):
        """Boolean over ALL splats: which could be on screen.

        Deliberately written as three contiguous 1-D passes over the position
        columns. The obvious version - building an (N, 4) clip-space array -
        is several times slower because every write is strided, and it must
        stay well under the cost of the sort it is meant to save.

        Only x, y and w are needed; z is left to the GPU's near/far clipping.
        A per-splat radius margin is included, because a large splat whose
        CENTRE is off screen can still be visible - a captured sky is a few
        splats hundreds of units across, and culling those by centre makes the
        background flicker at the frame edge.
        """
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
                if p.get("lod_points") and d > 30.0 and mode == 'SPLAT':
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
        s.uniform_sampler("sh_tex",
                          self.sh_tex if self.sh_tex is not None
                          else SplatRenderer._dummy_sh_tex())
        b = batch_override if batch_override is not None else self.batch
        if b is not None:
            b.draw(s)

        gpu.state.blend_set('NONE')
        gpu.state.depth_test_set('NONE')
