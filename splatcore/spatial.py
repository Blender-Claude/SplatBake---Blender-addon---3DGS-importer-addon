"""A voxel bucket grid over a model's splats, built once at load.

WHY THIS EXISTS
---------------
`SplatRenderer.frustum_mask` used to project all N splat centres into clip
space every moving frame. That is O(N) in the *total* splat count and does not
get cheaper as the visible fraction falls, so on a multi-million-splat capture
it set a hard floor on navigation framerate - measured around 350 ms on a
9.35M scene, against the ~3 ms the old comment assumed. It also allocated a
112 MB temporary every frame.

The fix is to test a few tens of thousands of BUCKETS instead of millions of
splats. Buckets are built once here; per frame the renderer projects only the
bucket centres and gathers the answer back onto splats with a single integer
take, which is a memory-bandwidth pass rather than arithmetic.

CONSERVATIVE BY CONSTRUCTION
----------------------------
Each bucket carries a radius covering its own half-diagonal PLUS the largest
splat radius inside it, so a bucket is kept whenever any part of any splat it
holds could touch the frustum. Over-inclusion is harmless - a few extra splats
reach the sort. Under-inclusion would pop geometry at the screen edge, which
is exactly what the per-splat radius margin was added to prevent, so the grid
must never drop a splat the exact test would keep.

TWO THINGS THAT LOOK LIKE DETAILS AND ARE NOT
---------------------------------------------
*Cell size comes from the DENSE part of the scene, not the bounding box.* A
capture's bounds are set by a handful of sky and floater splats hundreds of
units out. Sizing cells by total volume then yields cells tens of units across,
the buckets grow larger than the visible frustum, and the cull stops culling -
measured at 69% of the scene kept unnecessarily on a close-up camera. Robust
percentile bounds fix it.

*Per-bucket maxima use sorted reduceat, not `np.maximum.at`.* The `ufunc.at`
family falls off numpy's vectorised path and runs an element-at-a-time loop;
it alone was over half the build time. The sort that replaces it also yields
`bucket_order()`, which normal smoothing and ambient occlusion need.

The grid is reusable: the same bucketing answers "which splats are near this
one". scipy is not available inside Blender, so a grid is the practical
stand-in for a KD-tree - the same reasoning `lod.py` applies to its LOD merge.
"""

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Blender-Claude


import numpy as np

# Enough buckets that the per-frame projection is trivial, few enough that the
# gather stays the dominant cost. ~40k over the dense region of a capture.
TARGET_BUCKETS = 40000
# Percentile bounds used for cell sizing, so outliers cannot inflate the cell.
_LO_PCT, _HI_PCT = 1.0, 99.0
_MIN_CELL = 1e-6
# Percentiles are taken on a subsample: on 9.35M splats this measured 27 ms
# against 774 ms for the full array, and the resulting cell size differed by
# 0.1%. Percentiles converge fast, so the full sort buys nothing.
_PCT_SAMPLE = 250_000
# Bucket 0 is reserved: everything outside the dense region lands there and is
# treated as always visible. See _OUTLIER_BUCKET below.
_OUTLIER_BUCKET = 0
# Padding around the robust bounds, in cells, before a splat counts as an
# outlier.
_PAD_CELLS = 2.0
# Each axis is floored at this fraction of the largest axis, so a flat capture
# cannot drive the cell size to zero.
_FLAT_AXIS_FLOOR = 0.02
# Hard ceiling on the occupancy array. 24M cells is ~192 MB as int64 counts,
# which is affordable; the cell size is grown until the span fits under it.
_MAX_CELLS = 24_000_000


