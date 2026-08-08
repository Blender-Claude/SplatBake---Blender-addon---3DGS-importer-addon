"""Persistence: make splat models survive saving and reopening a .blend.

Self-contained, like lighting.py and uvtools.py.

WHAT IS AND IS NOT STORED
-------------------------
Not the splats. A multi-million-splat cloud is hundreds of megabytes of float
data; writing that into every .blend would bloat files and make saving slow,
and Blender would have to keep a second copy of it all in ID properties.

Instead each handle Empty carries a small *recipe* — the source file path, the
import options used, and the rest transform — and the splats are re-read from
disk when the file opens. The .blend grows by a few hundred bytes per model.

The one thing that cannot be recovered from the file is which splats you
DELETED with the splat-delete tool, so that alive mask is stored too: packed
to one bit per splat, zlib-compressed, base64'd, and written only when
something has actually been deleted. On a 2M-splat model that is typically a
few kilobytes rather than the 2 MB a raw mask would take.

WHY THE REST TRANSFORM MATTERS
------------------------------
`rest_inv` is the inverse of the handle's transform at import time; the
renderer computes `box.matrix_world @ rest_inv`, so moving the handle moves
the splats. If we recreated it from the handle's *current* matrix on reload,
every model that had been moved would jump. So the original is stored and
restored verbatim.

WHY LOADING IS DEFERRED
-----------------------
Re-reading gigabyte scans inside `load_post` would freeze Blender before the
UI appears. The handler therefore only queues the work and returns; a one-shot
timer does the actual loading a moment later, so the file opens promptly and
the models stream in after it.
"""

import base64
import os
import zlib

import bpy
import numpy as np
from mathutils import Matrix

from . import loaders, state
from .renderer import SplatRenderer

KEY = "splatbake"          # ID-property namespace on the handle Empty
SCHEMA = 1

_PENDING = []              # handle names still to be restored
_FAILED = []               # (name, reason) for the report


# ------------------------------------------------------------------ writing

def _pack_alive(alive):
    """Alive mask -> compact text, or None when nothing has been deleted."""
    if alive is None or bool(alive.all()):
        return None
    bits = np.packbits(np.asarray(alive, dtype=bool))
    return base64.b64encode(zlib.compress(bits.tobytes(), 6)).decode("ascii")


def _unpack_alive(text, n):
    bits = np.frombuffer(zlib.decompress(base64.b64decode(text)), dtype=np.uint8)
    # packbits pads to a whole byte, so a mask for `n` splats is exactly
    # ceil(n/8) bytes. Checking the BYTE count catches a mask belonging to a
    # different model; checking only the truncated length would not, because
    # a longer mask silently slices down to n and applies the wrong bits.
    if len(bits) != (n + 7) // 8:
        raise ValueError(f"alive mask is for a different splat count "
                         f"({len(bits) * 8} bits, expected {n})")
    mask = np.unpackbits(bits)[:n].astype(bool)
    if len(mask) != n:
        raise ValueError("alive mask length does not match the model")
    return mask


def remember(r, filepath, opts):
    """Store the recipe for model `r` on its handle Empty.

    `opts` mirrors the import operator's settings so the reload reproduces the
    same data: use_sh, max_points, weighted, trim, upright, lod.
    """
    box = bpy.data.objects.get(r.box_name)
    if box is None:
        return
    try:
        path = bpy.path.relpath(filepath) if bpy.data.filepath else filepath
    except Exception:
        path = filepath
    d = {
        "schema": SCHEMA,
        "file": path,
        "file_abs": os.path.abspath(filepath),
        "use_sh": int(bool(opts.get("use_sh", True))),
        "max_points": int(opts.get("max_points", 0)),
        "weighted": int(bool(opts.get("weighted", True))),
        "trim": float(opts.get("trim", 0.0)),
        "upright": int(bool(opts.get("upright", True))),
        "lod": str(opts.get("lod", "FULL")),
        # Row-major 4x4; restored verbatim so a moved model does not jump.
        "rest_inv": [float(v) for row in r.rest_inv for v in row],
    }
    box[KEY] = d
    update_alive(r)


def update_alive(r):
    """Refresh only the deleted-splat mask (cheap; call after edits)."""
    box = bpy.data.objects.get(r.box_name)
    if box is None or KEY not in box:
        return
    try:
        packed = _pack_alive(getattr(r, "alive", None))
        d = dict(box[KEY])
        if packed is None:
            d.pop("alive", None)
        else:
            d["alive"] = packed
        d["count"] = int(getattr(r, "N", 0))
        box[KEY] = d
    except Exception as e:
        print("[SplatBake] could not store the deleted-splat mask:", e)


def forget(box):
    if box is not None and KEY in box:
        try:
            del box[KEY]
        except Exception:
            pass


# ------------------------------------------------------------------ reading

def _resolve(rec):
    """Find the source file: relative to the .blend first (so a moved project
    folder still works), then the absolute path recorded at import."""
    rel = rec.get("file")
    if rel:
        try:
            p = bpy.path.abspath(rel)
            if os.path.exists(p):
                return p
        except Exception:
            pass
    ab = rec.get("file_abs")
    if ab and os.path.exists(ab):
        return ab
    return None


