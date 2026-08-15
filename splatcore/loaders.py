"""Reading and preparing gaussian-splat data.

Supports standard 3DGS PLY (with optional spherical-harmonic coefficients) and
the binary .splat format. Also provides import-time cleanup helpers: outlier
trimming, opacity-weighted subsampling, and an upright rotation.
"""

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Blender-Claude


import math
import os
import numpy as np

SH_C0 = 0.28209479177387814

_PLY_TO_NP = {
    "float": "f4", "float32": "f4", "double": "f8", "float64": "f8",
    "uchar": "u1", "uint8": "u1", "char": "i1", "int8": "i1",
    "ushort": "u2", "uint16": "u2", "short": "i2", "int16": "i2",
    "uint": "u4", "uint32": "u4", "int": "i4", "int32": "i4",
}


def load_ply(filepath, want_sh=False):
    """Parse a 3DGS PLY into numpy arrays. Decodes SH DC -> RGB, logit ->
    opacity, log -> scale, and normalises the quaternion. Optionally also
    extracts the higher-order SH coefficients for view-dependent colour."""
    with open(filepath, "rb") as f:
        if f.readline().strip() != b"ply":
            raise ValueError("Not a PLY file")
        fmt, count, props, in_vertex = None, 0, [], False
        while True:
            line = f.readline()
            if not line:
                raise ValueError("Unexpected end of header")
            s = line.strip().decode("ascii", errors="replace")
            if s.startswith("format"):
                fmt = s.split()[1]
            elif s.startswith("element"):
                parts = s.split()
                in_vertex = (parts[1] == "vertex")
                if in_vertex:
                    count = int(parts[2])
            elif s.startswith("property") and in_vertex:
                parts = s.split()
                if parts[1] != "list":
                    props.append((parts[2], parts[1]))
            elif s.startswith("end_header"):
                break
        if count == 0:
            raise ValueError("PLY contains no vertices")
        if fmt == "ascii":
            rows, read = [], 0
            while read < count:
                ln = f.readline()
                if not ln:
                    break
                rows.append([float(v) for v in ln.split()])
                read += 1
            arr = np.array(rows, dtype=np.float32)
            cols = {n: arr[:, i] for i, (n, _) in enumerate(props)}
        else:
            bo = "<" if fmt == "binary_little_endian" else ">"
            dt = np.dtype([(n, bo + _PLY_TO_NP.get(t, "f4")) for (n, t) in props])
            raw = f.read(count * dt.itemsize)
            rec = np.frombuffer(raw, dtype=dt, count=count)
            cols = {n: rec[n].astype(np.float32) for (n, _) in props}

    def has(*names):
        return all(n in cols for n in names)

    if not has("x", "y", "z"):
        raise ValueError("PLY has no x/y/z coordinates")
    n = count
    xyz = np.stack([cols["x"], cols["y"], cols["z"]], axis=1)

    if has("f_dc_0", "f_dc_1", "f_dc_2"):
        rgb = np.clip(0.5 + SH_C0 * np.stack(
            [cols["f_dc_0"], cols["f_dc_1"], cols["f_dc_2"]], axis=1), 0.0, 1.0)
    elif has("red", "green", "blue"):
        rgb = np.stack([cols["red"], cols["green"], cols["blue"]], axis=1)
        if rgb.max() > 1.0:
            rgb = rgb / 255.0
    else:
        rgb = np.full((n, 3), 0.8, dtype=np.float32)

    opacity = (1.0 / (1.0 + np.exp(-cols["opacity"]))) if "opacity" in cols \
        else np.ones(n, dtype=np.float32)
    scale = np.exp(np.stack([cols["scale_0"], cols["scale_1"], cols["scale_2"]], axis=1)) \
        if has("scale_0", "scale_1", "scale_2") else np.full((n, 3), 0.01, np.float32)

    if has("rot_0", "rot_1", "rot_2", "rot_3"):
        q = np.stack([cols["rot_0"], cols["rot_1"], cols["rot_2"], cols["rot_3"]], axis=1)
        nrm = np.linalg.norm(q, axis=1, keepdims=True)
        nrm[nrm == 0] = 1.0
        quat = q / nrm
    else:
        quat = np.tile(np.array([1, 0, 0, 0], np.float32), (n, 1))

    out = {
        "xyz": xyz.astype(np.float32), "rgb": rgb.astype(np.float32),
        "opacity": opacity.astype(np.float32), "scale": scale.astype(np.float32),
        "quat": quat.astype(np.float32),
    }

    # Optional higher-order SH (channel-major: R's K coeffs, then G's, then B's).
    if want_sh and has("f_dc_0", "f_dc_1", "f_dc_2"):
        rest_names = sorted([nm for nm in cols if nm.startswith("f_rest_")],
                            key=lambda s: int(s.split("_")[-1]))
        m = len(rest_names)
        if m >= 9 and m % 3 == 0:
            k = m // 3
            rest = np.stack([cols[nm] for nm in rest_names], axis=1)   # (N, 3K)
            sh = rest.reshape(n, 3, k).transpose(0, 2, 1)              # (N, K, 3)
            out["sh"] = np.ascontiguousarray(sh).astype(np.float32)
            out["dc"] = np.stack([cols["f_dc_0"], cols["f_dc_1"],
                                  cols["f_dc_2"]], axis=1).astype(np.float32)
            # base colour unclipped: the shader adds SH bands then clamps,
            # matching the reference clip-at-the-end behaviour
            out["rgb"] = (0.5 + SH_C0 * out["dc"]).astype(np.float32)
    return out


