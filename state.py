"""Module-level state for all live splat instances.

Each model has a proxy box Empty that drives its transform (so Blender's native
G/R/S work on it). This module holds the renderer list, the viewport draw
handler, the active model, the box watcher (delete box -> remove model), and the
global splat-delete history.
"""

import bpy

VERSION = "1.16.1"

RENDERERS = []          # active SplatRenderer instances
ACTIVE = None           # the renderer last clicked / loaded
_HANDLE = None          # the SpaceView3D draw handler
DELETE_HISTORY = []     # (renderer, splat_id) for global undo

# Models whose handle Empty was deleted. Blender's undo restores the Empty,
# but the renderer lives in Python state that undo cannot see - so dropping it
# on delete meant undo brought the object back with no splats attached. They
# are parked here instead and re-attached the moment the handle reappears.
# Bounded, because each one holds its full splat data on the GPU.
TRASH = []
TRASH_MAX = 4


def draw_params(scene):
    return {
        "density": scene.fgs_density,
        "splat_scale": scene.fgs_splat_scale,
        "sharpness": scene.fgs_sharpness,
        "opacity_cutoff": scene.fgs_opacity,
        "max_pixels": (0.0 if scene.fgs_max_pixels <= 0.0
                       else scene.fgs_max_pixels * 100.0),
        "antialias": (1.0 if scene.fgs_antialias else 0.0),
        "exposure": scene.fgs_exposure,
        "saturation": scene.fgs_saturation,
        "gamma": scene.fgs_gamma,
        "tint": tuple(scene.fgs_tint),
        "use_sh": scene.fgs_use_sh,
        "mode": scene.fgs_display_mode,
        "point_size": scene.fgs_point_size,
        "hq_sort": scene.fgs_hq_sort,
        "aniso": scene.fgs_despike,
        "lod": scene.fgs_lod,
        "lod_points": scene.fgs_lod_points,
        "view_transform": scene.view_settings.view_transform,
        "pc_gaussian": scene.fgs_pc_gaussian,
        "sh_quality": scene.fgs_sh_quality,
        "cull_frustum": getattr(scene, "fgs_cull_frustum", False),
        "adaptive_sort": getattr(scene, "fgs_adaptive_sort", True),
        "wave_phase": WAVE["phase"],
        "wave_r": WAVE["r"],
        "wave_pr": WAVE["pr"],
        "wave_c": WAVE["c"],
        "wave_soft": WAVE["soft"],
    }


# Camera-motion tracking for "Point Cloud While Moving". Splats are expensive
# to draw and sort; points are not. Detecting motion here rather than hooking
# navigation operators means it works for every way of moving the view -
# orbit, pan, walk, fly, a dragged timeline, an animated camera.
_MOTION = {"mv": None, "t": 0.0, "moving": False}
MOVE_SETTLE = 1.0          # seconds of stillness before splats return


def _settle_tick():
    """One-shot: nudge a redraw once the camera has been still long enough.

    Without it the viewport would sit showing points indefinitely, because
    Blender only redraws when something asks it to - and 'nothing happened
    for a second' is not an event."""
    import time
    if time.perf_counter() - _MOTION["t"] >= MOVE_SETTLE:
        _MOTION["moving"] = False
        redraw_all()
        return None                     # stop the timer
    return 0.1                          # check again shortly


def camera_moving(rv3d):
    """True while the view is changing, and for MOVE_SETTLE seconds after."""
    import time
    now = time.perf_counter()
    try:
        mv = tuple(rv3d.view_matrix[i][j] for i in range(4) for j in range(4))
    except Exception:
        return False
    if _MOTION["mv"] is None or mv != _MOTION["mv"]:
        _MOTION["mv"] = mv
        _MOTION["t"] = now
        if not _MOTION["moving"]:
            _MOTION["moving"] = True
        if not bpy.app.timers.is_registered(_settle_tick):
            bpy.app.timers.register(_settle_tick, first_interval=MOVE_SETTLE)
        return True
    if _MOTION["moving"] and now - _MOTION["t"] >= MOVE_SETTLE:
        _MOTION["moving"] = False
    return _MOTION["moving"]