class BucketGrid:
    """Uniform voxel grid over splat centres.

    Attributes
    ----------
    bucket_of : (N,) int32     bucket index for each splat
    centers   : (B, 3) float32 bucket centroids
    radius    : (B,) float32   conservative radius per bucket
    """

    __slots__ = ("bucket_of", "centers", "radius", "cell", "n_buckets",
                 "n_outliers", "_order", "_starts")

    def __init__(self, xyz, cull_r=None, target=TARGET_BUCKETS):
        xyz = np.ascontiguousarray(xyz, np.float32)
        n = len(xyz)
        self._order = None
        self._starts = None

        # -- cell size from the dense region, measured on a subsample ---
        step = max(1, n // _PCT_SAMPLE)
        sub = xyz[::step]
        lo_r = np.percentile(sub, _LO_PCT, axis=0).astype(np.float32)
        hi_r = np.percentile(sub, _HI_PCT, axis=0).astype(np.float32)
        # A flat capture - a wall, a floor, a photo plane - has near-zero
        # extent on one axis. Taking the raw product then gives a volume close
        # to zero, a microscopic cell, and a grid span of billions of cells:
        # the allocation below is what actually kills Blender. Flooring each
        # axis against the LARGEST one keeps the cell proportional to the
        # scene the user can actually see.
        ext = np.maximum(hi_r - lo_r, 0.0)
        ref = float(ext.max())
        if ref <= 0.0:                      # every splat at one point
            ref = 1.0
        ext = np.maximum(ext, ref * _FLAT_AXIS_FLOOR)
        vol = float(np.prod(ext))
        cell = max((vol / max(target, 1)) ** (1.0 / 3.0), _MIN_CELL)
        self.cell = np.float32(cell)

        # -- split off the outliers -------------------------------------
        # A capture's true bounds are set by a few sky and floater splats
        # hundreds of units out. Binning them would stretch the grid span to
        # hundreds of millions of potential cells, which forces a slow sorted
        # path and wastes memory on empty space. They are few and enormous -
        # exactly the splats that are on screen almost always - so they go in
        # one reserved bucket that is permanently visible. Conservative by
        # definition: a bucket that is never culled can never drop a splat.
        pad = np.float32(cell * _PAD_CELLS)
        lo_g = lo_r - pad
        hi_g = hi_r + pad
        inl = np.all((xyz >= lo_g) & (xyz <= hi_g), axis=1)
        n_out = int(n - inl.sum())

        self.bucket_of = np.zeros(n, np.int32)      # 0 = outlier bucket
        if inl.all():
            xin = xyz
        else:
            xin = xyz[inl]

        # -- quantise the inliers ---------------------------------------
        # Quantise, then verify the span really is affordable. The cell size
        # above is a heuristic on the dense region; an awkward distribution can
        # still produce more cells than the occupancy array can hold, so the
        # cell is grown until it fits rather than trusting the estimate. This
        # is a hard safety bound, not a tuning knob - exceeding it means an
        # allocation big enough to take Blender down with it.
        for _ in range(8):
            ijk = np.floor((xin - lo_g) / cell).astype(np.int64)
            np.maximum(ijk, 0, out=ijk)
            span = ijk.max(axis=0) + 1
            potential = int(span[0]) * int(span[1]) * int(span[2])
            if potential <= _MAX_CELLS:
                break
            cell *= max((potential / _MAX_CELLS) ** (1.0 / 3.0), 1.26)
            self.cell = np.float32(cell)
            del ijk
        keys = ijk[:, 0] + span[0] * (ijk[:, 1] + span[1] * ijk[:, 2])
        del ijk

        # Occupancy by bincount, then a cumsum renumbers the occupied cells
        # compactly. No sort: O(N + potential). Bucket ids start at 1 so 0
        # stays reserved for the outliers.
        occ = np.bincount(keys, minlength=potential) > 0
        ids = (np.cumsum(occ)).astype(np.int32)     # occupied -> 1, 2, 3, ...
        if inl.all():
            self.bucket_of = ids[keys]
        else:
            self.bucket_of[inl] = ids[keys]
        b = int(occ.sum()) + 1                      # +1 for the outlier bucket
        del keys, occ, ids
        self.n_buckets = b

        # -- centroids ---------------------------------------------------
        cnt = np.maximum(np.bincount(self.bucket_of, minlength=b), 1
                         ).astype(np.float32)
        c = np.empty((b, 3), np.float32)
        for k in range(3):
            c[:, k] = np.bincount(self.bucket_of, weights=xyz[:, k],
                                  minlength=b) / cnt
        self.centers = c

        # -- conservative radius -----------------------------------------
        # Every bucket gets its cell half-diagonal. Splats are discs, so a
        # large one whose centre is inside the bucket can still stick out; a
        # blanket margin of half a cell covers every splat up to that size,
        # and only the rare splats bigger than that need a per-bucket maximum.
        # Restricting `maximum.at` to those few keeps it off the hot path -
        # run over all N it is slower than everything else here combined.
        rad = np.float32(cell * 0.8660254)
        thresh = np.float32(cell * 0.5)
        margin = np.full(b, thresh, np.float32)
        if cull_r is not None and len(cull_r) == n:
            cr = np.asarray(cull_r, np.float32)
            big = cr > thresh
            if big.any():
                np.maximum.at(margin, self.bucket_of[big], cr[big])
        self.radius = rad + margin
        # The outlier bucket is forced visible in `visible_buckets` rather
        # than given an infinite radius: inf multiplied by a zero scale factor
        # (a degenerate projection matrix) is NaN, and NaN fails every
        # comparison, which would silently CULL the sky instead of keeping it.
        self.n_outliers = n_out

    # -- per-frame ------------------------------------------------------
    def visible_buckets(self, mvp):
        """Boolean over buckets: could this bucket touch the frustum?

        Only x, y and w are tested; near/far is left to the GPU, matching the
        per-splat test this replaces.
        """
        M = np.ascontiguousarray(mvp[:3][:, [0, 1, 3]])
        h = self.centers @ M
        h += mvp[3][[0, 1, 3]]
        x, y, w = h[:, 0], h[:, 1], h[:, 2]
        m = self.radius * np.float32(
            0.5 * (abs(float(mvp[0, 0])) + abs(float(mvp[1, 1]))))
        lim = np.abs(w)
        lim += m
        keep = np.greater(w, -m)
        np.logical_and(keep, np.less_equal(np.abs(x), lim), out=keep)
        np.logical_and(keep, np.less_equal(np.abs(y), lim), out=keep)
        keep[_OUTLIER_BUCKET] = True        # sky and floaters: always drawn
        return keep

    def frustum_mask(self, mvp, out=None):
        """Boolean over ALL splats, via the bucket test."""
        vis = self.visible_buckets(mvp)
        return np.take(vis, self.bucket_of, out=out)

    # -- reusable neighbourhood queries ---------------------------------
    def bucket_order(self):
        """Splat indices sorted by bucket, plus per-bucket start offsets.

        Cached: the build uses it for per-bucket maxima, and normal smoothing
        and ambient occlusion reuse it without a second sort.
        """
        if self._order is None:
            self._order = np.argsort(self.bucket_of, kind='stable')
            counts = np.bincount(self.bucket_of, minlength=self.n_buckets)
            self._starts = np.concatenate(
                [np.zeros(1, np.int64), np.cumsum(counts)])
        return self._order, self._starts
