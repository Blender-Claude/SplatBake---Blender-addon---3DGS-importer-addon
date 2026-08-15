"""Level-of-detail handling for streamed SOG scenes.

Kept in its own module so the LOD strategy can be tuned without touching the
reader, the renderer or anything else.

WHAT A STREAMED SOG ACTUALLY CONTAINS
-------------------------------------
A streamed scene is a spatial hierarchy written out as numbered folders:
`0_0`, `0_1`, ... `1_0`, ... `2_0`. The leading number is the LOD level, 0
being the finest. Each level holds roughly half the splats of the one below
it, with roughly sqrt(2) larger radii.

The tempting reading is that the levels are alternatives - pick one, discard
the rest. That is wrong, and it is wrong in a way that is easy to miss:

  * Most of the scene IS represented at every level. On a real 6.25M-splat
    capture, 93% of the coarse levels' occupied space also contains fine
    splats, and a redundant coarse splat sits about 0.3 of its own spacing
    from a fine one - i.e. directly on top of it. Drawing all levels renders
    those surfaces two or three times over, the coarse copies blurring across
    the fine detail.

  * But a small number of splats exist ONLY at the coarse levels, and they are
    the giant ones. On that same capture they are ~0.2% of the splats with a
    median radius of 1.52 against level 0's 0.022 - seventy times larger, some
    of them 40 units across. These are the sky and far backdrop: regions the
    hierarchy never needed to subdivide. Keep only level 0 and the foreground
    survives while the background vanishes.

So neither "stack everything" nor "take the finest level" is right. The
complete scene at its best quality is: **every region drawn at the finest
level that actually covers it.**

HOW THE MERGE WORKS
-------------------
Level 0 is taken whole. Then, for each coarser level in turn, a splat is kept
only if its own neighbourhood is not already described by a finer one. The
test is a voxel occupancy lookup rather than a nearest-neighbour search:
scipy is not available inside Blender, and a grid is O(n) in numpy anyway.

The cell size is derived from the data - a multiple of the finest level's
typical splat radius - so it scales with the capture instead of assuming
world units.
"""

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Blender-Claude


import numpy as np

# Cell size = CELL_FACTOR x the finest level's median splat radius. The
# separation this has to resolve is stark (redundant coarse splats sit ~0.3x
# their spacing from a fine one; genuine background sits tens of units away),
# so the exact value is not delicate.
CELL_FACTOR = 4.0
_MIN_CELL = 1e-6


def splat_radius(scale):
    """Typical radius of each splat: the mean of its two largest axes, which
    is the disc it actually draws (the smallest axis is its thickness)."""
    s = np.sort(np.asarray(scale, np.float32), axis=1)
    return 0.5 * (s[:, 2] + s[:, 1])


def _cell_keys(xyz, cell, origin):
    """Map points to a 1-D integer key per occupied voxel."""
    c = np.floor((xyz - origin) / cell).astype(np.int64)
    c -= c.min(axis=0)
    span = c.max(axis=0) + 1
    return c[:, 0] + span[0] * (c[:, 1] + span[1] * c[:, 2]), span


def _covered(fine_xyz, test_xyz, cell):
    """Boolean per test point: is this neighbourhood already occupied?

    Both sets are quantised onto the same grid and compared with a sorted
    search - no scipy, no per-point Python loop.
    """
    if len(fine_xyz) == 0 or len(test_xyz) == 0:
        return np.zeros(len(test_xyz), bool)
    origin = np.minimum(fine_xyz.min(axis=0), test_xyz.min(axis=0))
    both = np.concatenate([fine_xyz, test_xyz])
    keys, _ = _cell_keys(both, cell, origin)
    fine_keys = np.unique(keys[:len(fine_xyz)])
    test_keys = keys[len(fine_xyz):]
    i = np.searchsorted(fine_keys, test_keys)
    i = np.clip(i, 0, len(fine_keys) - 1)
    return fine_keys[i] == test_keys


def _take(part, mask):
    return {k: v[mask] for k, v in part.items()}


def merge(levels, mode='COMPLETE', cell_factor=CELL_FACTOR, report=print):
    """Combine per-level splat dicts into the set that should be drawn.

    `levels` is a list ordered finest-first. Modes:

      COMPLETE - level 0 whole, plus only those coarser splats covering ground
                 no finer level describes. The full scene, minimal overdraw.
                 This is the default and what you almost always want.
      ALL      - every level stacked, redundancy included. Matches a naive
                 reader; heavier and hazier, kept for comparison.
      FINEST   - level 0 only. Fastest, but drops the coarse-only background.
      COARSE   - the coarsest level alone, for a quick preview.
    """
    # `levels` may be a generator, so each level can be loaded, filtered and
    # released one at a time - the whole point being that the rejected splats
    # never all exist at once. A streamed scene with degree-3 SH is well over
    # a gigabyte if every level is held.
    it = iter(levels)
    fine = None
    for lv in it:
        if lv is not None and len(lv.get("xyz", ())) > 0:
            fine = lv
            break
    if fine is None:
        raise ValueError("no LOD levels to merge")

    if mode == 'FINEST':
        for _ in it:                       # drain, so any files close
            pass
        return fine, {"mode": 'FINEST', "kept": [len(fine["xyz"])]}
    if mode == 'COARSE':
        last = fine
        for lv in it:
            if lv is not None and len(lv.get("xyz", ())) > 0:
                last = lv
        return last, {"mode": 'COARSE', "kept": [len(last["xyz"])]}
    if mode == 'ALL':
        parts = [fine] + [lv for lv in it
                          if lv is not None and len(lv.get("xyz", ())) > 0]
        return _concat(parts), {"mode": 'ALL',
                                "kept": [len(p["xyz"]) for p in parts]}

    cell = (max(float(np.median(splat_radius(fine["scale"]))) * cell_factor,
                _MIN_CELL) if "scale" in fine else _MIN_CELL)
    kept_parts = [fine]
    kept_counts = [len(fine["xyz"])]
    accum = fine["xyz"]
    for lv in it:
        if lv is None or len(lv.get("xyz", ())) == 0:
            continue
        keep = ~_covered(accum, lv["xyz"], cell)
        n = int(keep.sum())
        kept_counts.append(n)
        if n:
            part = _take(lv, keep)
            kept_parts.append(part)
            # Coarse splats that survive join the covered set, so a still
            # coarser level does not add a third copy of the same patch.
            accum = np.concatenate([accum, part["xyz"]])
        del lv                              # release the rejected splats now
    out = _concat(kept_parts)
    info = {"mode": 'COMPLETE', "kept": kept_counts, "cell": cell}
    if report:
        report(f"[SplatBake] LOD merge: {len(out['xyz']):,} splats drawn "
               f"(level 0 whole, plus {sum(kept_counts[1:]):,} from coarser "
               f"levels covering ground it does not) - kept {kept_counts}")
    return out, info


def _concat(parts):
    """Join level dicts, keeping only the keys every part provides."""
    if len(parts) == 1:
        return parts[0]
    keys = set(parts[0])
    for p in parts[1:]:
        keys &= set(p)
    if "sh" in keys:
        k = min(p["sh"].shape[1] for p in parts)
        for p in parts:
            if p["sh"].shape[1] != k:
                p["sh"] = np.ascontiguousarray(p["sh"][:, :k, :])
    total = sum(len(p["xyz"]) for p in parts)
    out = {}
    for key in keys:
        ref = parts[0][key]
        buf = np.empty((total,) + ref.shape[1:], ref.dtype)
        off = 0
        for p in parts:
            a = p[key]
            buf[off:off + len(a)] = a
            off += len(a)
        out[key] = buf
    return out