def renderer_visible(r, space=None):
    """Is this model's handle Empty visible right now?

    The splats are a GPU overlay, so nothing hides them automatically. Tying
    them to their handle object makes them obey Blender's OWN visibility
    system: H / Alt+H, the outliner eye and monitor icons, hidden or excluded
    collections, and local view (/) all hide the splats with the handle.
    `visible_get` accounts for every one of those in a single call.

    A model with no handle object always draws - there is nothing to ask."""
    name = getattr(r, "box_name", None)
    box = bpy.data.objects.get(name) if name else None
    if box is None:
        return True
    try:
        # `viewport` makes local view count too; without a space we still get
        # hide/hide_viewport/collection state.
        if space is not None and getattr(space, "type", None) == 'VIEW_3D':
            return bool(box.visible_get(viewport=space))
        return bool(box.visible_get())
    except Exception:
        # visible_get needs the object in the current view layer; if it is
        # not, fall back to the raw flags rather than dropping the model.
        try:
            return not (box.hide_viewport or box.hide_get())
        except Exception:
            return True


def visible_renderers(space=None):
    return [r for r in RENDERERS if renderer_visible(r, space)]


def _draw_multi(region, rv3d, params, ordered, vp):
    """Draw several splat models with ONE global depth order.

    Splats are transparent, so they cannot use the depth buffer - they rely
    entirely on draw order. Ordering whole models by their centre means a
    small model placed inside a big one (a car in a street scan) is composited
    either wholly in front or wholly behind it. Here each model is already
    depth-sorted internally, so we cut every model's sorted list at shared
    depth boundaries and draw slab by slab, far to near, interleaving models.
    Returns False if the fast single-model path should be used instead.
    """
    import numpy as np
    vis = [r for r in ordered if r.visible_in_frustum(vp)]
    if len(vis) < 2:
        return False
    density = params["density"]
    hq = bool(params.get("hq_sort", True))
    for r in vis:
        mv = np.array(rv3d.view_matrix @ r.model_matrix(), dtype=np.float32)
        r._maybe_sort(mv, density, hq)
    live = [(r, r.depth_range()) for r in vis]
    live = [(r, dr) for r, dr in live if dr is not None]
    if len(live) < 2:
        return False

    # boundaries: every model's near/far, with overlapping spans subdivided
    pts = sorted(set([dr[0] for _, dr in live] + [dr[1] for _, dr in live]))
    if len(pts) < 2:
        return False
    edges = [pts[0]]
    SUB = 32                       # slabs across a region shared by 2+ models
    for a, b in zip(pts[:-1], pts[1:]):
        if b <= a:
            continue
        mid = 0.5 * (a + b)
        cover = sum(1 for _, dr in live if dr[0] <= mid <= dr[1])
        if cover >= 2:
            for i in range(1, SUB + 1):
                edges.append(a + (b - a) * (i / float(SUB)))
        else:
            edges.append(b)
    if len(edges) < 2:
        return False
    edges = np.asarray(edges, np.float32)
    # widen the outer edges so the very first/last splats are inside the range
    eps = float(edges[-1] - edges[0]) * 1e-5 + 1e-6
    edges[0] -= eps
    edges[-1] += eps

    for r, _ in live:
        r.build_slabs(edges)
    for i in range(len(edges) - 1):        # far -> near
        for r, _ in live:
            sl = r._slabs
            if not sl or i >= len(sl) or sl[i] is None:
                continue
            r.draw(region, rv3d, params, skip_sort=True, batch_override=sl[i])
    return True


