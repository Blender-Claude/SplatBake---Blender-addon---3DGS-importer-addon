"""SOG (Spatially Ordered Gaussians) reader - web viewer format, version 2.

SOG stores a splat scene as a set of 8-bit images plus a meta.json describing
how to dequantise them, giving files roughly 15-20x smaller than the
equivalent PLY. Two layouts exist and both are supported here:

  * bundled    - a ZIP with the extension .sog, files at the archive root
  * unbundled  - a directory containing meta.json next to the images

The decoding maths lives in `decode()`, which takes plain numpy arrays and so
can be verified without Blender. Only `read_u8_image()` needs Blender, which
supplies the WebP decoder (no external dependency).

Spec: https://developer.playcanvas.com/user-manual/gaussian-splatting/formats/sog/
"""

import json
import math
import os
import tempfile
import zipfile

import numpy as np

SH_C0 = 0.28209479177387814
_SH_COEFFS = (3, 8, 15)        # coefficients per colour channel, by band count
_CENTROIDS_PER_ROW = 64        # fixed by the spec


# ---------------------------------------------------------------- decoding

def _dequant_means(meta, ml, mu, count):
    """16 bits per axis, split low/high byte across two images, dequantised
    into a log domain and then un-logged."""
    lo = ml.reshape(-1, ml.shape[-1])[:count, :3].astype(np.uint16)
    hi = mu.reshape(-1, mu.shape[-1])[:count, :3].astype(np.uint16)
    q = ((hi << 8) | lo).astype(np.float32) / 65535.0
    mins = np.asarray(meta["means"]["mins"], np.float32)
    maxs = np.asarray(meta["means"]["maxs"], np.float32)
    n = mins + (maxs - mins) * q                    # log-domain position
    return (np.sign(n) * (np.exp(np.abs(n)) - 1.0)).astype(np.float32)


def _dequant_scales(meta, img, count):
    """Per-axis codebook indices; the codebook is log-domain."""
    cb = np.asarray(meta["scales"]["codebook"], np.float32)
    idx = img.reshape(-1, img.shape[-1])[:count, :3]
    return np.exp(cb[idx]).astype(np.float32)


def _dequant_quats(img, count):
    """'Smallest three': RGB hold three signed components quantised to
    +/-sqrt(2)/2, and A holds 252..255 naming which component was dropped.
    Components are ordered (w, x, y, z) throughout - the same order the
    renderer and the PLY loader use, so no re-ordering is needed."""
    px = img.reshape(-1, img.shape[-1])[:count]
    abc = (px[:, :3].astype(np.float32) / 255.0 - 0.5) * 2.0 / math.sqrt(2.0)
    mode = px[:, 3].astype(np.int32) - 252
    # Values outside 252..255 are reserved; treat them as "w was dropped"
    # rather than throwing away the whole model.
    bad = (mode < 0) | (mode > 3)
    if bad.any():
        mode = np.where(bad, 0, mode)
    d = np.sqrt(np.maximum(0.0, 1.0 - (abc * abc).sum(axis=1)))
    quat = np.zeros((count, 4), np.float32)
    for m in range(4):
        sel = (mode == m)
        if not sel.any():
            continue
        quat[sel, m] = d[sel]                      # the reconstructed one
        rest = [j for j in range(4) if j != m]     # the three stored ones
        for k, j in enumerate(rest):
            quat[sel, j] = abc[sel, k]
    nrm = np.linalg.norm(quat, axis=1, keepdims=True)
    nrm[nrm == 0] = 1.0
    return (quat / nrm).astype(np.float32), int(bad.sum())


def _dequant_sh0(meta, img, count):
    """RGB are codebook indices for the DC coefficient; A is opacity, already
    in 0..1 (no logit/sigmoid, unlike PLY)."""
    cb = np.asarray(meta["sh0"]["codebook"], np.float32)
    px = img.reshape(-1, img.shape[-1])[:count]
    dc = cb[px[:, :3]].astype(np.float32)
    opacity = (px[:, 3].astype(np.float32) / 255.0)
    return dc, opacity