def load_splat(filepath):
    """Parse a binary .splat file (32 bytes/splat: pos f32x3, scale f32x3,
    rgba u8x4, rot u8x4). Colours/scales/opacity are already linear here."""
    with open(filepath, "rb") as f:
        raw = f.read()
    n = len(raw) // 32
    if n == 0:
        raise ValueError("Empty or invalid .splat file")
    dt = np.dtype([("pos", "<f4", 3), ("scale", "<f4", 3),
                   ("rgba", "u1", 4), ("rot", "u1", 4)])
    rec = np.frombuffer(raw, dtype=dt, count=n)
    rgba = rec["rgba"].astype(np.float32) / 255.0
    quat = (rec["rot"].astype(np.float32) - 128.0) / 128.0
    nrm = np.linalg.norm(quat, axis=1, keepdims=True)
    nrm[nrm == 0] = 1.0
    return {
        "xyz": np.ascontiguousarray(rec["pos"]).astype(np.float32),
        "rgb": np.ascontiguousarray(rgba[:, :3]).astype(np.float32),
        "opacity": np.ascontiguousarray(rgba[:, 3]).astype(np.float32),
        "scale": np.ascontiguousarray(rec["scale"]).astype(np.float32),
        "quat": np.ascontiguousarray(quat / nrm).astype(np.float32),
    }


def load_any(filepath, want_sh=False, lod='FULL', budget=0):
    """Dispatch by extension. `lod` / `budget` only apply to streamed SOG."""
    low = filepath.lower()
    base = os.path.basename(low)
    if low.endswith(".splat"):
        return load_splat(filepath)   # .splat carries no SH
    if base == "lod-meta.json":
        from . import sog
        return sog.load_streamed(filepath, want_sh=want_sh, detail=lod,
                                 budget=budget)
    if (low.endswith(".sog") or low.endswith(".zip") or base == "meta.json"
            or base == "lod-meta.json" or os.path.isdir(filepath)):
        from . import sog
        # detail/budget must travel: pointing at a FOLDER is the normal way to
        # open a streamed scene, and that path resolves to load_streamed too.
        return sog.load_sog(filepath, want_sh=want_sh, detail=lod,
                            budget=budget)
    return load_ply(filepath, want_sh=want_sh)


def robust_bounds(xyz, sample=150000):
    """Bounds of the scene's CORE, ignoring sky/floater splats.

    Captures routinely contain a handful of splats thousands of units away.
    Real example: a 1.9M-splat scan whose true extent is ~140 units but whose
    raw min/max span 117,000 because of 11 stray splats. Using raw min/max for
    anything user-facing (the transform handle, the sort pivot) puts the pivot
    tens of thousands of units off-scene.

    Median +/- 4*MAD rather than fixed percentiles: percentiles fail once the
    floater cluster is larger than the cut fraction. Returns (lo, hi).
    """
    step = max(1, len(xyz) // max(sample, 1))
    samp = xyz[::step].astype(np.float32)
    med = np.median(samp, axis=0)
    mad = np.median(np.abs(samp - med), axis=0) * 1.4826 + 1e-6
    keep = np.all((samp >= med - 4.0 * mad) & (samp <= med + 4.0 * mad), axis=1)
    core = samp[keep] if keep.any() else samp
    return core.min(axis=0), core.max(axis=0)


def trim_outliers(data, pct):
    """Drop splats outside the central percentile box on each axis."""
    if pct <= 0.0:
        return data
    xyz = data["xyz"]
    lo = np.percentile(xyz, pct, axis=0)
    hi = np.percentile(xyz, 100.0 - pct, axis=0)
    mask = np.all((xyz >= lo) & (xyz <= hi), axis=1)
    if mask.sum() == 0:
        return data
    return {k: v[mask] for k, v in data.items()}


def subsample(data, budget, weighted):
    """Reduce to `budget` splats, optionally preferring high-opacity ones."""
    n = len(data["xyz"])
    if budget <= 0 or n <= budget:
        return data
    rng = np.random.default_rng(0)
    if weighted:
        p = data["opacity"].astype(np.float64)
        tot = p.sum()
        p = (p / tot) if tot > 0 else None
        keep = rng.choice(n, budget, replace=False, p=p)
    else:
        keep = rng.choice(n, budget, replace=False)
    keep.sort()
    return {k: v[keep] for k, v in data.items()}


def _quat_mul(q1, q2):
    w1, x1, y1, z1 = q1[..., 0], q1[..., 1], q1[..., 2], q1[..., 3]
    w2, x2, y2, z2 = q2[..., 0], q2[..., 1], q2[..., 2], q2[..., 3]
    return np.stack([
        w1*w2 - x1*x2 - y1*y2 - z1*z2,
        w1*x2 + x1*w2 + y1*z2 - z1*y2,
        w1*y2 - x1*z2 + y1*w2 + z1*x2,
        w1*z2 + x1*y2 - y1*x2 + z1*w2,
    ], axis=-1)


def apply_upright(data):
    """Rotate -90 deg about X so the data's -Y (up) becomes Blender's Z+
    (Y-up captures -> Blender Z-up). Positions and per-gaussian orientations
    are rotated together so nothing shears."""
    # 3DGS captures are Y-DOWN: up is -Y. Rotate -90 deg about X so -Y
    # becomes Blender's Z+ (the previous +90 put scenes upside-down).
    R = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], dtype=np.float32)
    a = math.pi / 4.0
    qfix = np.array([math.cos(a), -math.sin(a), 0.0, 0.0], dtype=np.float32)
    data["xyz"] = (R @ data["xyz"].T).T.astype(np.float32)
    data["quat"] = _quat_mul(qfix[None, :], data["quat"]).astype(np.float32)
    return data