def _draw_callback():
    if not RENDERERS:
        return
    region = bpy.context.region
    rv3d = bpy.context.region_data
    if region is None or rv3d is None:
        return
    if WAVE["phase"] and WAVE["tick"]:
        WAVE["tick"] = False       # one step per rendered frame
        wave_advance()
    params = draw_params(bpy.context.scene)
    params["view_pos"] = rv3d.view_matrix.inverted().translation
    vp = rv3d.window_matrix @ rv3d.view_matrix
    sc_ = bpy.context.scene

    # The reveal targets ONE model (the one just imported). Everyone else gets
    # the wave uniforms disabled so already-loaded models keep drawing
    # normally instead of replaying the animation.
    quiet = None
    if WAVE["phase"] != 0 and WAVE["target"] is not None:
        quiet = dict(params)
        quiet["wave_phase"] = 0
        quiet["wave_r"] = -1.0
        quiet["wave_pr"] = -1.0

    # Hidden handles mean hidden splats: drop them before ordering so the
    # global depth sort, the frustum pass and the draw loop all agree.
    live = visible_renderers(getattr(bpy.context, "space_data", None))
    if not live:
        return

    # Composite models back-to-front by the view depth of their centre, so the
    # one you're looking at sits on top instead of whichever loaded last.
    vm = rv3d.view_matrix

    def _depth(r):
        c = r.model_matrix() @ r.center_local
        return vm[2][0] * c.x + vm[2][1] * c.y + vm[2][2] * c.z + vm[2][3]

    ordered = sorted(live, key=_depth)   # ascending z = farthest first

    # Per-model settings: each model can carry its own display mode, density,
    # size and SH quality. Resolved here so the sort path below can tell
    # whether the models still share parameters.
    # Points while the view moves: decided once per draw, applied both to the
    # shared fast path below and to each model's own parameters, so per-model
    # settings cannot put splats back mid-motion.
    force_points = (getattr(sc_, "fgs_points_moving", False)
                    and camera_moving(rv3d))
    if force_points:
        params = dict(params)
        params["mode"] = 'POINTS'

    per = None
    if getattr(sc_, "fgs_per_model", False):
        try:
            from . import permodel
            per = permodel
        except Exception as e:
            print("[SplatBake] per-model settings unavailable:", e)

    # Several models that all draw as splats need one global depth order,
    # otherwise a small model inside a big one sorts wholly in front/behind.
    # That single pass shares one set of parameters, so it is only valid while
    # every model resolves to the same ones.
    if (len(ordered) > 1 and params.get("mode") == "SPLAT"
            and WAVE["phase"] == 0
            and (per is None or per.uniform(ordered))):
        if per is not None:
            params = per.params_for(ordered[0], params)
        try:
            if _draw_multi(region, rv3d, params, ordered, vp):
                return
        except Exception as e:
            print("[SplatBake] global sort failed, per-model draw:", e)

    for r in ordered:
        try:
            if not r.visible_in_frustum(vp):
                continue
            p = params if (quiet is None or r is WAVE["target"]) else quiet
            if per is not None:
                p = per.params_for(r, p)
                if force_points:
                    p = dict(p)
                    p["mode"] = 'POINTS'
            r.draw(region, rv3d, p)
        except Exception as e:
            print("[SplatBake] draw error:", e)


def add_handle():
    global _HANDLE
    if _HANDLE is None:
        _HANDLE = bpy.types.SpaceView3D.draw_handler_add(
            _draw_callback, (), 'WINDOW', 'POST_VIEW')


def remove_handle():
    global _HANDLE
    if _HANDLE is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_HANDLE, 'WINDOW')
        _HANDLE = None


def tag_redraw(context):
    for area in context.screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()


def redraw_all():
    wm = bpy.context.window_manager
    if wm:
        for win in wm.windows:
            for a in win.screen.areas:
                if a.type == 'VIEW_3D':
                    a.tag_redraw()


def set_active(r):
    global ACTIVE
    ACTIVE = r


def active_renderer(context=None):
    """The model whose box is the active object, else the last touched/loaded."""
    if context is not None and context.active_object is not None:
        name = context.active_object.name
        for r in RENDERERS:
            if r.box_name == name:
                return r
    if ACTIVE in RENDERERS:
        return ACTIVE
    return RENDERERS[-1] if RENDERERS else None