def _dequant_shn(meta, centroids, labels, count, chunk=1 << 20):
    """Palette-compressed higher-order SH.

    Each gaussian stores a 16-bit palette index; the palette image holds
    `coeffs` pixels per entry, 64 entries per row, and each pixel's RGB are
    codebook indices for the three colour channels. Gathered in chunks so a
    multi-million-splat scene does not spike memory."""
    bands = int(meta["shN"]["bands"])
    if not 1 <= bands <= 3:
        raise ValueError(f"SOG: unsupported shN.bands {bands}")
    k = _SH_COEFFS[bands - 1]
    cb = np.asarray(meta["shN"]["codebook"], np.float32)
    lab = labels.reshape(-1, labels.shape[-1])[:count]
    index = (lab[:, 0].astype(np.int32)
             | (lab[:, 1].astype(np.int32) << 8))
    palette = int(meta["shN"].get("count", index.max() + 1))
    np.clip(index, 0, max(palette - 1, 0), out=index)

    cent = centroids
    ch = cent.shape[-1]
    flat = cent.reshape(-1, ch)
    width = cent.shape[1]
    out = np.empty((count, k, 3), np.float32)
    cols = np.arange(k, dtype=np.int32)
    for s in range(0, count, chunk):
        e = min(s + chunk, count)
        lb = index[s:e]
        u = (lb % _CENTROIDS_PER_ROW)[:, None] * k + cols[None, :]
        v = (lb // _CENTROIDS_PER_ROW)[:, None]
        out[s:e] = cb[flat[(v * width + u).ravel(), :3]].reshape(-1, k, 3)
    return out


def decode(meta, imgs, want_sh=True):
    """Turn decoded 8-bit images + meta into the addon's splat dict.

    imgs maps logical names ('means_l', 'means_u', 'scales', 'quats', 'sh0',
    and optionally 'shN_centroids' / 'shN_labels') to uint8 arrays shaped
    (H, W, C) with the origin at the TOP-LEFT, as the spec requires.
    """
    version = int(meta.get("version", 0))
    if version != 2:
        raise ValueError(
            f"SOG version {version} is not supported (this reader implements "
            f"version 2). Re-export it with a current converter.")
    count = int(meta["count"])
    if count <= 0:
        raise ValueError("SOG: meta.count is zero")
    cap = imgs["means_l"].shape[0] * imgs["means_l"].shape[1]
    if count > cap:
        raise ValueError(f"SOG: meta.count ({count}) exceeds image capacity "
                         f"({cap})")

    xyz = _dequant_means(meta, imgs["means_l"], imgs["means_u"], count)
    scale = _dequant_scales(meta, imgs["scales"], count)
    quat, bad_modes = _dequant_quats(imgs["quats"], count)
    dc, opacity = _dequant_sh0(meta, imgs["sh0"], count)

    out = {
        "xyz": xyz,
        "rgb": (0.5 + SH_C0 * dc).astype(np.float32),
        "opacity": opacity.astype(np.float32),
        "scale": scale,
        "quat": quat,
    }
    if bad_modes:
        print(f"[SplatBake] SOG: {bad_modes} quaternions had a "
              f"reserved mode byte and were reconstructed as mode 0")

    if want_sh and "shN" in meta and "shN_centroids" in imgs:
        try:
            out["sh"] = _dequant_shn(meta, imgs["shN_centroids"],
                                     imgs["shN_labels"], count)
            out["dc"] = dc.astype(np.float32)
        except Exception as e:
            print("[SplatBake] SOG: higher-order SH skipped:", e)
    return out


# ------------------------------------------------------------ file access

def read_u8_image(path):
    """Decode an image to a (H, W, 4) uint8 array with the origin at the
    TOP-LEFT, using Blender's own image loader (so WebP needs no extra
    dependency).

    Two settings matter and both are easy to get wrong:
      * colorspace Non-Color - the spec says pixels are raw integers; the
        default sRGB setting would gamma-convert every value.
      * alpha_mode CHANNEL_PACKED - sh0 keeps opacity in A and codebook
        indices in RGB; straight/premultiplied handling would blend them.
    """
    import bpy
    img = bpy.data.images.load(path, check_existing=False)
    try:
        try:
            img.colorspace_settings.name = 'Non-Color'
        except Exception:
            pass
        try:
            img.alpha_mode = 'CHANNEL_PACKED'
        except Exception:
            pass
        w, h = img.size
        if w == 0 or h == 0:
            raise ValueError(f"could not decode '{os.path.basename(path)}' "
                             f"(is this Blender build missing WebP support?)")
        buf = np.empty(w * h * 4, dtype=np.float32)
        img.pixels.foreach_get(buf)
        # Blender hands back rows bottom-up; SOG indexes from the top-left.
        arr = buf.reshape(h, w, 4)[::-1]
        return np.rint(arr * 255.0).astype(np.uint8)
    finally:
        try:
            bpy.data.images.remove(img)
        except Exception:
            pass


_SLOTS = (("means", ("means_l", "means_u")),
          ("scales", ("scales",)),
          ("quats", ("quats",)),
          ("sh0", ("sh0",)),
          ("shN", ("shN_centroids", "shN_labels")))


def find_manifest(folder, depth=2):
    """Locate a scene manifest inside `folder`.

    Streamed downloads unzip to different shapes - the manifest may sit at the
    top, or one level down inside a wrapper folder named after the scene. So
    pointing the importer at the folder you unzipped works either way, rather
    than requiring you to find lod-meta.json yourself.

    A streamed manifest wins over a single-scene one: if both are present, the
    lod-meta.json describes the whole scene and meta.json only one chunk of it
    - which is exactly the trap that silently loads a fragment.
    """
    if not os.path.isdir(folder):
        return None
    best_lod = best_single = None
    for root, dirs, files in os.walk(folder):
        rel = os.path.relpath(root, folder)
        level = 0 if rel == "." else rel.count(os.sep) + 1
        if level > depth:
            dirs[:] = []
            continue
        names = {f.lower(): f for f in files}
        if "lod-meta.json" in names and best_lod is None:
            best_lod = os.path.join(root, names["lod-meta.json"])
        if "meta.json" in names and best_single is None:
            best_single = os.path.join(root, names["meta.json"])
    return best_lod or best_single


def _resolve(meta, root):
    """Map logical image names to real paths using meta's `files` lists.
    Filenames are arbitrary per the spec - only their ORDER is defined - so
    never guess names from the slot."""
    out = {}
    for key, names in _SLOTS:
        block = meta.get(key)
        if not block:
            continue
        files = block.get("files") or []
        for name, fn in zip(names, files):
            p = os.path.join(root, fn)
            if not os.path.exists(p):
                if key == "shN":
                    continue          # optional: fall back to base colour
                raise ValueError(f"SOG: missing '{fn}' referenced by meta.json")
            out[name] = p
    return out


def load_sog(filepath, want_sh=True, detail='FULL', budget=0):
    """Load a bundled .sog (ZIP), an unbundled meta.json, or a folder.

    `detail` and `budget` only matter when the path resolves to a streamed
    scene; a single-scene SOG ignores them.
    """
    path = os.path.abspath(filepath)
    if zipfile.is_zipfile(path):
        with tempfile.TemporaryDirectory(prefix="fgs_sog_") as tmp:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
                metas = [n for n in names
                         if os.path.basename(n).lower() == "meta.json"]
                lods = [n for n in names
                        if os.path.basename(n).lower() == "lod-meta.json"]
                if lods:
                    # A streamed scene inside a zip: unpack the whole thing to
                    # a temporary folder and read it from there, so importing
                    # the download directly works.
                    lods.sort(key=len)
                    inner = os.path.dirname(lods[0])
                    zf.extractall(tmp)
                    return load_streamed(os.path.join(tmp, inner,
                                                      "lod-meta.json"),
                                         want_sh=want_sh, detail=detail,
                                         budget=budget)
                if not metas:
                    raise ValueError("SOG archive has no meta.json")
                metas.sort(key=len)          # shallowest wins
                inner = os.path.dirname(metas[0])
                # Extract only what we need, flattened next to meta.json.
                wanted = [n for n in names
                          if os.path.dirname(n) == inner and not n.endswith("/")]
                for n in wanted:
                    dst = os.path.join(tmp, os.path.basename(n))
                    with zf.open(n) as src, open(dst, "wb") as f:
                        f.write(src.read())
            return _load_dir(os.path.join(tmp, "meta.json"), want_sh)
    if os.path.isdir(path):
        path = find_manifest(path) or os.path.join(path, "meta.json")
    if os.path.basename(path).lower() == "lod-meta.json":
        return load_streamed(path, want_sh=want_sh, detail=detail,
                             budget=budget)
    return _load_dir(path, want_sh)


# ------------------------------------------------------- streamed / LOD SOG

def _subsample(data, budget):
    """Opacity-weighted thinning, applied per chunk so peak memory stays
    bounded (a full streamed scene with degree-3 SH can exceed a gigabyte)."""
    n = len(data["xyz"])
    if budget <= 0 or n <= budget:
        return data
    rng = np.random.default_rng(0)
    u = rng.random(n)
    w = np.clip(data["opacity"].astype(np.float64), 1e-4, None)
    keys = np.log(np.maximum(u, 1e-12)) / w      # Efraimidis-Spirakis
    keep = np.sort(np.argpartition(keys, -budget)[-budget:])
    return {k: v[keep] for k, v in data.items()}


def _concat(parts):
    """Join chunk dicts, keeping only keys every chunk provides. Chunks may
    legitimately carry different SH band counts, so trim to the smallest.

    Fills a preallocated buffer and drops each chunk's reference as soon as it
    is copied. np.concatenate would hold every chunk AND the result at once,
    which roughly doubles peak memory - and a full streamed scene with
    degree-3 SH is already close to a gigabyte per array."""
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
            p[key] = None          # free this chunk's array immediately
        out[key] = buf
    return out


def levels_from_manifest(meta, root):
    """Group chunk meta.json paths by LOD level.

    The level is the leading number of each chunk's folder ("1_2/meta.json"
    -> level 1). That convention is not written down in the spec, so it is
    only trusted when the grouping reproduces the manifest's own per-level
    `counts`; otherwise the caller falls back to loading everything.
    """
    files = meta.get("filenames") or []
    if not files:
        return None
    groups = {}
    for f in files:
        base = os.path.basename(os.path.dirname(f))
        try:
            lvl = int(base.split("_")[0])
        except (ValueError, IndexError):
            return None
        groups.setdefault(lvl, []).append(os.path.join(root, f))
    counts = meta.get("counts") or []
    if len(counts) == len(groups):
        for lvl, paths in groups.items():
            if not 0 <= lvl < len(counts):
                return None
            total = 0
            for p in paths:
                try:
                    with open(p, "r", encoding="utf-8") as fh:
                        total += int(json.load(fh).get("count", 0))
                except Exception:
                    return None
            if total != int(counts[lvl]):
                print("[SplatBake] SOG: LOD grouping did not match the "
                      "manifest counts; loading every chunk")
                return None
    return groups


def load_streamed(filepath, want_sh=True, detail='FULL', budget=0):
    """Load a streamed / LOD SOG scene (lod-meta.json + chunk folders).

    The level handling lives in lod.py - see the long note there for why the
    levels are neither pure alternatives nor a simple stack. In short: level 0
    is the refined foreground, and a few thousand GIANT splats exist only at
    the coarser levels and carry the sky and far backdrop. The default merge
    takes level 0 whole and adds only the coarse splats covering ground no
    finer level describes.

    `detail` maps to lod.merge modes:
      FULL   -> COMPLETE (whole scene, minimal overdraw)  [recommended]
      FAST   -> FINEST   (level 0 only; drops the coarse-only background)
      COARSE -> COARSE   (coarsest level alone, quick preview)
      ALL    -> ALL      (every level stacked, redundancy included)
    """
    from . import lod

    path = os.path.abspath(filepath)
    root = os.path.dirname(path)
    with open(path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    if int(meta.get("version", 0)) != 1:
        print(f"[SplatBake] SOG: unexpected lod-meta version "
              f"{meta.get('version')}; reading it anyway")

    # A streamed scene may carry a separate ENVIRONMENT alongside the LOD
    # levels - the sky and far backdrop, as one un-tiled scene. It is named by
    # the manifest's "environment" key and appears in NO level, so anything
    # that only walks `filenames` silently drops it. On a real capture that is
    # 77,223 splats carrying 99% of the scene's visual area: the whole sky.
    # It is never LOD-filtered; it is simply always drawn.
    env = None
    env_rel = meta.get("environment")
    if env_rel:
        env_path = os.path.join(root, str(env_rel).replace("/", os.sep))
        if os.path.exists(env_path):
            try:
                print(f"[SplatBake] loading environment: {env_rel}")
                env = _load_dir(env_path, want_sh)
            except Exception as e:
                print("[SplatBake] environment could not be read:", e)
        else:
            print(f"[SplatBake] manifest names an environment ({env_rel}) "
                  f"that is not present")

    groups = levels_from_manifest(meta, root)
    if not groups:
        # No usable level structure - fall back to loading every chunk.
        chunks = [os.path.join(root, f) for f in (meta.get("filenames") or [])]
        chunks = [p for p in chunks if os.path.exists(p)]
        if not chunks:
            raise ValueError("SOG: lod-meta.json lists no readable chunks")
        parts = []
        for i, p in enumerate(chunks):
            print(f"[SplatBake] SOG chunk {i + 1}/{len(chunks)}")
            parts.append(_load_dir(p, want_sh))
        data = lod._concat(parts + ([env] if env is not None else []))
        return _apply_budget(data, budget)

    order = sorted(groups)                                  # 0 = finest
    total_chunks = sum(len(groups[l]) for l in order)
    done = [0]

    def level_stream():
        """Yield one whole level at a time, so lod.merge can filter and
        release it before the next is read."""
        for lvl in order:
            parts = []
            for p in groups[lvl]:
                if not os.path.exists(p):
                    continue
                done[0] += 1
                print(f"[SplatBake] SOG chunk {done[0]}/{total_chunks}: "
                      f"{os.path.basename(os.path.dirname(p))}")
                parts.append(_load_dir(p, want_sh))
            if parts:
                yield lod._concat(parts)

    mode = {'FULL': 'COMPLETE', 'FAST': 'FINEST',
            'COARSE': 'COARSE', 'ALL': 'ALL'}.get(detail, 'COMPLETE')
    data, _info = lod.merge(level_stream(), mode)

    # Safety net: load any scene folder present on disk that the manifest
    # never named. `environment` was one such key - undocumented, easy to
    # miss, and it held the entire sky. Rather than hope no OTHER key exists,
    # anything with a readable meta.json beside the manifest is picked up and
    # drawn in full. Extra data is a far smaller failure than silently
    # dropping a third of a scene.
    extras = _unreferenced_scenes(root, groups, env_rel)
    for name, path in extras:
        try:
            print(f"[SplatBake] + extra scene folder '{name}' "
                  f"(not named by the manifest)")
            part = _load_dir(path, want_sh)
            data = lod._concat([data, part])
        except Exception as e:
            print(f"[SplatBake] could not read '{name}':", e)

    if env is not None:
        print(f"[SplatBake] + {len(env['xyz']):,} environment splats "
              f"(sky / far backdrop)")
        data = lod._concat([data, env])
    return _apply_budget(data, budget)


def _unreferenced_scenes(root, groups, env_rel):
    """Scene folders next to the manifest that it does not mention.

    A streamed export is a folder of sub-scenes plus a manifest describing
    how to combine them. The manifest is the map - but if it omits something,
    or names it under a key this reader does not know, that data would vanish
    without a word. So the folder itself is checked too.
    """
    referenced = set()
    for paths in (groups or {}).values():
        for p in paths:
            referenced.add(os.path.normcase(os.path.abspath(p)))
    if env_rel:
        referenced.add(os.path.normcase(os.path.abspath(
            os.path.join(root, str(env_rel).replace("/", os.sep)))))
    out = []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return out
    for name in entries:
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        mp = os.path.join(d, "meta.json")
        if not os.path.exists(mp):
            continue
        if os.path.normcase(os.path.abspath(mp)) in referenced:
            continue
        out.append((name, mp))
    return out


def _apply_budget(data, budget):
    """Opacity-weighted thinning, applied once to the final merged scene."""
    n = len(data["xyz"])
    if budget <= 0 or n <= budget:
        return data
    rng = np.random.default_rng(0)
    u = rng.random(n)
    op = data.get("opacity")
    if op is not None:
        w = np.clip(op.astype(np.float64), 1e-4, None)
        keys = np.log(np.maximum(u, 1e-12)) / w
    else:
        keys = u
    keep = np.sort(np.argpartition(keys, -int(budget))[-int(budget):])
    print(f"[SplatBake] thinned {n:,} -> {budget:,} splats (Max Splats)")
    return {k: v[keep] for k, v in data.items()}


def _load_dir(meta_path, want_sh):
    if not os.path.exists(meta_path):
        raise ValueError(f"SOG: no meta.json at {meta_path}")
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    paths = _resolve(meta, os.path.dirname(meta_path))
    need_sh = want_sh and "shN" in meta
    imgs = {}
    for name, p in paths.items():
        if name.startswith("shN") and not need_sh:
            continue
        imgs[name] = read_u8_image(p)
    for req in ("means_l", "means_u", "scales", "quats", "sh0"):
        if req not in imgs:
            raise ValueError(f"SOG: meta.json does not provide '{req}'")
    return decode(meta, imgs, want_sh=want_sh)