def _restore_one(box):
    rec = dict(box[KEY])
    if int(rec.get("schema", 0)) != SCHEMA:
        raise ValueError("saved with a different add-on version")
    path = _resolve(rec)
    if path is None:
        raise FileNotFoundError(rec.get("file") or rec.get("file_abs") or "?")

    data = loaders.load_any(path, want_sh=bool(rec.get("use_sh", 1)),
                            lod=rec.get("lod", "FULL"),
                            budget=int(rec.get("max_points", 0)))
    cap = int(rec.get("max_points", 0))
    if cap and len(data["xyz"]) > cap:
        data = loaders.subsample(data, cap, bool(rec.get("weighted", 1)))
    trim = float(rec.get("trim", 0.0))
    if trim > 0.0:
        data = loaders.trim_outliers(data, trim)
    if bool(rec.get("upright", 1)):
        data = loaders.apply_upright(data)
    if len(data["xyz"]) == 0:
        raise ValueError("no splats in the file")

    flat = rec.get("rest_inv")
    rest_inv = (Matrix([list(flat[i * 4:i * 4 + 4]) for i in range(4)])
                if flat and len(flat) == 16 else Matrix.Identity(4))
    r = SplatRenderer(data, box.name, rest_inv)

    packed = rec.get("alive")
    if packed:
        try:
            r.alive = _unpack_alive(packed, r.N)
        except Exception as e:
            # A mismatched mask is not worth losing the model over.
            print("[SplatBake] deleted-splat mask skipped:", e)
    state.add_renderer(r)
    return r


def _drain():
    """Timer callback: restore the queued models, one per tick so the UI can
    breathe and so one bad file cannot stall the rest."""
    if not _PENDING:
        if _FAILED:
            names = ", ".join(n for n, _ in _FAILED[:3])
            more = "" if len(_FAILED) <= 3 else f" (+{len(_FAILED) - 3} more)"
            print(f"[SplatBake] could not restore: {names}{more}")
            _FAILED.clear()
        return None
    name = _PENDING.pop(0)
    box = bpy.data.objects.get(name)
    if box is not None and KEY in box:
        try:
            r = _restore_one(box)
            print(f"[SplatBake] restored '{name}' ({r.N:,} splats)")
        except FileNotFoundError as e:
            _FAILED.append((name, f"file not found: {e}"))
            print(f"[SplatBake] '{name}': source file not found ({e}) - "
                  f"re-import it, or put the file back and reopen")
        except Exception as e:
            _FAILED.append((name, str(e)))
            print(f"[SplatBake] '{name}' could not be restored: {e}")
    state.redraw_all()
    return 0.05 if _PENDING else 0.0


@bpy.app.handlers.persistent
def _on_load(*args):
    """After a .blend opens: drop any stale models, then queue the ones this
    file describes. Clearing first matters - the renderer list is module
    state, so without it the models from the PREVIOUS file would keep drawing
    against handle names that no longer exist."""
    try:
        state.clear_all()
    except Exception:
        pass
    _PENDING.clear()
    _FAILED.clear()
    try:
        _PENDING.extend(o.name for o in bpy.data.objects
                        if o.type == 'EMPTY' and KEY in o)
    except Exception:
        return
    if not _PENDING:
        return
    print(f"[SplatBake] restoring {len(_PENDING)} splat model(s)...")
    if not bpy.app.timers.is_registered(_drain):
        bpy.app.timers.register(_drain, first_interval=0.2)


def sync_from_blend():
    """Re-read every model's alive mask from its handle Empty.

    This is what makes Ctrl+Z work on splat deletion. The alive mask is a
    numpy array in Python module state, and Blender's undo system only ever
    snapshots its own database - it cannot see, save or roll back a module
    variable. So no amount of undo would bring a deleted splat back, in ANY
    Blender version; this is architectural rather than a 4.x bug.

    The fix is to keep the authoritative copy where undo CAN see it: the
    packed mask already stored as an ID property on the handle (the same one
    that makes models survive a reopen). Blender snapshots object custom
    properties, so after an undo the property holds the older mask - and this
    function copies it back into the renderer.
    """
    changed = False
    for r in list(state.RENDERERS):
        box = bpy.data.objects.get(r.box_name)
        if box is None or KEY not in box:
            continue
        try:
            rec = box[KEY]
            packed = rec.get("alive") if hasattr(rec, "get") else None
        except Exception:
            continue
        try:
            want = (_unpack_alive(packed, r.N) if packed
                    else np.ones(r.N, dtype=bool))
        except Exception:
            continue
        if want.shape != r.alive.shape or np.array_equal(want, r.alive):
            continue
        r.alive[:] = want
        r._force = True
        r._alive_ver += 1
        changed = True
    if changed:
        state.redraw_all()
    return changed


def push_undo(message="Edit Splats"):
    """Record the current masks in Blender data and add an undo step, so the
    edit can be rolled back with Ctrl+Z like any other Blender change."""
    for r in list(state.RENDERERS):
        update_alive(r)
    try:
        bpy.ops.ed.undo_push(message=message)
    except Exception as e:
        print("[SplatBake] could not push an undo step:", e)


@bpy.app.handlers.persistent
def _on_undo(*args):
    # Two different things can need undoing: whole models (the handle Empty
    # came back, so re-attach its renderer) and deleted splats within a model
    # (the stored mask rolled back, so re-apply it).
    try:
        if state.recover_orphans():
            state.redraw_all()
    except Exception as e:
        print("[SplatBake] model recovery after undo failed:", e)
    try:
        sync_from_blend()
    except Exception as e:
        print("[SplatBake] undo resync failed:", e)


def register():
    if _on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load)
    for h in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _on_undo not in h:
            h.append(_on_undo)


def unregister():
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    for h in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _on_undo in h:
            h.remove(_on_undo)
    _PENDING.clear()
    _FAILED.clear()
    if bpy.app.timers.is_registered(_drain):
        try:
            bpy.app.timers.unregister(_drain)
        except Exception:
            pass