def add_renderer(r):
    RENDERERS.append(r)
    set_active(r)
    add_handle()


def _remove_box(r):
    box = bpy.data.objects.get(r.box_name)
    if box is not None:
        try:
            bpy.data.objects.remove(box, do_unlink=True)
        except Exception:
            pass


def remove_renderer(r, recycle=True, keep_box=False):
    """Drop a model.

    keep_box=True detaches the renderer but LEAVES the handle Empty in the
    scene. Reloading a model at a different detail level needs that: it
    replaces the splats behind an existing handle, and deleting the Empty
    would invalidate the very object the reload reads its recipe from.
    """
    global ACTIVE
    if WAVE.get("target") is r:
        stop_wave()
    if r in RENDERERS:
        RENDERERS.remove(r)
    if recycle:
        _trash(r)          # so Ctrl+Z can bring the splats back with the Empty
    if not keep_box:
        _remove_box(r)
    DELETE_HISTORY[:] = [(rr, sid) for (rr, sid) in DELETE_HISTORY if rr is not r]
    if ACTIVE is r:
        ACTIVE = RENDERERS[-1] if RENDERERS else None
    if not RENDERERS:
        remove_handle()


def clear_all():
    """Remove every instance, its box, and the draw handler."""
    global RENDERERS, DELETE_HISTORY, ACTIVE
    TRASH.clear()
    stop_wave()
    remove_handle()
    for r in RENDERERS:
        _remove_box(r)
    RENDERERS = []
    DELETE_HISTORY = []
    ACTIVE = None


def apply_box_visibility(show):
    """Show or hide every box's wireframe; the object stays selectable."""
    for r in RENDERERS:
        box = bpy.data.objects.get(r.box_name)
        if box is not None:
            box.empty_display_size = 1.0 if show else 0.0


# ---- two-stage droplet reveal -------------------------------------------
# Stage 1: the point cloud sweeps in from the centre.
# Stage 2: a splat front sweeps out behind it, converting dots -> gaussians.
#
# The animation is driven by FRAMES ACTUALLY DRAWN, not by wall-clock time:
# on a big scene the first frame costs shader compiles, the first 899k sort
# and a ~21MB index upload, so a clock-driven wave had already "finished"
# before frame one reached the screen -- which is why everything popped in
# at once. One step per drawn frame guarantees the reveal is always seen.
WAVE = {
    "phase": 0,          # 0 = off, 1 = points sweeping in, 2 = splats
    "r": -1.0,           # splat front radius (<0 = inactive)
    "pr": -1.0,          # point front radius (<0 = inactive)
    "c": (0.0, 0.0, 0.0),
    "soft": 1.0,
    "end": 0.0,
    "tick": False,       # set by the timer, consumed by the draw callback
    "target": None,      # the ONE renderer being revealed; others draw as usual
}
_WAVE_STEP = [0]
_PTS_STEPS = 16          # frames for the point cloud to fill in
_SPL_STEPS = 24          # frames for the splats to take over


def start_wave(center, radius, target=None):
    """Kick off the point-cloud -> gaussian reveal for ONE model.

    `center` / `radius` are in the target model's LOCAL (data) frame - that is
    the frame the shaders compare splat centres against. Only `target` is
    gated by the front; every other loaded model keeps drawing normally, so
    importing a second model no longer replays the reveal on the first."""
    if radius <= 0.0:
        WAVE.update(phase=0, r=-1.0, pr=-1.0, target=None)
        return
    WAVE["c"] = (float(center[0]), float(center[1]), float(center[2]))
    WAVE["soft"] = max(radius * 0.10, 1e-3)
    WAVE["end"] = radius + WAVE["soft"] * 2.0
    WAVE["phase"] = 1
    WAVE["pr"] = 0.0         # no dots yet
    WAVE["r"] = -1.0         # no splat front yet: nothing gets consumed
    WAVE["tick"] = False
    WAVE["target"] = target
    _WAVE_STEP[0] = 0
    if not bpy.app.timers.is_registered(_wave_step):
        bpy.app.timers.register(_wave_step)


def stop_wave():
    """Cancel the reveal instantly (target removed, scene cleared, ...)."""
    WAVE.update(phase=0, r=-1.0, pr=-1.0, tick=False, target=None)


def wave_advance():
    """Advance one step. Called from the draw callback so every step renders."""
    if WAVE["phase"] == 0:
        return
    _WAVE_STEP[0] += 1
    i = _WAVE_STEP[0]
    end = WAVE["end"]
    ease = lambda t: 1.0 - (1.0 - t) ** 3      # quick start, gentle settle
    if i <= _PTS_STEPS:
        WAVE["phase"] = 1
        WAVE["pr"] = ease(i / float(_PTS_STEPS)) * end
        WAVE["r"] = -1.0
    elif i <= _PTS_STEPS + _SPL_STEPS:
        WAVE["phase"] = 2
        WAVE["pr"] = end
        WAVE["r"] = ease((i - _PTS_STEPS) / float(_SPL_STEPS)) * end
    else:
        WAVE.update(phase=0, r=-1.0, pr=-1.0, target=None)  # done -> normal


def _wave_step():
    """Timer: just keeps redraws coming; the draw callback does the stepping."""
    if WAVE["phase"] == 0:
        return None
    WAVE["tick"] = True
    _redraw_all()
    return 0.01


def _redraw_all():
    try:
        for w in bpy.context.window_manager.windows:
            for area in w.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    except Exception:
        pass


def raw_world_diag():
    """Diagonal of the FULL (untrimmed) world bounds of all models - used to
    push the viewport clip end far enough that background splats survive."""
    from mathutils import Vector
    mn = Vector((1e18, 1e18, 1e18)); mx = Vector((-1e18, -1e18, -1e18))
    found = False
    for r in RENDERERS:
        M = r.model_matrix()
        for c in r._aabb:
            w = M @ Vector((float(c[0]), float(c[1]), float(c[2])))
            mn.x = min(mn.x, w.x); mn.y = min(mn.y, w.y); mn.z = min(mn.z, w.z)
            mx.x = max(mx.x, w.x); mx.y = max(mx.y, w.y); mx.z = max(mx.z, w.z)
            found = True
    return (mx - mn).length if found else 0.0


def _tight_world_bounds():
    """Union of every VISIBLE model's trimmed (2%-98%) bounds in world space.
    Framing should go to what you can actually see; if everything is hidden
    we fall back to all models so Frame still does something sensible."""
    from mathutils import Vector
    mn = Vector((1e18, 1e18, 1e18)); mx = Vector((-1e18, -1e18, -1e18))
    found = False
    pool = visible_renderers(getattr(bpy.context, "space_data", None))
    if not pool:
        pool = RENDERERS
    for r in pool:
        corners = getattr(r, "_tight_corners", None)
        if corners is None:
            continue
        M = r.model_matrix()
        for c in corners:
            w = M @ Vector((float(c[0]), float(c[1]), float(c[2])))
            mn.x = min(mn.x, w.x); mn.y = min(mn.y, w.y); mn.z = min(mn.z, w.z)
            mx.x = max(mx.x, w.x); mx.y = max(mx.y, w.y); mx.z = max(mx.z, w.z)
            found = True
    return (mn, mx) if found else None


def nice_step(raw):
    """1-2-5 series, identical to the web viewer's niceStep()."""
    import math
    raw = max(raw, 1e-9)
    p = 10.0 ** math.floor(math.log10(raw))
    r = raw / p
    return (1 if r < 1.5 else 2 if r < 3.5 else 5 if r < 7.5 else 10) * p


def pick_splat_point(region, rv3d, mx, my):
    """Nearest splat centre to the cursor ray, within ~1 degree. Returns a
    world-space Vector or None. Same test as the web viewer's pickPoint()."""
    import numpy as np
    from bpy_extras import view3d_utils
    from mathutils import Vector
    o = view3d_utils.region_2d_to_origin_3d(region, rv3d, (mx, my))
    d = view3d_utils.region_2d_to_vector_3d(region, rv3d, (mx, my)).normalized()
    od = np.array((o.x, o.y, o.z), dtype=np.float32)
    dd = np.array((d.x, d.y, d.z), dtype=np.float32)
    best_t = None; best_p = None
    for r in RENDERERS:
        M = np.array(r.model_matrix(), dtype=np.float32)
        step = max(1, r.N // 200000)
        c = r.centers[::step]
        world = c @ M[:3, :3].T + M[:3, 3]        # (m,3) world centres
        rel = world - od
        t = rel @ dd
        m = t > 0.0
        if not np.any(m):
            continue
        rel_m = rel[m]; t_m = t[m]
        perp2 = np.einsum('ij,ij->i', rel_m, rel_m) - t_m * t_m
        lim = t_m * 0.017                          # ~1 degree cone
        hit = perp2 < lim * lim
        if not np.any(hit):
            continue
        ti = t_m[hit]; k = int(np.argmin(ti)); tt = float(ti[k])
        if best_t is None or tt < best_t:
            best_t = tt
            best_p = od + dd * tt
    if best_p is None:
        return None
    return Vector((float(best_p[0]), float(best_p[1]), float(best_p[2])))


def pick_renderer_under_cursor(region, rv3d, mx, my, radius=16.0):
    """The model under the cursor, made forgiving. Two passes:

    1. nearest splat CENTRE within `radius` px (front-most wins) - exact,
       decimated on huge clouds so a click stays instant;
    2. if no centre is that close, cast the cursor ray against every model's
       outlier-trimmed bounding box and take the nearest entry.

    Pass 2 means clicking anywhere ON or INSIDE a model's volume selects it -
    no pixel accuracy needed - while pass 1 still resolves overlapping models
    to the one actually under the cursor. Hidden models are skipped: if you
    cannot see it, you cannot click it."""
    live = visible_renderers(getattr(bpy.context, "space_data", None))
    best = None
    for r in live:
        res = r.pick(region, rv3d, mx, my, radius, coarse=True)
        if res is not None and (best is None or res[0] < best[0]):
            best = (res[0], r)
    if best is not None:
        return best[1]
    hit = None
    for r in live:
        t = r.ray_hits_bounds(region, rv3d, mx, my)
        if t is not None and (hit is None or t < hit[0]):
            hit = (t, r)
    return hit[1] if hit else None


def delete_under_cursor(region, rv3d, mx, my, radius=12.0):
    """Delete the front-most splat across all instances under the cursor."""
    best = None   # (depth_w, renderer, splat_id)
    for r in RENDERERS:
        res = r.pick(region, rv3d, mx, my, radius)
        if res is not None and (best is None or res[0] < best[0]):
            best = (res[0], r, res[1])
    if best is None:
        return False
    best[1].kill(best[2])
    DELETE_HISTORY.append((best[1], best[2]))
    return True


def undo_delete():
    if DELETE_HISTORY:
        r, sid = DELETE_HISTORY.pop()
        if r in RENDERERS:
            r.revive(sid)
        return True
    return False


def restore_all():
    global DELETE_HISTORY
    for r in RENDERERS:
        r.restore_all()
    DELETE_HISTORY = []


@bpy.app.handlers.persistent
def _watch_box(scene, depsgraph):
    """When a box is deleted in the outliner/viewport, remove its model."""
    if not RENDERERS and not TRASH:
        return
    # Handles that CAME BACK are dealt with first - an undone deletion, or a
    # Shift+D copy that needs a renderer. Only then is the survivor list built.
    # Building it first was a real bug: the list was a snapshot taken before
    # the new models were added, so assigning it back over RENDERERS threw
    # them straight away again, and a duplicated model never appeared.
    recovered = recover_orphans()
    recovered = adopt_new_handles() or recovered
    survivors = [r for r in RENDERERS
                 if bpy.data.objects.get(r.box_name) is not None]
    if len(survivors) != len(RENDERERS):
        global ACTIVE
        for r in RENDERERS:
            if r not in survivors:
                _trash(r)
        RENDERERS[:] = survivors
        if WAVE.get("target") is not None and WAVE["target"] not in RENDERERS:
            stop_wave()
        if ACTIVE not in RENDERERS:
            ACTIVE = RENDERERS[-1] if RENDERERS else None
        if not RENDERERS:
            remove_handle()
        redraw_all()
    elif recovered:
        redraw_all()


def adopt_new_handles():
    """Give a renderer to any handle Empty that has a recipe but no splats.

    This is what makes Blender's own Shift+D work. Duplicating the handle
    copies the object and its custom properties, but the splats live in Python
    state that Blender knows nothing about - so the copy would appear as an
    empty box. Here the new handle is matched to a loaded model by its recipe,
    and given an INSTANCE of it: same GPU buffers, its own transform and its
    own alive mask.

    Only handles whose source is already in the scene are adopted. Anything
    else is left to the reload path, which reads it from disk.
    """
    try:
        from . import persist
        from .renderer import SplatRenderer
    except Exception:
        return False
    live = {r.box_name for r in RENDERERS}
    parked = {n for n, _ in TRASH}
    donors = {}
    for r in RENDERERS:
        box = bpy.data.objects.get(r.box_name)
        if box is None or persist.KEY not in box:
            continue
        try:
            donors.setdefault(str(dict(box[persist.KEY]).get("file", "")), r)
        except Exception:
            pass
    if not donors:
        return False
    made = []
    for obj in bpy.data.objects:
        if obj.type != 'EMPTY' or obj.name in live or obj.name in parked:
            continue
        if persist.KEY not in obj:
            continue
        try:
            rec = dict(obj[persist.KEY])
        except Exception:
            continue
        src = donors.get(str(rec.get("file", "")))
        if src is None:
            continue
        try:
            r = SplatRenderer(src.source, obj.name, src.rest_inv.copy(),
                              share_from=src)
            r.alive = src.alive.copy()
        except Exception as e:
            print("[SplatBake] could not instance duplicated handle:", e)
            continue
        RENDERERS.append(r)
        made.append(obj.name)
    if made:
        add_handle()
        print(f"[SplatBake] duplicated {len(made)} model(s) as instances "
              f"(sharing GPU data)")
    return bool(made)


def _trash(r):
    """Park a removed model so undo can bring it back instantly."""
    name = getattr(r, "box_name", None)
    if not name:
        return
    TRASH[:] = [(n, rr) for (n, rr) in TRASH if n != name]
    TRASH.append((name, r))
    while len(TRASH) > TRASH_MAX:
        TRASH.pop(0)


def recover_orphans():
    """Re-attach any parked model whose handle Empty has come back.

    This is what makes Ctrl+Z work after deleting a whole model: undo restores
    the Empty, and the renderer that belongs to it is still sitting in TRASH
    with its GPU buffers intact, so it reappears immediately instead of having
    to be re-read from disk.
    """
    if not TRASH:
        return False
    live = {r.box_name for r in RENDERERS}
    back = []
    for name, r in list(TRASH):
        if name in live:
            TRASH.remove((name, r))
            continue
        if bpy.data.objects.get(name) is not None:
            TRASH.remove((name, r))
            RENDERERS.append(r)
            r._force = True
            back.append(r)
    if back:
        global ACTIVE
        if ACTIVE is None:
            ACTIVE = back[-1]
        add_handle()
        print(f"[SplatBake] restored {len(back)} model(s) after undo")
    return bool(back)


def register():
    if _watch_box not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_watch_box)


def unregister():
    clear_all()
    if _watch_box in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_watch_box)
