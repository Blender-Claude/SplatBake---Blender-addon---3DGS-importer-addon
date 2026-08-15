"""Operators: load, transform, duplicate, edit.

Each model has a proxy box Empty (optionally hidden) that drives its transform.
Native Blender G/R/S (with X/Y/Z constraints) work on the selected box; the
"Select Splat" tool picks the model under the cursor and makes its box active,
and a custom click-and-drag tool is also provided.
"""

# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Blender-Claude


import os
import math
import bpy
import numpy as np
from mathutils import Matrix, Vector
from bpy.props import StringProperty, FloatProperty, IntProperty, BoolProperty
from bpy_extras.io_utils import ImportHelper
from bpy_extras import view3d_utils
from bpy.types import Operator

from . import boxes, state
from .splatcore import loaders
from .renderer import SplatRenderer


def _area_under(context, event):
    for a in context.screen.areas:
        if a.type == 'VIEW_3D' and a.x <= event.mouse_x <= a.x + a.width \
                and a.y <= event.mouse_y <= a.y + a.height:
            region = next((rg for rg in a.regions if rg.type == 'WINDOW'), None)
            rv3d = a.spaces.active.region_3d
            if region is not None and rv3d is not None:
                return region, rv3d
    return None, None


class FGS_OT_load(Operator, ImportHelper):
    bl_idname = "fgs.load_splat"
    bl_label = "Load Gaussian Splat (.ply / .splat / .sog / .zip)"
    bl_options = {"REGISTER"}
    filename_ext = ".ply"
    filter_glob: StringProperty(
        default="*.ply;*.splat;*.sog;*.json;*.zip", options={"HIDDEN"})
    max_points: IntProperty(
        name="Max Splats to Load", default=0, min=0,
        description="Memory budget: how many splats to hold on the GPU. "
                    "Splats over the limit are dropped by opacity importance. "
                    "Rough cost per splat: ~60 bytes without spherical "
                    "harmonics, ~240 bytes with full SH - so 4M splats is "
                    "about 0.25 GB plain, or 1 GB with SH. Raise it on a "
                    "large scene if your GPU has the memory. 0 (the default) means no limit - load everything the file contains")
    weighted: BoolProperty(
        name="Keep Solid Splats", default=True,
        description="When subsampling, prefer high-opacity splats")
    trim: FloatProperty(
        name="Trim Outliers %", default=0.0, min=0.0, max=10.0,
        description="Crop this percentile off each axis to drop far floaters")
    lod: bpy.props.EnumProperty(
        name="Streamed SOG Detail", default='FULL',
        description="For streamed / LOD SOG scenes (lod-meta.json) only. Most "
                    "surfaces are stored at every level, but the sky and far "
                    "backdrop exist ONLY as giant splats at the coarse "
                    "levels - so neither stacking everything nor taking a "
                    "single level is right. The default merges them properly",
        items=[
            ('FULL', "Complete scene (recommended)",
             "The finest level in full, plus only those coarser splats "
             "covering ground it does not - which is where the sky and far "
             "backdrop live. The whole scene with the least overdraw"),
            ('FAST', "Foreground only (fast)",
             "The finest level alone. Lighter, but the background is stored "
             "as a few giant splats at the coarser levels, so it goes missing"),
            ('COARSE', "Coarse (preview)",
             "The coarsest level alone - fastest way to check a scene"),
            ('ALL', "Every level stacked (heaviest)",
             "All levels including the redundant ones, which paints most "
             "surfaces two or three times over. Complete, but heavier and "
             "hazier than the recommended merge"),
        ])
    true_scale: BoolProperty(
        name="True Scale (1:1 handle)", default=False,
        description="Give the model's handle a clean identity transform - "
                    "Scale (1,1,1) at the model centre - so the file's units "
                    "pass through 1:1 and the sidebar reads true. Unticked "
                    "(the current behaviour), the handle is scaled to frame "
                    "the cloud, which is handy for grabbing but shows the "
                    "half-extents as its Scale - and clearing that scale "
                    "squashes the model. The model appears at the same size "
                    "either way")
    upright: BoolProperty(
        name="Rotate Upright (Y+ to Z+)", default=True,
        description="Rotate +90 deg about X so a Y-up capture stands up in "
                    "Blender's Z-up world (maps the data's Y+ axis to Z+)")
    replace: BoolProperty(
        name="Replace Existing", default=False,
        description="Clear all currently loaded splats first. Off by default, "
                    "so each import adds another model to the scene")
    use_sh: BoolProperty(
        name="View-Dependent Colour (SH)", default=True,
        description="Load spherical-harmonic coefficients (PLY only). Richer "
                    "colour, more memory/CPU; toggle the effect in the panel")

    def execute(self, context):
        wm = context.window_manager
        wm.progress_begin(0, 100)
        import time as _t
        _t0 = _t.perf_counter()
        try:
            data = loaders.load_any(self.filepath, want_sh=self.use_sh,
                                    lod=self.lod, budget=self.max_points)
        except Exception as e:
            wm.progress_end()
            self.report({"ERROR"}, f"Failed to read file: {e}")
            return {"CANCELLED"}

        data = loaders.trim_outliers(data, self.trim)
        wm.progress_update(35)
        print(f"[SplatBake] parsed {len(data['xyz']):,} splats "
              f"in {_t.perf_counter()-_t0:.1f}s")
        data = loaders.subsample(data, self.max_points, self.weighted)
        if len(data["xyz"]) == 0:
            wm.progress_end()
            self.report({"ERROR"}, "No splats to draw")
            return {"CANCELLED"}
        if self.upright:
            data = loaders.apply_upright(data)

        if self.replace:
            state.clear_all()
        box_name = None
        try:
            base = os.path.splitext(os.path.basename(self.filepath))[0] or "Splat"
            box_name, rest_inv = boxes.make_box(context, data["xyz"],
                                                show=context.scene.fgs_show_bbox,
                                                name=base,
                                                true_scale=self.true_scale)
            r = SplatRenderer(data, box_name, rest_inv)
        except Exception as e:
            self.report({"ERROR"}, f"GPU setup failed: {e}")
            if box_name is not None:
                b = bpy.data.objects.get(box_name)
                if b is not None:
                    try:
                        bpy.data.objects.remove(b, do_unlink=True)
                    except Exception:
                        pass
            wm.progress_end()
            return {"CANCELLED"}

        wm.progress_update(90)
        state.add_renderer(r)
        # Store the recipe on the handle so the model comes back when this
        # .blend is reopened. Never fatal: a model that cannot be remembered
        # still works for this session.
        try:
            from . import persist
            persist.remember(r, self.filepath, {
                "true_scale": self.true_scale,
                "use_sh": self.use_sh,
                "max_points": self.max_points,
                "weighted": self.weighted,
                "trim": self.trim,
                "upright": self.upright,
                "lod": self.lod,
            })
        except Exception as e:
            print("[SplatBake] could not record the reload recipe:", e)
        # Everything is loaded by now; the reveal below is purely cosmetic.
        # Pre-warm the shaders/batches/sort so its frames are all cheap.
        try:
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    r.prewarm(area.spaces.active.region_3d,
                              context.scene.fgs_density)
                    break
        except Exception as e:
            print("[SplatBake] pre-warm skipped:", e)
        if context.scene.fgs_wave_on:
            # The reveal is scoped to the NEW model only: centre/radius come
            # from ITS outlier-trimmed bounds (in the data's local frame, the
            # frame the shaders test against), and start_wave gets it as the
            # target so models already in the scene keep drawing untouched.
            try:
                tc = getattr(r, "_tight_corners", None)
                if tc is not None:
                    lo = tc.min(axis=0)
                    hi = tc.max(axis=0)
                    c = (lo + hi) * 0.5
                    rad = float(np.linalg.norm(hi - lo)) * 0.5
                    state.start_wave((float(c[0]), float(c[1]), float(c[2])),
                                     rad, target=r)
            except Exception as e:
                print("[SplatBake] reveal skipped:", e)
        # Match the web viewer's colour: draw linearised, let Standard re-encode
        # to sRGB. (AgX/Filmic are what wash the splats out.)
        try:
            vs = context.scene.view_settings
            if getattr(context.scene, "fgs_raw_tones", False):
                try:
                    vs.view_transform = 'Raw'      # web-exact sRGB blending
                except Exception:
                    vs.view_transform = 'Standard'
            else:
                vs.view_transform = 'Standard'     # classic look (default)
            vs.look = 'None'
            vs.exposure = 0.0
            vs.gamma = 1.0
        except Exception:
            pass
        # frame the new model on its trimmed bounds
        try:
            raw_diag = state.raw_world_diag()
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    sp = area.spaces.active
                    rv3d = sp.region_3d
                    tb = state._tight_world_bounds()
                    if rv3d is not None and tb is not None:
                        mn, mx = tb
                        rv3d.view_location = (mn + mx) * 0.5
                        rv3d.view_distance = max((mx - mn).length * 0.85, 0.05)
                    # big scans exceed the default 1000 clip end: extend so
                    # background splats are not cut off. x4 the raw diagonal
                    # (not x2): orbiting around a scene puts the camera up to
                    # a diagonal away from the far side, and the sky splats
                    # in streamed scenes sit at the raw bounds' very edge.
                    if raw_diag > 0 and sp.clip_end < raw_diag * 4.0:
                        sp.clip_end = min(raw_diag * 4.0, 500000.0)
        except Exception:
            pass
        state.tag_redraw(context)
        wm.progress_end()
        print(f"[SplatBake] ready in {_t.perf_counter()-_t0:.1f}s total")
        n_final = len(data["xyz"])
        # Thinning to the Max Splats budget is easy to miss - it only printed
        # to the console - and on a big streamed scene it can discard more
        # than half the splats. Say so where the user will actually see it.
        if self.max_points > 0 and n_final >= self.max_points:
            self.report({'WARNING'},
                        f"Loaded {n_final:,} splats - capped by Max Splats. "
                        f"Raise it to keep more detail, or turn off "
                        f"spherical harmonics to fit more in memory")
        else:
            self.report({"INFO"}, f"Loaded {n_final:,} splats "
                                  f"({len(state.RENDERERS)} in scene)")
        return {"FINISHED"}


class FGS_OT_clear(Operator):
    bl_idname = "fgs.clear_splat"
    bl_label = "Clear All Splats"

    def execute(self, context):
        state.clear_all()
        state.tag_redraw(context)
        return {"FINISHED"}


class FGS_OT_reset_transform(Operator):
    bl_idname = "fgs.reset_transform"
    bl_label = "Reset Transform"
    bl_description = "Return the active model to its original position/rotation/scale"

    def execute(self, context):
        r = state.active_renderer(context)
        if r is not None:
            box = bpy.data.objects.get(r.box_name)
            if box is not None:
                box.matrix_basis = r.rest_inv.inverted()
        state.tag_redraw(context)
        return {"FINISHED"}


class FGS_OT_remove_active(Operator):
    bl_idname = "fgs.remove_active"
    bl_label = "Remove Active Model"
    bl_description = "Remove the active (last-clicked) splat model from the scene"

    def execute(self, context):
        r = state.active_renderer(context)
        if r is not None:
            state.remove_renderer(r)
        state.tag_redraw(context)
        return {"FINISHED"}


class FGS_OT_duplicate(Operator):
    bl_idname = "fgs.duplicate_splat"
    bl_label = "Duplicate Model"
    bl_description = ("Copy the active model. The copy shares the source data "
                     "but moves independently, offset to the side")

    def execute(self, context):
        src = state.active_renderer(context)
        if src is None:
            self.report({'WARNING'}, "Nothing to duplicate")
            return {'CANCELLED'}
        src_box = bpy.data.objects.get(src.box_name)
        if src_box is None:
            self.report({'WARNING'}, "Source box not found")
            return {'CANCELLED'}
        offset = Matrix.Translation(Vector((2.0 * src.half.x, 0.0, 0.0)))
        try:
            empty = boxes.spawn_box(context, offset @ src_box.matrix_world.copy(),
                                    context.scene.fgs_show_bbox,
                                    name=src_box.name)
            # share the donor's GPU buffers: an instance, not a second upload
            r = SplatRenderer(src.source, empty.name,
                              src.rest_inv.copy(), share_from=src)
            r.alive = src.alive.copy()
        except Exception as e:
            self.report({'ERROR'}, f"Duplicate failed: {e}")
            return {'CANCELLED'}
        state.add_renderer(r)
        try:
            from . import persist
            if persist.KEY in src_box:
                rec = dict(src_box[persist.KEY])
                rec["rest_inv"] = [float(v) for row in r.rest_inv for v in row]
                empty[persist.KEY] = rec
                persist.update_alive(r)
        except Exception as e:
            print("[SplatBake] duplicate kept no reload recipe:", e)
        state.tag_redraw(context)
        self.report({'INFO'}, f"Duplicated ({len(state.RENDERERS)} models)")
        return {'FINISHED'}


_CLIPBOARD = []


def _selected_renderers(context):
    """Every loaded model whose handle Empty is currently selected, in
    selection order. Falls back to the active model when nothing is selected
    but one is active."""
    sel = {o.name for o in context.selected_objects}
    out = [r for r in state.RENDERERS if r.box_name in sel]
    if not out:
        a = state.active_renderer(context)
        if a is not None:
            out = [a]
    return out


class FGS_OT_copy(Operator):
    bl_idname = "fgs.copy_splat"
    bl_label = "Copy Splat Model"
    bl_description = ("Copy the selected splat model(s) to the splat "
                      "clipboard, ready to paste with Ctrl+V")

    def execute(self, context):
        # (1.20.11) Copy follows the ACTIVE object. The fallback to the
        # active splat model made Ctrl+C hijack copies of ordinary objects:
        # with a cube selected, no handle was selected, the fallback grabbed
        # the splat anyway, and FINISHED swallowed the key - copy a cube,
        # paste a splat. If the active object is not a splat handle, this is
        # not our copy: hands off, and drop our clipboard so the following
        # Ctrl+V is Blender's own paste too (last copy wins).
        act = context.view_layer.objects.active
        handles = {r.box_name for r in state.RENDERERS}
        if act is not None and act.select_get() and act.name not in handles:
            _CLIPBOARD.clear()
            return {'PASS_THROUGH'}
        rs = _selected_renderers(context)
        if not rs:
            # Nothing of ours selected - let Blender's own copy run instead.
            return {'PASS_THROUGH'}
        _CLIPBOARD.clear()
        for r in rs:
            box = bpy.data.objects.get(r.box_name)
            if box is None:
                continue
            # The heavy splat arrays are SHARED by reference, never copied:
            # a paste costs a bool mask and a matrix, not another 1.9M splats.
            # Holding the reference also keeps the data alive if the original
            # is deleted before pasting.
            _CLIPBOARD.append({
                "renderer": r,       # donor for GPU-buffer sharing on paste
                "source": r.source,
                "alive": r.alive.copy(),
                "rest_inv": r.rest_inv.copy(),
                "matrix": box.matrix_world.copy(),
                "name": box.name,
            })
        if not _CLIPBOARD:
            return {'PASS_THROUGH'}
        n = len(_CLIPBOARD)
        self.report({'INFO'},
                    f"Copied {n} splat model{'s' if n > 1 else ''}")
        return {'FINISHED'}


class FGS_OT_paste(Operator):
    bl_idname = "fgs.paste_splat"
    bl_label = "Paste Splat Model"
    bl_options = {'REGISTER', 'UNDO'}
    bl_description = ("Paste the splat model(s) from the splat clipboard. "
                      "The copy shares the source data but moves "
                      "independently")

    def execute(self, context):
        if not _CLIPBOARD:
            # Empty clipboard - don't swallow Ctrl+V from Blender's own paste.
            return {'PASS_THROUGH'}
        made = []
        for entry in _CLIPBOARD:
            try:
                empty = boxes.spawn_box(context, entry["matrix"].copy(),
                                        context.scene.fgs_show_bbox,
                                        name=entry["name"])
                donor = entry.get("renderer")
                r = SplatRenderer(entry["source"], empty.name,
                                  entry["rest_inv"].copy(),
                                  share_from=donor)
                r.alive = entry["alive"].copy()
            except Exception as e:
                self.report({'ERROR'}, f"Paste failed: {e}")
                return {'CANCELLED'}
            state.add_renderer(r)
            # Carry the original's reload recipe onto the copy, with the
            # copy's own rest transform and alive mask.
            try:
                from . import persist
                src_box = bpy.data.objects.get(entry["name"])
                if src_box is not None and persist.KEY in src_box:
                    rec = dict(src_box[persist.KEY])
                    rec["rest_inv"] = [float(v) for row in r.rest_inv
                                       for v in row]
                    empty[persist.KEY] = rec
                    persist.update_alive(r)
            except Exception as e:
                print("[SplatBake] copy kept no reload recipe:", e)
            made.append(empty)

        # Select exactly what was pasted, so G / R / S act on it immediately.
        for o in context.selected_objects:
            o.select_set(False)
        for o in made:
            o.select_set(True)
        if made:
            context.view_layer.objects.active = made[-1]
        state.tag_redraw(context)
        n = len(made)
        self.report({'INFO'},
                    f"Pasted {n} model{'s' if n > 1 else ''} in place "
                    f"- press G to move")
        return {'FINISHED'}


class FGS_OT_select_splat(Operator):
    bl_idname = "fgs.select_splat"
    bl_label = "Select Splat (click)"
    bl_description = ("Click a model to select its handle, then use Blender's "
                     "native G / R / S (and X/Y/Z) to transform it")

    def _commit(self, context):
        """Write the masks into Blender data and add ONE undo step for the
        whole session. Doing this per click would mean a compress-and-store
        pass on every splat, which stutters on million-splat models; one step
        per session also matches how people actually undo - 'take back that
        bit of erasing', not 'take back that one splat'."""
        if not getattr(self, "_dirty", False):
            return
        self._dirty = False
        try:
            from . import persist
            persist.push_undo("Delete Splats")
        except Exception as e:
            print("[SplatBake] delete undo step failed:", e)

    def modal(self, context, event):
        if not state.RENDERERS:
            context.workspace.status_text_set(None)
            self._commit(context)
            return {'CANCELLED'}
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            context.workspace.status_text_set(None)
            return {'CANCELLED'}
        if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE',
                          'TRACKPADPAN', 'TRACKPADZOOM'} or event.type.startswith('NUMPAD'):
            return {'PASS_THROUGH'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            region, rv3d = _area_under(context, event)
            context.workspace.status_text_set(None)
            if region is None:
                return {'FINISHED'}
            mx = event.mouse_x - region.x
            my = event.mouse_y - region.y
            r = state.pick_renderer_under_cursor(region, rv3d, mx, my)
            if r is not None:
                box = bpy.data.objects.get(r.box_name)
                if box is not None:
                    for o in context.selected_objects:
                        o.select_set(False)
                    box.select_set(True)
                    context.view_layer.objects.active = box
                    state.set_active(r)
                    self.report({'INFO'}, "Selected - press G / R / S (X/Y/Z) to transform")
            return {'FINISHED'}
        return {'RUNNING_MODAL'}

    def invoke(self, context, event):
        if not state.RENDERERS:
            self.report({'WARNING'}, "Load a splat first")
            return {'CANCELLED'}
        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set(
            "Click a model to select it, then G / R / S    ESC: cancel")
        return {'RUNNING_MODAL'}


class FGS_OT_move_splat(Operator):
    bl_idname = "fgs.move_splat"
    bl_label = "Drag-Transform (no keys)"
    bl_description = ("Alternative to native G/R/S: click a model and drag to "
                     "MOVE, Shift-drag to ROTATE, Ctrl-drag to SCALE. ESC/RMB finishes")

    def modal(self, context, event):
        if not state.RENDERERS:
            context.workspace.status_text_set(None)
            return {'CANCELLED'}
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            context.workspace.status_text_set(None)
            self._commit(context)
            return {'FINISHED'}
        if not self._drag and (
                event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE',
                               'TRACKPADPAN', 'TRACKPADZOOM'}
                or event.type.startswith('NUMPAD')):
            return {'PASS_THROUGH'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS' and not self._drag:
            region, rv3d = _area_under(context, event)
            if region is None:
                return {'RUNNING_MODAL'}
            mx = event.mouse_x - region.x
            my = event.mouse_y - region.y
            r = state.pick_renderer_under_cursor(region, rv3d, mx, my)
            if r is None:
                return {'RUNNING_MODAL'}
            box = bpy.data.objects.get(r.box_name)
            if box is None:
                return {'RUNNING_MODAL'}
            state.set_active(r)
            self._box = box
            self._region = region
            self._rv3d = rv3d
            self._m0 = box.matrix_world.copy()
            self._pivot = r.model_matrix() @ r.center_local
            self._mx0, self._my0 = mx, my
            self._start = view3d_utils.region_2d_to_location_3d(
                region, rv3d, (mx, my), self._pivot)
            self._mode = ('ROTATE' if event.shift
                          else 'SCALE' if event.ctrl else 'MOVE')
            self._drag = True
            return {'RUNNING_MODAL'}

        if event.type == 'MOUSEMOVE' and self._drag:
            mx = event.mouse_x - self._region.x
            my = event.mouse_y - self._region.y
            if self._mode == 'MOVE':
                now = view3d_utils.region_2d_to_location_3d(
                    self._region, self._rv3d, (mx, my), self._pivot)
                self._box.matrix_world = Matrix.Translation(now - self._start) @ self._m0
            elif self._mode == 'ROTATE':
                angle = (mx - self._mx0) * 0.01
                M = (Matrix.Translation(self._pivot)
                     @ Matrix.Rotation(angle, 4, 'Z')
                     @ Matrix.Translation(-self._pivot))
                self._box.matrix_world = M @ self._m0
            else:  # SCALE
                f = min(max(math.exp((my - self._my0) * 0.005), 0.05), 20.0)
                M = (Matrix.Translation(self._pivot)
                     @ Matrix.Scale(f, 4)
                     @ Matrix.Translation(-self._pivot))
                self._box.matrix_world = M @ self._m0
            state.tag_redraw(context)
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE' and self._drag:
            self._drag = False
            self._box = None
            return {'RUNNING_MODAL'}
        return {'RUNNING_MODAL'}

    def invoke(self, context, event):
        if not state.RENDERERS:
            self.report({'WARNING'}, "Load a splat first")
            return {'CANCELLED'}
        self._drag = False
        self._box = None
        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set(
            "Drag: move    Shift-drag: rotate    Ctrl-drag: scale    "
            "MMB/wheel: navigate    ESC/RMB: finish")
        return {'RUNNING_MODAL'}


class FGS_OT_delete_mode(Operator):
    bl_idname = "fgs.delete_mode"
    bl_label = "Delete Splats (click)"
    bl_description = ("Click-to-delete mode. LMB removes the splat under the "
                     "cursor, Z undoes, MMB/wheel navigates, ESC/RMB finishes")

    def modal(self, context, event):
        if not state.RENDERERS:
            context.workspace.status_text_set(None)
            return {'CANCELLED'}
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            context.workspace.status_text_set(None)
            return {'FINISHED'}
        if event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE',
                          'TRACKPADPAN', 'TRACKPADZOOM'} or event.type.startswith('NUMPAD'):
            return {'PASS_THROUGH'}
        if event.type == 'Z' and event.value == 'PRESS':
            state.undo_delete()
            state.tag_redraw(context)
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            region, rv3d = _area_under(context, event)
            if region is None:
                return {'RUNNING_MODAL'}
            if state.delete_under_cursor(region, rv3d,
                                         event.mouse_x - region.x,
                                         event.mouse_y - region.y):
                self._dirty = True
            state.tag_redraw(context)
            return {'RUNNING_MODAL'}
        return {'RUNNING_MODAL'}

    def invoke(self, context, event):
        if not state.RENDERERS:
            self.report({'WARNING'}, "Load a splat first")
            return {'CANCELLED'}
        self._dirty = False
        context.window_manager.modal_handler_add(self)
        context.workspace.status_text_set(
            "LMB: delete splat    Z: undo one    MMB / wheel: navigate    "
            "ESC / RMB: finish (then Ctrl+Z undoes the session)")
        return {'RUNNING_MODAL'}


def _commit_masks(message):
    try:
        from . import persist
        persist.push_undo(message)
    except Exception as e:
        print("[SplatBake] undo step failed:", e)


class FGS_OT_undo_delete(Operator):
    bl_idname = "fgs.undo_delete"
    bl_label = "Undo Last Delete"

    def execute(self, context):
        state.undo_delete()
        _commit_masks("Undo Splat Delete")
        state.tag_redraw(context)
        return {'FINISHED'}


class FGS_OT_restore(Operator):
    bl_idname = "fgs.restore_splats"
    bl_label = "Restore All Splats"

    def execute(self, context):
        state.restore_all()
        _commit_masks("Restore Splats")
        state.tag_redraw(context)
        return {'FINISHED'}


def _grade_np(rgb, sc):
    """Apply the viewport colour grade to baked vertex colours, then convert
    sRGB -> linear (colour attributes are read as scene-linear by shaders)."""
    tint = np.array(tuple(sc.fgs_tint), dtype=np.float32)
    c = rgb.astype(np.float32) * float(sc.fgs_exposure) * tint
    lum = c @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    c = lum[:, None] + (c - lum[:, None]) * float(sc.fgs_saturation)
    c = np.power(np.clip(c, 0.0, None), 1.0 / float(sc.fgs_gamma))
    c = np.clip(c, 0.0, 1.0)
    c = np.where(c <= 0.04045, c / 12.92,
                 np.power((c + 0.055) / 1.055, 2.4))
    return c.astype(np.float32)


_BAKE_SIGMA = 2.8284271    # quad half-extent = 2*sqrt(2) sigma: the exact
                           # footprint of the viewport quads (corner +/-2,
                           # gaussian discarded above A = 4), so baked discs
                           # cover the same area as the drawn splats
_BAKE_EDGE = 0.0183156389  # exp(-4): kernel value at the quad edge


def _lnk(nt, dst, v):
    """Wire a socket into `dst`, or assign a constant."""
    if isinstance(v, bpy.types.NodeSocket):
        nt.links.new(v, dst)
    else:
        dst.default_value = v


def _math(nt, op, a=None, b=None, c=None):
    n = nt.nodes.new('ShaderNodeMath')
    n.operation = op
    for sock, v in zip(n.inputs, (a, b, c)):
        if v is not None:
            _lnk(nt, sock, v)
    return n.outputs[0]


def _vmath(nt, op, a=None, b=None, scale=None):
    n = nt.nodes.new('ShaderNodeVectorMath')
    n.operation = op
    if a is not None:
        _lnk(nt, n.inputs[0], a)
    if b is not None:
        _lnk(nt, n.inputs[1], b)
    if scale is not None:
        _lnk(nt, n.inputs["Scale"], scale)
    out = "Value" if op in {'DOT_PRODUCT', 'LENGTH', 'DISTANCE'} else 0
    return n.outputs[out]


def _nodes_srgb_to_linear(nt, vec):
    """Exact per-channel sRGB -> scene-linear decode (the fragment shader's
    srgb_to_linear), so the render engine's re-encode is a no-op."""
    sep = nt.nodes.new('ShaderNodeSeparateXYZ')
    nt.links.new(vec, sep.inputs[0])
    outs = []
    for ch in ("X", "Y", "Z"):
        cch = sep.outputs[ch]
        t = _math(nt, 'GREATER_THAN', cch, 0.04045)
        lo = _math(nt, 'DIVIDE', cch, 12.92)
        hi = _math(nt, 'POWER',
                   _math(nt, 'DIVIDE', _math(nt, 'ADD', cch, 0.055), 1.055),
                   2.4)
        outs.append(_math(nt, 'MULTIPLY_ADD', t,
                          _math(nt, 'SUBTRACT', hi, lo), lo))
    comb = nt.nodes.new('ShaderNodeCombineXYZ')
    for ch, o in zip(("X", "Y", "Z"), outs):
        nt.links.new(o, comb.inputs[ch])
    return comb.outputs[0]


def _nodes_grade(nt, vec, sc):
    """The viewport colour grade (exposure/tint, saturation, gamma) rebuilt in
    nodes with the scene's current values baked in, then sRGB -> linear."""
    tint = tuple(sc.fgs_tint)
    ex = float(sc.fgs_exposure)
    c = _vmath(nt, 'MULTIPLY', vec,
               (ex * tint[0], ex * tint[1], ex * tint[2]))
    lum = _vmath(nt, 'DOT_PRODUCT', c, (0.2126, 0.7152, 0.0722))
    lumv_n = nt.nodes.new('ShaderNodeCombineXYZ')
    for ch in ("X", "Y", "Z"):
        nt.links.new(lum, lumv_n.inputs[ch])
    c = _vmath(nt, 'ADD',
               _vmath(nt, 'SCALE', _vmath(nt, 'SUBTRACT', c,
                                          lumv_n.outputs[0]),
                      scale=float(sc.fgs_saturation)),
               lumv_n.outputs[0])
    g = nt.nodes.new('ShaderNodeGamma')
    nt.links.new(c, g.inputs["Color"])
    g.inputs["Gamma"].default_value = 1.0 / max(float(sc.fgs_gamma), 1e-3)
    return _nodes_srgb_to_linear(nt, g.outputs[0])


def _nodes_sh_deg1(nt, base_vec, rinv_rows):
    """Live degree-1 view-dependent colour: the node-graph twin of the vertex
    shader's first SH band. The view direction (splat -> camera, flipped) is
    rotated into the model's data frame with the bake-time inverse model
    rotation - exactly the shader's cam_local convention."""
    geo = nt.nodes.new('ShaderNodeNewGeometry')
    d_world = _vmath(nt, 'SCALE', geo.outputs["Incoming"], scale=-1.0)
    comps = [_vmath(nt, 'DOT_PRODUCT', d_world, tuple(row))
             for row in rinv_rows]
    comb = nt.nodes.new('ShaderNodeCombineXYZ')
    for ch, o in zip(("X", "Y", "Z"), comps):
        nt.links.new(o, comb.inputs[ch])
    d = _vmath(nt, 'NORMALIZE', comb.outputs[0])
    sep = nt.nodes.new('ShaderNodeSeparateXYZ')
    nt.links.new(d, sep.inputs[0])
    dx, dy, dz = sep.outputs["X"], sep.outputs["Y"], sep.outputs["Z"]

    def coef(attr_name):
        a = nt.nodes.new('ShaderNodeAttribute')
        a.attribute_name = attr_name
        # decode the 0..1 offset encoding back to the raw +/-2 range
        return _vmath(nt, 'ADD',
                      _vmath(nt, 'SCALE', a.outputs["Color"], scale=4.0),
                      (-2.0, -2.0, -2.0))

    term = _vmath(nt, 'ADD',
                  _vmath(nt, 'ADD',
                         _vmath(nt, 'SCALE', coef("SH0"),
                                scale=_math(nt, 'MULTIPLY', dy, -1.0)),
                         _vmath(nt, 'SCALE', coef("SH1"), scale=dz)),
                  _vmath(nt, 'SCALE', coef("SH2"),
                         scale=_math(nt, 'MULTIPLY', dx, -1.0)))
    c = _vmath(nt, 'ADD', base_vec,
               _vmath(nt, 'SCALE', term, scale=0.4886025119029199))
    c = _vmath(nt, 'MAXIMUM', c, (0.0, 0.0, 0.0))
    return _vmath(nt, 'MINIMUM', c, (1.0, 1.0, 1.0))


_ATLAS_UV = "SplatCol"     # dedicated UV layer for the colour atlas


def _color_atlas(name, col):
    """Pack one texel per splat into a square 32-bit float image and return
    (image, uv_per_splat (n,2)).

    Why a texture rather than vertex colours: the texel is addressed exactly
    (nearest-neighbour, texel centres, Non-Color, full float) so the value
    reaching the shader is bit-for-bit the colour we computed - no
    interpolation across the quad, no colour-management guesswork, and it
    survives glTF/FBX export, which vertex colours often do not."""
    n = len(col)
    side = max(1, int(math.ceil(math.sqrt(n))))
    if side > 8192:
        raise RuntimeError("too many splats for one colour atlas")
    img = bpy.data.images.new(name + "_cols", side, side,
                              alpha=False, float_buffer=True)
    try:
        img.colorspace_settings.name = 'Non-Color'   # store values verbatim
    except Exception:
        pass
    px = np.zeros((side * side, 4), np.float32)
    px[:n, :3] = col
    px[:, 3] = 1.0
    img.pixels.foreach_set(px.ravel())
    try:
        img.pack()          # keep the colours inside the .blend
    except Exception:
        pass
    i = np.arange(n, dtype=np.float32)
    xs = np.mod(i, side)
    ys = np.floor(i / side)          # row 0 = bottom, matching UV v = 0
    uv = np.empty((n, 2), np.float32)
    uv[:, 0] = (xs + 0.5) / side
    uv[:, 1] = (ys + 0.5) / side
    return img, uv


def _aa_camera(context, r):
    """(modelview, fx, fy, is_persp) for AA compensation, from the scene
    camera at render resolution if there is one, else the current viewport -
    the same choice the CAMERA colour mode makes, so colour and opacity are
    frozen for one consistent viewpoint. Returns None if neither exists."""
    try:
        cam = context.scene.camera
        if cam is not None:
            rd = context.scene.render
            w = rd.resolution_x * rd.resolution_percentage / 100.0
            h = rd.resolution_y * rd.resolution_percentage / 100.0
            dg = context.evaluated_depsgraph_get()
            proj = cam.calc_matrix_camera(dg, x=int(w), y=int(h))
            mv = cam.matrix_world.inverted() @ r.model_matrix()
            return (np.array(mv, np.float32), 0.5 * w * proj[0][0],
                    0.5 * h * proj[1][1],
                    getattr(cam.data, "type", 'PERSP') == 'PERSP')
        for a in context.screen.areas:
            if a.type != 'VIEW_3D':
                continue
            rv = a.spaces.active.region_3d
            reg = next((rg for rg in a.regions if rg.type == 'WINDOW'), None)
            if rv is None or reg is None:
                continue
            mv = rv.view_matrix @ r.model_matrix()
            return (np.array(mv, np.float32),
                    0.5 * reg.width * rv.window_matrix[0][0],
                    0.5 * reg.height * rv.window_matrix[1][1],
                    bool(rv.is_perspective))
    except Exception as e:
        print("[SplatBake] AA camera unavailable:", e)
    return None


def _aa_factors(centers, quat, scale, mv, fx, fy, is_persp):
    """The viewport's antiAlias opacity compensation, in numpy.

    Same formula as the shader: project each splat's 3D covariance to 2D,
    dilate it by the reference viewer's +0.3 px, and scale opacity by
    sqrt(det_original / det_dilated). The dilation matters most for splats
    that land smaller than a pixel, so the effect is a gentle fade of distant
    splats and no change at all up close - which is exactly what the toggle
    does live.

    It is VIEW-DEPENDENT, like the SH colour: the bake freezes it for the
    chosen camera. Returns an (N,) multiplier in [0,1].
    """
    n = len(centers)
    w = quat[:, 0]; x = quat[:, 1]; y = quat[:, 2]; z = quat[:, 3]
    R = np.empty((n, 3, 3), np.float32)
    R[:, 0, 0] = 1 - 2 * (y * y + z * z)
    R[:, 0, 1] = 2 * (x * y - w * z)
    R[:, 0, 2] = 2 * (x * z + w * y)
    R[:, 1, 0] = 2 * (x * y + w * z)
    R[:, 1, 1] = 1 - 2 * (x * x + z * z)
    R[:, 1, 2] = 2 * (y * z - w * x)
    R[:, 2, 0] = 2 * (x * z - w * y)
    R[:, 2, 1] = 2 * (y * z + w * x)
    R[:, 2, 2] = 1 - 2 * (x * x + y * y)
    M = R * scale[:, None, :]                     # R @ diag(scale)
    Sig = M @ np.transpose(M, (0, 2, 1))          # 3D covariance
    Wm = np.asarray(mv, np.float32)[:3, :3]
    Sig_c = Wm @ Sig @ Wm.T
    p = centers @ Wm.T + np.asarray(mv, np.float32)[:3, 3]
    zc = np.where(np.abs(p[:, 2]) < 1e-6, 1e-6, p[:, 2])
    J = np.zeros((n, 3, 3), np.float32)
    if is_persp:
        J[:, 0, 0] = fx / zc
        J[:, 0, 2] = -fx * p[:, 0] / (zc * zc)
        J[:, 1, 1] = fy / zc
        J[:, 1, 2] = -fy * p[:, 1] / (zc * zc)
    else:
        J[:, 0, 0] = fx
        J[:, 1, 1] = fy
    cov = J @ Sig_c @ np.transpose(J, (0, 2, 1))
    a0 = cov[:, 0, 0]; c0 = cov[:, 1, 1]; b = cov[:, 0, 1]
    det0 = a0 * c0 - b * b
    a = a0 + 0.3; c = c0 + 0.3                    # reference dilation
    D = a * c - b * b
    return np.sqrt(np.clip(det0 / np.maximum(D, 1e-9),
                           0.0, 1.0)).astype(np.float32)


def _bake_material(name, soft, sc, live_rinv=None, color_img=None,
                   kernel='NORM'):
    """Emission material for baked splats.

    soft      : radial gaussian alpha times per-splat opacity, using the
                viewport's NORMALISED kernel - exactly zero at the quad edge,
                so quads never show a visible clip line.
    live_rinv : three rows of the inverse model 3x3 -> build the LIVE
                degree-1 SH chain; Col/SH* attributes are stored raw and the
                full viewport grade + sRGB->linear runs in nodes.
                None -> colours were graded/linearised at bake time.
    color_img : per-splat colour atlas; when given the base colour is read
                from it through the SplatCol UV layer instead of the "Col"
                vertex-colour attribute.
    """
    mat = bpy.data.materials.new(name + "_mat")
    mat.use_nodes = True
    nt = mat.node_tree
    for nd in list(nt.nodes):
        nt.nodes.remove(nd)
    out = nt.nodes.new('ShaderNodeOutputMaterial')
    out.location = (900, 0)
    if color_img is not None:
        cuv = nt.nodes.new('ShaderNodeUVMap')
        cuv.uv_map = _ATLAS_UV
        tex = nt.nodes.new('ShaderNodeTexImage')
        tex.image = color_img
        tex.interpolation = 'Closest'    # exact texel, no bleed between splats
        tex.extension = 'EXTEND'
        nt.links.new(cuv.outputs["UV"], tex.inputs["Vector"])
        base = tex.outputs["Color"]
    else:
        col_attr = nt.nodes.new('ShaderNodeAttribute')
        col_attr.attribute_name = "Col"
        base = col_attr.outputs["Color"]
    if live_rinv is not None:
        base = _nodes_sh_deg1(nt, base, live_rinv)
        base = _nodes_grade(nt, base, sc)
    emis = nt.nodes.new('ShaderNodeEmission')
    nt.links.new(base, emis.inputs["Color"])
    if not soft:
        nt.links.new(emis.outputs["Emission"], out.inputs["Surface"])
        return mat

    uv = nt.nodes.new('ShaderNodeUVMap')
    uv.uv_map = "UVMap"
    # A = 4 * (u^2 + v^2): quad edge = A 4, matching the viewport quads.
    # 'NORM'  : alpha = (exp(-A) - exp(-4)) / (1 - exp(-4)) - the viewport's
    #           normalised 'PC' / 'V215' kernel, exactly zero at the quad edge.
    # 'PLAIN' : alpha = exp(-A) - the viewport's 'SOFT (classic Blender)'
    #           kernel, which never quite reaches zero and so reads softer.
    a4 = _math(nt, 'MULTIPLY',
               _vmath(nt, 'DOT_PRODUCT', uv.outputs["UV"], uv.outputs["UV"]),
               4.0)
    falloff = _math(nt, 'EXPONENT', _math(nt, 'MULTIPLY', a4, -1.0))
    if kernel == 'PLAIN':
        gauss = falloff
    else:
        gauss = _math(nt, 'MAXIMUM',
                      _math(nt, 'DIVIDE',
                            _math(nt, 'SUBTRACT', falloff, _BAKE_EDGE),
                            1.0 - _BAKE_EDGE),
                      0.0)
    opac = nt.nodes.new('ShaderNodeAttribute')
    opac.attribute_name = "Opac"
    alpha = _math(nt, 'MULTIPLY', gauss, opac.outputs["Fac"])
    transp = nt.nodes.new('ShaderNodeBsdfTransparent')
    mix = nt.nodes.new('ShaderNodeMixShader')
    mix.location = (700, 0)
    _lnk(nt, mix.inputs[0], alpha)
    nt.links.new(transp.outputs["BSDF"], mix.inputs[1])
    nt.links.new(emis.outputs["Emission"], mix.inputs[2])
    nt.links.new(mix.outputs["Shader"], out.inputs["Surface"])

    # Order-independent (dithered/hashed) transparency so overlapping discs
    # don't fight over draw order. Names differ across Blender versions.
    for attr, val in (("surface_render_method", 'DITHERED'),
                      ("blend_method", 'HASHED'),
                      ("shadow_method", 'NONE')):
        try:
            setattr(mat, attr, val)
        except Exception:
            pass
    # If shadows do render (engine-dependent), keep them alpha-true rather
    # than solid. (Shadow casting itself is disabled on the baked OBJECT:
    # the old use_transparent_shadow=False made Cycles cast one big opaque
    # shadow blob from the whole disc cloud.)
    try:
        mat.use_transparent_shadow = True
    except Exception:
        pass
    return mat


def _disc_geometry(centers, quat, scale, aniso, splat_scale, orient_to=None,
                   align_normal=None):
    """Pure-numpy disc geometry: per-splat quads oriented by the two largest
    covariance axes, sized like the viewport quads. Returns
    (verts (4n,3) local, faces (2n,3), uvs (6n,2))."""
    n = len(centers)
    scale = np.maximum(scale.astype(np.float32), 1e-6)
    # De-spike like the viewport: clamp each axis to aniso x per-splat median.
    # aniso <= 0 means OFF - the shader guards this with `if (aniso > 0.0)`
    # and the bake MUST too: multiplying by 0 collapsed every disc to zero
    # area, so the baked mesh existed but was completely invisible.
    if float(aniso) > 0.0:
        med = np.median(scale, axis=1)
        scale = np.minimum(scale, (float(aniso) * med)[:, None])
    ext = _BAKE_SIGMA * max(float(splat_scale), 1e-3)

    w, x, y, z = quat[:, 0], quat[:, 1], quat[:, 2], quat[:, 3]
    R = np.empty((n, 3, 3), np.float32)
    R[:, 0, 0] = 1 - 2 * (y * y + z * z)
    R[:, 0, 1] = 2 * (x * y - w * z)
    R[:, 0, 2] = 2 * (x * z + w * y)
    R[:, 1, 0] = 2 * (x * y + w * z)
    R[:, 1, 1] = 1 - 2 * (x * x + z * z)
    R[:, 1, 2] = 2 * (y * z - w * x)
    R[:, 2, 0] = 2 * (x * z - w * y)
    R[:, 2, 1] = 2 * (y * z + w * x)
    R[:, 2, 2] = 1 - 2 * (x * x + y * y)

    order = np.argsort(scale, axis=1)      # ascending; two largest last
    ni = np.arange(n)
    a1, a2 = order[:, 2], order[:, 1]
    t = R[ni, :, a1] * (scale[ni, a1] * ext)[:, None]
    u = R[ni, :, a2] * (scale[ni, a2] * ext)[:, None]

    # Re-seat each disc into the regularised surface plane (lit bakes only).
    #
    # By default a disc spans its own two largest axes, so its facing is set by
    # that splat's own orientation - and on a real capture those orientations
    # are noisy enough that shading them averages out to a flat tint. Rebuilding
    # the disc in the plane PERPENDICULAR to the smoothed normal makes
    # neighbouring splats share a facing, which is what lets a lamp shade form.
    #
    # The disc keeps its two largest extents, so it covers the same area and the
    # model looks the same unlit; only which way it faces changes. Flat shading
    # then reads the coherent normal straight off the geometry, with no custom
    # split normals - which matters, because setting those per loop on millions
    # of splats through the Python API is far too slow to be usable.
    if align_normal is not None:
        N = np.ascontiguousarray(align_normal, np.float32)
        N = N / np.maximum(np.linalg.norm(N, axis=1, keepdims=True), 1e-8)
        # Any vector not parallel to N gives a valid in-plane direction. Pick
        # the world axis N leans on least, so the cross product stays stable.
        helper = np.zeros((n, 3), np.float32)
        helper[np.arange(n), np.argmin(np.abs(N), axis=1)] = 1.0
        e1 = np.cross(N, helper)
        e1 /= np.maximum(np.linalg.norm(e1, axis=1, keepdims=True), 1e-8)
        e2 = np.cross(N, e1)
        len_t = np.linalg.norm(t, axis=1, keepdims=True)
        len_u = np.linalg.norm(u, axis=1, keepdims=True)
        t = e1 * len_t
        u = e2 * len_u
        # Winding chosen so the face normal is +N, giving the whole model a
        # consistent outward facing without a second orientation pass.
        wrong = np.einsum('ij,ij->i', np.cross(t, u), N) < 0.0
        if wrong.any():
            u[wrong] = -u[wrong]
        orient_to = None                    # already oriented, do not re-flip

    # Consistent outward winding, for lit bakes only.
    #
    # A splat's quaternion fixes its axes but not their SIGN, so face normals
    # come out pointing in and out at random. That was assumed harmless on the
    # grounds that renderers flip backfacing normals toward the viewer - but
    # that is exactly what breaks it: with random signs, EVERY disc ends up
    # flipped to face the camera, N.L becomes near-identical across the whole
    # model, and the lighting collapses to one flat tint with no form.
    #
    # Flipping the winding so each normal points away from the model centre
    # gives neighbouring splats on a surface a shared orientation, which is
    # what lets a lamp actually shade the form. Negating `u` (rather than `t`)
    # reverses the cross product; the kernel UVs are radially symmetric, so
    # the disc's appearance is unaffected.
    if orient_to is not None:
        nrm = np.cross(t, u)
        away = np.einsum('ij,ij->i', nrm, centers - np.asarray(
            orient_to, np.float32)[None, :]) < 0.0
        if away.any():
            u[away] = -u[away]

    verts = np.empty((4 * n, 3), np.float32)
    verts[0::4] = centers - t - u
    verts[1::4] = centers + t - u
    verts[2::4] = centers + t + u
    verts[3::4] = centers - t + u

    base = (np.arange(n) * 4).astype(np.int64)
    faces = np.empty((2 * n, 3), np.int64)
    faces[0::2, 0] = base; faces[0::2, 1] = base + 1; faces[0::2, 2] = base + 2
    faces[1::2, 0] = base; faces[1::2, 1] = base + 2; faces[1::2, 2] = base + 3

    uv_unit = np.array([[-1, -1], [1, -1], [1, 1],
                        [-1, -1], [1, 1], [-1, 1]], np.float32)  # 6 loops/splat
    uvs = np.tile(uv_unit, (n, 1))
    return verts, faces, uvs


def _fast_tri_mesh(name, verts, tris):
    """Build an all-triangle mesh via foreach_set - orders of magnitude
    faster than from_pydata's Python-list path on large bakes.

    The low-level add()/foreach_set() route depends on Blender-version
    details of the face-offset array, so the result is VERIFIED by element
    count and falls back to the slow-but-universal from_pydata if anything
    is off. A silently malformed mesh renders as nothing, which is worth a
    few lines of paranoia."""
    mesh = bpy.data.meshes.new(name)
    nv, nf = len(verts), len(tris)
    ok = False
    try:
        mesh.vertices.add(nv)
        mesh.vertices.foreach_set(
            "co", np.ascontiguousarray(verts, np.float32).ravel())
        mesh.loops.add(nf * 3)
        mesh.loops.foreach_set(
            "vertex_index", np.ascontiguousarray(tris, np.int32).ravel())
        mesh.polygons.add(nf)
        mesh.polygons.foreach_set(
            "loop_start", np.arange(0, nf * 3, 3, dtype=np.int32))
        try:
            mesh.polygons.foreach_set("loop_total", np.full(nf, 3, np.int32))
        except Exception:
            pass                               # read-only in newer Blenders
        mesh.update(calc_edges=True)
        ok = (len(mesh.vertices) == nv and len(mesh.polygons) == nf
              and len(mesh.loops) == nf * 3)
    except Exception as e:
        print("[SplatBake] fast mesh build failed, using fallback:", e)
    if not ok:
        print("[SplatBake] fast mesh path rejected - rebuilding")
        mesh.clear_geometry()
        mesh.from_pydata(np.asarray(verts).tolist(), [],
                         np.asarray(tris).tolist())
        mesh.update()
    return mesh


def _bake_cards(r, context, cap_pct, soft=True, boost=1.0, min_opacity=0.0,
                sh_mode='BASE', cam_world=None, use_texture=True, lit=False,
                delight=0.0, emission_mix=0.0, cast_shadows=False,
                normal_mode='CAMERA', shadow_strength=1.0,
                light_gain=3.0, sh_k=None, kernel='NORM', aa_view=None):
    """Build a renderable mesh for the active model: one gaussian disc per
    splat (2 triangles), sized and soft-clipped exactly like the viewport
    quads. Colour detail depends on sh_mode:
      BASE    flat base colour (fastest, least detail)
      CAMERA  FULL-degree SH evaluated toward `cam_world` - the exact
              viewport colour for that camera position, baked in
      LIVE1   degree-1 SH evaluated live in the material, so colours shift
              with the render camera (raw attrs + node-side grade)
    Returns (object, count, mode_used)."""
    sc = context.scene
    src = r.source
    idx = np.where(r.alive)[0]
    if len(idx) == 0:
        raise RuntimeError("model has no live splats")

    # Percentage of THIS model's live splats (1.20.9): the old absolute cap
    # meant one number for every model - starving a big scan at the same
    # setting that was meaninglessly generous for a small one. The share is
    # taken of the live count, so 100% bakes exactly what the viewport
    # displays.
    cap = max(1, int(round(len(idx) * float(cap_pct) / 100.0)))

    op_full = src.get("opacity")
    # Near-invisible splats cost a transparency hit per ray but contribute
    # almost nothing: culling them is the single biggest Cycles speed-up.
    # Never let it empty the model, though - if a scene's opacities are all
    # low, an empty bake is far worse than a slow one.
    if op_full is not None and min_opacity > 0.0:
        keep = op_full[idx].astype(np.float32) >= float(min_opacity)
        if keep.sum() >= max(1, 0.01 * len(idx)):
            idx = idx[keep]
    # Over budget: importance-sample by opacity (like the loader's weighted
    # subsample) so solid structure survives and haze goes first.
    # Efraimidis-Spirakis keys (log u / w, take the largest) give weighted
    # sampling without replacement in one O(n) pass - numpy's choice(p=...)
    # without replacement uses a rejection loop that crawls on millions.
    if len(idx) > cap:
        rng = np.random.default_rng(0)
        u = rng.random(len(idx))
        if op_full is not None:
            wgt = np.clip(op_full[idx].astype(np.float64), 1e-4, None)
            keys = np.log(np.maximum(u, 1e-12)) / wgt
        else:
            keys = u
        sel = np.argpartition(keys, -int(cap))[-int(cap):]
        idx = np.sort(idx[sel])
    n = len(idx)

    mode = sh_mode if (sh_mode == 'BASE' or r.has_sh) else 'BASE'
    if lit and mode == 'LIVE1':
        # LIVE1 stores RAW colour and rebuilds the grade + view-dependent
        # term in the material. The lit material has no such chain (the
        # colour is an albedo, not a radiance), so raw values would render
        # wrong. Camera-baked SH keeps the directional detail and bakes the
        # grade in, which is what an albedo needs.
        mode = 'CAMERA'

    centers = src["xyz"][idx].astype(np.float32)
    quat = src["quat"][idx].astype(np.float32)          # w, x, y, z
    opacity = (np.clip(op_full[idx].astype(np.float32), 0.0, 1.0)
               if op_full is not None else np.ones(n, np.float32))
    opacity = np.clip(opacity * float(boost), 0.0, 1.0)
    if aa_view is not None:
        # The preview's AA compensation is a per-splat opacity factor, so it
        # bakes exactly - frozen for the chosen camera, like the SH colour.
        try:
            mv, fx, fy, is_persp = aa_view
            opacity = np.clip(
                opacity * _aa_factors(centers, quat,
                                      src["scale"][idx].astype(np.float32),
                                      mv, fx, fy, is_persp), 0.0, 1.0)
        except Exception as e:
            print("[SplatBake] AA compensation skipped:", e)

    # Lit bakes need consistently oriented normals; the emission path does not
    # use normals at all, so it keeps the cheaper unoriented winding.
    #
    # (1.20.10) "Align Discs to Surface" was removed here. It re-seated every
    # disc into the smoothed surface plane, and under the OLD physical
    # lighting model that was the difference between lighting working and
    # not. Under the preview-matched model (1.20.7) it became the opposite:
    # coherently oriented, elongated discs each shaded with one uniform tint
    # read as literal brushstrokes on the model, while the raw orientations
    # produce fine-grained variance that blends like the capture's own fuzz.
    # Found by the user isolating the toggle. Consistent outward WINDING
    # stays (radial from the model centre) - True Orientation depends on it,
    # and _disc_geometry keeps its align_normal input for the day a gentler
    # blend earns its way back.
    orient_to = None
    if lit:
        from .splatcore.loaders import robust_bounds
        _lo, _hi = robust_bounds(centers)
        orient_to = ((_lo + _hi) * 0.5).astype(np.float32)
    verts, faces, uvs = _disc_geometry(centers, quat, src["scale"][idx],
                                       sc.fgs_despike, sc.fgs_splat_scale,
                                       orient_to=orient_to)
    M = np.array(r.model_matrix(), dtype=np.float32)
    vh = np.concatenate([verts, np.ones((4 * n, 1), np.float32)], axis=1)
    world = (vh @ M.T)[:, :3]

    # -- colour, per mode ---------------------------------------------
    if mode == 'CAMERA':
        try:
            from .splatcore import sh as _shmod
            cw = cam_world if cam_world is not None else Vector((0.0, 0.0, 0.0))
            cam_local = r.model_matrix().inverted() @ Vector(cw)
            cl = np.array((cam_local.x, cam_local.y, cam_local.z), np.float32)
            shc = r.sh[idx].astype(np.float32)
            if sh_k is not None:
                # Match the preview's View Colour Quality: eval_sh reads the
                # degree straight off the coefficient count, so slicing IS
                # the quality setting (0 / 3 / 8 / 15 coefficients).
                shc = shc[:, :int(sh_k), :]
            rgb_view = _shmod.eval_sh(centers, r.dc[idx].astype(np.float32),
                                      shc, cl)
            col = _grade_np(rgb_view, sc)
        except Exception as e:
            # Never lose the whole bake over the optional colour refinement.
            print("[SplatBake] SH evaluation failed, base colour:", e)
            mode = 'BASE'
            col = _grade_np(src["rgb"][idx], sc)
    elif mode == 'LIVE1':
        col = src["rgb"][idx].astype(np.float32)   # raw: graded in the nodes
    else:
        col = _grade_np(src["rgb"][idx], sc)

    # -- de-lighting (scene-lit bakes only) ----------------------------
    # Only meaningful when the colour is about to become an ALBEDO. On the
    # emission path the captured lighting is the point, and removing it would
    # be destroying the very thing the user asked to reproduce.
    if lit and float(delight) > 0.0:
        try:
            from . import lighting as _lit
            nrm = _lit.splat_normals(quat, src["scale"][idx].astype(np.float32))
            col = _lit.delight(col, nrm, float(delight))
        except Exception as e:
            print("[SplatBake] de-lighting failed, colours unchanged:", e)
    if lit:
        # Albedo, not radiance: clamp before it becomes a texel.
        try:
            from . import lighting as _lit2
            col = _lit2.clamp_albedo(col)
        except Exception:
            pass

    nm = bpy.data.objects.get(r.box_name)
    nm = (nm.name if nm else "Splat") + "_baked"
    mesh = _fast_tri_mesh(nm, world, faces)

    # -- base colour: texture atlas (exact) or vertex colours ----------
    color_img = None
    if use_texture:
        try:
            color_img, cuv = _color_atlas(nm, col)
            # 6 loops per splat, all pointing at that splat's own texel
            mesh.uv_layers.new(name=_ATLAS_UV).data.foreach_set(
                "uv", np.repeat(cuv, 6, axis=0).ravel())
        except Exception as e:
            print("[SplatBake] colour atlas failed, vertex colours:", e)
            color_img = None
    if color_img is None:
        rgba = np.concatenate([np.repeat(col, 4, axis=0),
                               np.ones((4 * n, 1), np.float32)], axis=1)
        mesh.color_attributes.new(
            "Col", 'FLOAT_COLOR', 'POINT').data.foreach_set(
                "color", rgba.ravel())
    if soft:
        mesh.attributes.new("Opac", 'FLOAT', 'POINT').data.foreach_set(
            "value", np.repeat(opacity, 4).astype(np.float32))
        mesh.uv_layers.new(name="UVMap").data.foreach_set("uv", uvs.ravel())
    live_rinv = None
    if mode == 'LIVE1':
        # Degree-1 coefficients, offset-encoded into 0..1 (raw range +/-2)
        # so no colour-attribute pipeline can clamp the negatives away.
        for k in range(3):
            enc = np.clip(r.sh[idx, k, :].astype(np.float32) * 0.25 + 0.5,
                          0.0, 1.0)
            enc4 = np.concatenate([np.repeat(enc, 4, axis=0),
                                   np.ones((4 * n, 1), np.float32)], axis=1)
            mesh.color_attributes.new(f"SH{k}", 'FLOAT_COLOR',
                                      'POINT').data.foreach_set(
                "color", enc4.ravel())
        Minv3 = r.model_matrix().inverted().to_3x3()
        live_rinv = [tuple(Minv3[i]) for i in range(3)]

    if lit:
        # Scene-lit variant lives in its own module (lighting.py) so the
        # emission path stays untouched. Live SH makes no sense here: the
        # colour is an albedo, not a view-dependent radiance.
        from . import lighting
        mesh.materials.append(
            lighting.build_lit_material(nm, soft, color_img,
                                        atlas_uv=_ATLAS_UV,
                                        emission_mix=float(emission_mix),
                                        normal_mode=normal_mode,
                                        kernel=kernel,
                                        ambient=float(getattr(
                                            sc, "fgs_lit_preview_ambient",
                                            0.15)),
                                        gain=float(light_gain)))
    else:
        mesh.materials.append(
            _bake_material(nm, soft, sc, live_rinv, color_img,
                           kernel=kernel))

    obj = bpy.data.objects.new(nm, mesh)
    context.collection.objects.link(obj)
    if lit:
        from . import lighting
        stale = lighting.purge_stale_proxies(context)
        if stale:
            print(f"[SplatBake] removed {stale} stale shadow twin(s) left "
                  "behind by deleted bakes - they were darkening everything "
                  "baked in the same spot")
        lighting.prepare_object(obj)
        if cast_shadows:
            # The shadow rides on a camera-invisible twin, so the visible
            # model can never self-shadow - in either engine. (1.20.5: the
            # old Light Path thinning silently never ran in EEVEE and the
            # model went black; see lighting.add_shadow_proxy.)
            lighting.add_shadow_proxy(context, obj, soft,
                                      float(shadow_strength), kernel)
    else:
        try:
            # Emission splats are baked radiance: they must not cast shadows.
            # (Cycles honours this; it also undoes the opaque-shadow-blob bug.)
            obj.visible_shadow = False
        except Exception:
            pass
    for so in context.selected_objects:
        so.select_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj
    return obj, n, mode, (color_img is not None)


# ---------------------------------------------------------------------------
# EXPERIMENTAL - PARKED in 1.20.4, together with the solid-surface bake.
# "Set Up Lighting Test" existed to sanity-check lit SOLID bakes; it is
# hidden with them. The disc bake ("Bake Discs") does not use it. To bring
# it back: un-comment this class, its entry in `classes`, and its ui.py row.
# ---------------------------------------------------------------------------
# class FGS_OT_light_setup(Operator):
#     """One click that removes every variable between a lit bake and seeing it.
#
#     Across a lot of debugging, a lit bake that looked broken has turned out to
#     be one of: the render engine set to Cycles on a CPU (never converges), the
#     viewport left in Solid or Material Preview (materials not shown, or lit by
#     a studio HDRI that ignores your lamps), or simply no lamp in the scene.
#     Each is invisible from the render, and each looks exactly like a bug.
#
#     Rather than ask someone to check four things in three editors, set all of
#     them at once and report what changed.
#     """
#     bl_idname = "fgs.light_setup"
#     bl_label = "Set Up Lighting Test"
#     bl_description = ("Switch to EEVEE, add a sun if the scene has no lamp, "
#                       "and set the viewport to Rendered - so a lit bake can "
#                       "actually be seen")
#     bl_options = {'REGISTER', 'UNDO'}
#
#     def execute(self, context):
#         done = []
#         scn = context.scene
#         try:
#             eng = str(scn.render.engine).upper()
#             if 'EEVEE' not in eng:
#                 for name in ('BLENDER_EEVEE_NEXT', 'BLENDER_EEVEE'):
#                     try:
#                         scn.render.engine = name
#                         done.append("engine -> EEVEE")
#                         break
#                     except Exception:
#                         continue
#         except Exception:
#             pass
#
#         if not any(o.type == 'LIGHT' for o in scn.objects):
#             try:
#                 ld = bpy.data.lights.new("SplatBake Sun", 'SUN')
#                 ld.energy = 3.0
#                 sun = bpy.data.objects.new("SplatBake Sun", ld)
#                 scn.collection.objects.link(sun)
#                 sun.rotation_euler = (0.6, 0.0, 0.8)
#                 done.append("added a sun")
#             except Exception:
#                 pass
#
#         # Rendered shading, in every 3D viewport that is open - the setting is
#         # per-space, so changing only the active one leaves the others wrong.
#         try:
#             for area in context.screen.areas:
#                 if area.type == 'VIEW_3D':
#                     for sp in area.spaces:
#                         if sp.type == 'VIEW_3D':
#                             sp.shading.type = 'RENDERED'
#             done.append("viewport -> Rendered")
#         except Exception:
#             pass
#
#         self.report({'INFO'}, "Lighting test: " + (", ".join(done) or "already set"))
#         return {'FINISHED'}
#
#
class FGS_OT_bake(Operator):
    bl_idname = "fgs.bake_mesh"
    bl_label = "Bake to Mesh (render)"
    bl_options = {'REGISTER', 'UNDO'}
    bl_description = ("Build a real, renderable mesh of the active model so it "
                     "appears in F12 renders: one emission gaussian disc per "
                     "splat, kernel-matched to the viewport. Colour Detail "
                     "picks flat, camera-baked full SH, or live degree-1 SH")
    cap_pct: FloatProperty(
        name="Splats to Bake", default=100.0, min=0.1, max=100.0,
        subtype='PERCENTAGE',
        description="Share of THIS model's splats to bake, so the same "
                    "setting means the same fidelity on every model (each "
                    "splat becomes 2 triangles). Below 100%, splats are "
                    "kept by opacity importance, so solid structure "
                    "survives and haze goes first. Lower it if a huge "
                    "scene makes the bake or the render heavy")
    use_texture: BoolProperty(
        name="Colour as Texture (exact)", default=True,
        description="Bake each splat's colour into one texel of a 32-bit "
                    "float image and address it through its own UV layer "
                    "(nearest-neighbour, Non-Color). The colour reaching the "
                    "renderer is then exactly the one you see, and it "
                    "survives glTF/FBX export. Untick to use vertex colour "
                    "attributes instead")
    min_opacity: FloatProperty(
        name="Cull Below Opacity", default=0.0, min=0.0, max=0.9,
        description="Skip splats fainter than this. Near-invisible haze "
                    "costs a transparency bounce per ray while contributing "
                    "almost nothing - culling it is the biggest Cycles "
                    "speed-up on large scenes. 0 disables")
    sh_mode: bpy.props.EnumProperty(
        name="Colour Detail", default='CAMERA',
        description="How much of the view-dependent (SH) colour to keep",
        items=[
            ('BASE', "Base Colour",
             "Flat per-splat colour - fastest, least detail"),
            ('CAMERA', "Camera View (full SH)",
             "Evaluate the FULL spherical harmonics toward the scene camera "
             "and bake the result: the exact viewport colour for that camera "
             "position. Best for stills and camera-centred shots"),
            ('LIVE1', "Live (degree-1 SH)",
             "Evaluate degree-1 SH in the material itself, so colours shift "
             "with the render camera - most of the directional shading, "
             "correct from any angle. Uses more memory"),
        ])
    match_preview: BoolProperty(
        name="Match Preview Settings", default=True,
        description=(
            "Take the colour and shape settings from the panel - View Colour "
            "Quality (SH), Gaussian Mode and AA Compensation - so the bake "
            "reproduces what the viewport is showing instead of having its "
            "own separate settings. When lit, it also takes Preview "
            "Lighting, Light Gain and Preview Ambient. Untick to set the "
            "colour mode and kernel by hand below"))
    soft: BoolProperty(
        name="Soft Round Discs", default=True,
        description="Give each disc a soft radial gaussian alpha with the "
                    "splat's opacity baked in, instead of a hard opaque square. "
                    "Uses dithered transparency; untick for plain opaque discs "
                    "if transparency misbehaves on your GPU")
    boost: FloatProperty(
        name="Opacity Boost", default=1.0, min=0.5, max=3.0,
        description="1.0 = faithful to the splat data. Raise if the baked "
                    "model renders thinner / more see-through than the viewport")
    lit: BoolProperty(
        name="React to Scene Lights (Experimental)", default=True,
        description="Bake the colour as ALBEDO on a diffuse surface instead of "
                    "as emission, so the model is lit by the scene: black with "
                    "no lights, lit when a lamp shines on it, and it casts and "
                    "receives shadows. Off = self-lit, identical to the "
                    "viewport regardless of lighting")


    delight: FloatProperty(
        name="Remove Captured Lighting", default=0.75, min=0.0, max=1.0,
        subtype='FACTOR',
        description=(
            "How much of the lighting baked into the capture to divide out "
            "before the colours are used as albedo. Without this the original "
            "lighting is multiplied by the new lighting. Fits smooth degree-2 "
            "spherical harmonics to luminance against the splat normals and "
            "removes that trend; cast shadows and baked highlights remain. "
            "0 = keep the capture's lighting, 1 = remove the full fitted "
            "trend. Only used when React to Scene Lights is on"))
    emission_mix: FloatProperty(
        name="Keep Captured Colour", default=0.0, min=0.0, max=1.0,
        subtype='FACTOR',
        description=(
            "Blend some of the captured colour back in as emission, so the "
            "model stays visible even where no lamp reaches it. 0 = pure "
            "albedo (fully relit, and black wherever it is unlit), 1 = the "
            "original self-lit look with no lighting response. A small amount "
            "keeps the capture readable while lamps still shape it")) 
    light_gain: FloatProperty(
        name="Light Gain", default=3.0, min=0.1, max=10.0,
        description=(
            "How strongly the model converts incoming light into visible "
            "brightening. The captured palette forms a constant baseline, "
            "so at 1.0 (physically matched to a plain grey cube) a lamp "
            "must be close before it outshines that baseline. The default "
            "of 3 makes the model register lamps at roughly the distances "
            "a normal object does; raise it further for a more dramatic "
            "response"))
    cast_shadows: BoolProperty(
        name="Cast Shadows", default=True,
        description=(
            "Drop a shadow onto the rest of the scene. The shadow is carried "
            "by a camera-invisible twin object ('<name>_shadow'), so the "
            "visible model itself never enters a shadow map and can no "
            "longer shadow itself dark - in EEVEE or Cycles. Off skips the "
            "extra object"))
    normal_mode: bpy.props.EnumProperty(
        name="Splat Normals", default='CAMERA',
        items=[
            ('CAMERA', "Face Viewer",
             "Let the renderer turn each splat's normal toward the camera. "
             "Every visible splat receives light, so the model never goes "
             "black. This is Blender's native behaviour and matches the "
             "reference Cycles splat implementation"),
            ('TRUE', "True Orientation",
             "Shade with the orientation baked into the discs, so splats "
             "facing away from a lamp go dark and a lamp behind the model "
             "gives a real rim. More physically honest, but inside a fuzzy "
             "capture the splats facing away from you render black"),
        ],
        description="How splat normals are treated when lighting")
    shadow_strength: FloatProperty(
        name="Shadow Strength", default=1.0, min=0.0, max=1.0,
        subtype='FACTOR',
        description=(
            "Per-DISC density of the dropped shadow, applied to the shadow "
            "twin only. 1.0 = every disc fully solid to lamps: an honest, "
            "dense shadow (the twin is shrunk 4%, so the model's own "
            "lamp-facing shell stays lit either way). Lower it to let "
            "light bleed through the cloud for a softer, hazier shadow. "
            "Used when Cast Shadows is on"))
    def draw(self, context):
        """Explicit layout so de-lighting reads as what it is: a sub-option of
        scene lighting. Auto-generated dialogs would show it as a peer, which
        invites people to move a slider that does nothing on an emission bake.
        """
        lay = self.layout
        lay.use_property_split = True
        lay.use_property_decorate = False
        lay.prop(self, "match_preview")
        for name in ("cap_pct", "min_opacity", "use_texture", "boost"):
            lay.prop(self, name)
        # Greyed out rather than hidden when Match Preview is on: the values
        # it is taking from the panel stay visible, so the bake never looks
        # like it is ignoring settings that are simply being driven.
        man = lay.column()
        man.enabled = not self.match_preview
        man.prop(self, "sh_mode")
        man.prop(self, "soft")
        if self.match_preview:
            sc = context.scene
            q = (sc.fgs_sh_quality
                 if getattr(sc, "fgs_use_sh", True) else 'OFF')
            names = {'OFF': "base colour", 'DEG1': "low SH",
                     'DEG2': "medium SH", 'FULL': "full SH"}
            gm = {'SOFT': "soft kernel", 'V215': "defined kernel",
                  'PC': "web-exact kernel"}
            bits = [names.get(q, "full SH"),
                    gm.get(sc.fgs_pc_gaussian, "web-exact kernel")]
            if sc.fgs_antialias:
                bits.append("AA compensation")
            lay.label(text="From the panel: " + ", ".join(bits),
                      icon='CHECKMARK')
        lay.separator()
        box = lay.box()
        box.prop(self, "lit")
        if not self.lit:
            # The single most common confusion with this addon: bake with the
            # default, then wonder why lamps do nothing. Say so plainly, at
            # the moment the choice is made, rather than leaving it to the
            # tooltip.
            warn = box.column(align=True)
            warn.label(text="Self-lit bake: lamps will NOT affect it.",
                       icon='ERROR')
            warn.label(text="Tick the box above to relight the model.")
        sub = box.column()
        sub.enabled = self.lit
        sub.prop(self, "delight")
        sub.prop(self, "emission_mix")
        sub.prop(self, "light_gain")
        sub.prop(self, "normal_mode")
        sub.prop(self, "cast_shadows")
        shad = sub.column(align=True)
        shad.enabled = self.cast_shadows
        shad.prop(self, "shadow_strength")
        if self.lit:
            from . import lighting
            hint = lighting.scene_light_hint(context)
            if hint:
                box.label(text=hint, icon='INFO')
            eng = lighting.engine_hint(context)
            if eng:
                box.label(text=eng, icon='ERROR')

    def invoke(self, context, event):
        # Baking is heavy: let the user set the cap/softness first instead of
        # firing with defaults. (Everything stays adjustable in the redo
        # panel afterwards, since the operator registers UNDO.)
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        r = state.active_renderer(context)
        if r is None:
            self.report({'WARNING'}, "No active model - load or select one first")
            return {'CANCELLED'}
        # Camera position for the CAMERA colour mode: scene camera first,
        # else the current viewport eye.
        cam_world = None
        if context.scene.camera is not None:
            cam_world = context.scene.camera.matrix_world.translation.copy()
        else:
            for a in context.screen.areas:
                if a.type == 'VIEW_3D':
                    rv = a.spaces.active.region_3d
                    if rv is not None:
                        cam_world = rv.view_matrix.inverted().translation
                        break
        # -- Match Preview: the panel's settings ARE the bake's (1.20.12) ---
        sc = context.scene
        sh_mode = self.sh_mode
        soft = self.soft
        kernel = 'NORM'
        sh_k = None
        aa_view = None
        emission_mix = self.emission_mix
        light_gain = self.light_gain
        if self.match_preview:
            q = sc.fgs_sh_quality if getattr(sc, "fgs_use_sh", True) else 'OFF'
            sh_k = {'OFF': 0, 'DEG1': 3, 'DEG2': 8, 'FULL': 15}.get(q, 15)
            # 0 coefficients is the base colour, which is its own bake mode;
            # otherwise freeze the SH toward the camera at the same degree
            # the preview is drawing.
            sh_mode = 'BASE' if sh_k == 0 else 'CAMERA'
            gmode = getattr(sc, "fgs_pc_gaussian", 'PC')
            kernel = 'PLAIN' if gmode == 'SOFT' else 'NORM'
            soft = True                    # every preview mode is a gaussian
            if getattr(sc, "fgs_antialias", False):
                aa_view = _aa_camera(context, r)
            if self.lit:
                emission_mix = 1.0 - float(
                    getattr(sc, "fgs_lit_preview_mix", 1.0))
                light_gain = float(getattr(sc, "fgs_lit_preview_gain", 3.0))
        try:
            obj, n, mode, textured = _bake_cards(
                r, context, self.cap_pct, soft, self.boost,
                self.min_opacity,
                sh_mode, cam_world, self.use_texture, self.lit,
                self.delight, emission_mix, self.cast_shadows,
                self.normal_mode, self.shadow_strength,
                light_gain, sh_k, kernel, aa_view)
        except Exception as e:
            import traceback
            traceback.print_exc()          # full cause in the system console
            self.report({'ERROR'}, f"Bake failed: {e}")
            return {'CANCELLED'}
        # Thousands of stacked transparent discs need deep transparency in
        # Cycles, or rays terminate early and leave dark blotches.
        cyc = getattr(context.scene, "cycles", None)
        engine_is_cycles = 'CYCLES' in str(
            getattr(context.scene.render, "engine", "")).upper()
        if (engine_is_cycles and cyc is not None
                and getattr(cyc, "transparent_max_bounces", 0) < 256):
            try:
                cyc.transparent_max_bounces = 256
            except Exception:
                pass
        label = {'BASE': "base colour", 'CAMERA': "camera-view SH",
                 'LIVE1': "live degree-1 SH"}[mode]
        src = "texture" if textured else "vertex colour"
        note = "" if mode == self.sh_mode else " - no SH, used base"
        shade = ("scene-lit + shadow proxy"
                 if (self.lit and self.cast_shadows)
                 else "scene-lit" if self.lit else "self-lit")
        if self.lit:
            from . import lighting
            params = (f"splats={self.cap_pct:g}%, "
                      f"match_preview={self.match_preview}, "
                      f"sh={sh_mode}/{sh_k}, kernel={kernel}, "
                      f"aa={aa_view is not None}, "
                      f"emission_mix={emission_mix:g}, "
                      f"delight={self.delight:g}, "
                      f"normals={self.normal_mode}, "
                      f"cast={self.cast_shadows}, "
                      f"strength={self.shadow_strength:g}, "
                      f"gain={light_gain:g}, "
                      f"soft={self.soft}, boost={self.boost:g}")
            env, warns = lighting.describe_lit_environment(context, params)
            print("[SplatBake] lit environment: " + env)
            if emission_mix >= 0.5:
                warns.append(f"Keep Captured Colour={emission_mix:.2f}:"
                             " mostly self-lit, lamps will barely register")
            hint = lighting.scene_light_hint(context)
            if hint:
                warns.insert(0, hint)
            if warns:
                self.report({'WARNING'}, f"Baked '{obj.name}' ({n:,} discs, "
                                         f"{shade}) - " + "; ".join(warns))
                return {'FINISHED'}
        self.report({'INFO'},
                    f"Baked '{obj.name}' ({n:,} discs, {shade}, {label} via "
                    f"{src}{note}) - now renders in F12")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# EXPERIMENTAL - Bake Solid Surface, PARKED in 1.20.4.
# The volume reconstruction did not meet the bar on real captures, so the
# whole path is commented out rather than shipped half-working: the helpers
# (_surface_material, _surface_radii, _auto_detail, _snap_to_splats,
# _bake_surface) and the FGS_OT_bake_surface operator. uvtools.py (UV +
# texture) has no other caller and sits idle with it. NOTHING below is
# shared with the disc bake, which keeps its own live helpers above
# (_bake_cards, _bake_material, _disc_geometry, _grade_np,
# lighting.surface_normals). To resume: un-comment this block, the two
# names in `classes`, and the button rows in ui.py.
# ---------------------------------------------------------------------------
# def _surface_material(name, emissive):
#     """Material for the baked surface: vertex colours into a lit Principled
#     BSDF (default) or an unlit Emission shader."""
#     mat = bpy.data.materials.new(name + "_mat")
#     mat.use_nodes = True
#     nt = mat.node_tree
#     for nd in list(nt.nodes):
#         nt.nodes.remove(nd)
#     col = nt.nodes.new('ShaderNodeAttribute'); col.attribute_name = "Col"
#     col.location = (-500, 0)
#     out = nt.nodes.new('ShaderNodeOutputMaterial'); out.location = (240, 0)
#     if emissive:
#         sh = nt.nodes.new('ShaderNodeEmission'); sh.location = (-150, 0)
#         nt.links.new(col.outputs["Color"], sh.inputs["Color"])
#         nt.links.new(sh.outputs["Emission"], out.inputs["Surface"])
#     else:
#         sh = nt.nodes.new('ShaderNodeBsdfPrincipled'); sh.location = (-150, 0)
#         nt.links.new(col.outputs["Color"], sh.inputs["Base Color"])
#         try:
#             sh.inputs["Roughness"].default_value = 0.85
#         except Exception:
#             pass
#         nt.links.new(sh.outputs["BSDF"], out.inputs["Surface"])
#     return mat
#
#
# def _surface_radii(scale, voxel, radius_scale=1.0, despike=4.0):
#     """Per-splat blob radius for the volume reconstruction, in world units.
#
#     The old code used ONE radius for every splat, derived from the grid
#     resolution (1.6 * voxel). Two things went wrong with that:
#
#       * a tiny detail splat and a huge wall splat inflated to the same blob,
#         so thin structures fattened and broad surfaces went lumpy - the
#         surface could not follow the real contour because it no longer knew
#         what the real contour was;
#       * the radius was tied to Detail, so raising Detail SHRANK every blob
#         and the surface broke up. Detail should change resolution, not shape.
#
#     Now each splat contributes a blob sized by its own gaussian: the mean of
#     its two largest axes (the disc the splat actually draws - the smallest
#     axis is its thickness, which would under-size it), de-spiked so needle
#     splats can't balloon, and floored at ~1.1 voxels so every blob is big
#     enough to register on the grid and connect to its neighbours.
#
#     voxel=0 means "no grid yet - give me the raw sizes" (auto detail and the
#     ratio report ask for them). That call used to fall through to
#     np.clip(rad, 0, 0), which zeroed every radius: _auto_detail then saw a
#     zero median and quietly fell back to detail=200 whatever the scene, and
#     the voxel:splat ratio divided by (near) zero. One clip made
#     "Match Splat Size" a constant. (fixed in 1.20.2)
#     """
#     s = np.sort(np.maximum(scale.astype(np.float32), 1e-9), axis=1)
#     if despike > 0.0:
#         med = np.median(s, axis=1)
#         s = np.minimum(s, (float(despike) * med)[:, None])
#     rad = 0.5 * (s[:, 2] + s[:, 1]) * float(radius_scale)
#     if voxel > 0.0:
#         lo = 1.1 * float(voxel)              # must span a voxel to register
#         hi = 24.0 * float(voxel)             # stop one fat splat blobbing all
#         rad = np.clip(rad, lo, hi)
#     return rad.astype(np.float32)
#
#
# def _auto_detail(span, radii, target=2.0, lo=32, hi=8000):
#     """Grid resolution that actually resolves the splats.
#
#     "Detail" divides the scene's longest axis, so the right value depends
#     entirely on how big the splats are relative to the scene - a fixed number
#     is meaningless. Measured on a real 1.9M-splat scan: the scene spans 363
#     units but a typical splat radius is 0.044, so even detail=400 gives a
#     voxel 21x larger than a splat. At that size every blob collapses to a
#     single voxel and the surface CANNOT follow the contour, however the
#     radius is computed. That was the real accuracy ceiling.
#
#     Target ~2 voxels per splat radius. OpenVDB is sparse, so the cost tracks
#     occupied voxels (points x blob volume), not the full grid - which is why
#     four-figure detail is affordable.
#
#     (1.20.2: the original formula divided span by target*radius, so the
#     voxel came out target TIMES the radius - 4x coarser than documented -
#     and the 1.1-voxel floor then re-inflated most splats to match. Multiply,
#     don't divide. The hi clamp now binds on large scenes; the reported
#     voxel:splat ratio shows by how much.)
#     """
#     med = float(np.median(radii))
#     if med <= 0.0:
#         return int(np.clip(200, lo, hi))
#     return int(np.clip(round(float(target) * span / med), lo, hi))
#
#
# def _snap_to_splats(v, kd, pts, nrm, sig, op, k=12, iters=3):
#     """Pull each isosurface vertex onto the plane of its nearby splats.
#
#     WHY
#     ---
#     Points->Volume sums UNSIGNED blobs, so the extracted level set can only
#     sit somewhere out in the summed falloff. Where exactly depends on the
#     threshold AND on how many splats overlap locally: dense areas bulge
#     outward, sparse rims pull tight. No threshold is right everywhere, so
#     the shell floats off the splats by a varying amount.
#
#     THE FIX
#     -------
#     Implicit moving least squares: for the k nearest splats, take the
#     opacity- and distance-weighted mean of each splat's signed plane
#     distance n.(v - p), and walk the vertex back by it along the mean
#     normal. The zero set of that weighted mean passes through the splats
#     by construction - it is their weighted median plane - so three capped
#     steps land every vertex on the surface the splats describe, while the
#     volume mesh keeps supplying topology and closed holes.
#
#     GUARDS - the pass may only tighten, never destroy:
#       * no splat support (the volume bridged a real gap): vertex stays put;
#       * neighbours whose normal opposes the local mean are dropped, so a
#         thin wall's far side never averages against its near side
#         (verified on a synthetic two-sided wall: snaps to the near face,
#         not the midpoint);
#       * each step is capped at the local median splat radius, so one bad
#         normal cannot fling a vertex.
#
#     Returns (moved_count, mean_shift, max_shift) in world units - an
#     alignment claim should ship with its measurement.
#     """
#     n_moved, tot, mx = 0, 0.0, 0.0
#     kk = int(min(max(k, 1), len(pts)))
#     for vi in range(len(v)):
#         x0, y0, z0 = float(v[vi, 0]), float(v[vi, 1]), float(v[vi, 2])
#         loc = (x0, y0, z0)
#         for _ in range(int(iters)):
#             hits = kd.find_n(loc, kk)
#             if not hits:
#                 break
#             ids = np.fromiter((h[1] for h in hits), np.intp, len(hits))
#             d = np.fromiter((h[2] for h in hits), np.float32, len(hits))
#             s = sig[ids]
#             w = op[ids] * np.exp(-0.5 * np.square(d / s))
#             w[d > 3.0 * s] = 0.0
#             if float(w.sum()) < 1e-6:            # bridged gap - leave it be
#                 break
#             n = nrm[ids]
#             mean_n = (w[:, None] * n).sum(axis=0)
#             ln = float(np.linalg.norm(mean_n))
#             if ln < 1e-8:
#                 break
#             mean_n /= ln
#             keep = (n @ mean_n) > 0.0            # drop the wall's far side
#             w *= keep
#             ws = float(w.sum())
#             if ws < 1e-6:
#                 break
#             q = np.array(loc, np.float32) - pts[ids]
#             f = float((w * np.einsum('ij,ij->i', n, q)).sum() / ws)
#             cap_step = float(np.median(s[keep]))
#             if f > cap_step:
#                 f = cap_step
#             elif f < -cap_step:
#                 f = -cap_step
#             loc = (loc[0] - f * float(mean_n[0]),
#                    loc[1] - f * float(mean_n[1]),
#                    loc[2] - f * float(mean_n[2]))
#         dd = ((loc[0] - x0) ** 2 + (loc[1] - y0) ** 2
#               + (loc[2] - z0) ** 2) ** 0.5
#         if dd > 0.0:
#             v[vi, 0], v[vi, 1], v[vi, 2] = loc
#             n_moved += 1
#             tot += dd
#             mx = max(mx, dd)
#     return n_moved, (tot / n_moved if n_moved else 0.0), mx
#
#
# def _bake_surface(r, context, detail, min_opacity, cap, emissive, smooth,
#                   radius_scale=1.0, threshold=0.12, auto_detail=False,
#                   snap=True):
#     """Reconstruct ONE solid mesh from the splat cloud: a density volume is
#     built on a detail^3-style grid (OpenVDB Points->Volume) and its isosurface
#     extracted (Volume->Mesh). Vertex colours come from the nearest splat."""
#     sc = context.scene
#     src = r.source
#     idx = np.where(r.alive)[0]
#     op_full = src.get("opacity")
#     if op_full is not None:
#         keep = op_full[idx].astype(np.float32) >= float(min_opacity)
#         idx = idx[keep]
#     if len(idx) < 16:
#         raise RuntimeError("too few solid splats - lower Min Opacity")
#     if len(idx) > cap:
#         # Opacity-weighted, like the disc bake: a uniform draw discards solid
#         # surface splats at the same rate as haze, which pits the
#         # reconstruction. Efraimidis-Spirakis keys give weighted sampling
#         # without replacement in one pass.
#         rng = np.random.default_rng(0)
#         u = rng.random(len(idx))
#         if op_full is not None:
#             w = np.clip(op_full[idx].astype(np.float64), 1e-4, None)
#             keys = np.log(np.maximum(u, 1e-12)) / w
#         else:
#             keys = u
#         idx = np.sort(idx[np.argpartition(keys, -int(cap))[-int(cap):]])
#
#     M = np.array(r.model_matrix(), dtype=np.float32)
#     pts = src["xyz"][idx].astype(np.float32) @ M[:3, :3].T + M[:3, 3]
#
#     span = float((pts.max(axis=0) - pts.min(axis=0)).max())
#     if span <= 0.0:
#         raise RuntimeError("degenerate point cloud")
#
#     # Uniform world scale of the model, so a scaled-up model gets bigger blobs.
#     msc = float(np.cbrt(abs(np.linalg.det(M[:3, :3]))))
#     if not np.isfinite(msc) or msc <= 0.0:
#         msc = 1.0
#     wscale = src["scale"][idx] * msc
#     aniso = float(sc.fgs_despike) or 4.0
#
#     if auto_detail:
#         # Size the grid from the splats themselves (voxel=0 here: the floor
#         # clamp is what we are trying to choose, so ask for the raw sizes).
#         detail = _auto_detail(span, _surface_radii(wscale, 0.0, radius_scale,
#                                                    aniso))
#     voxel = span / float(detail)          # "levels" along the largest axis
#     radii = _surface_radii(wscale, voxel, radius_scale, aniso)
#     ratio = voxel / max(float(np.median(_surface_radii(wscale, 0.0,
#                                                        radius_scale, aniso))),
#                         1e-9)
#
#     nm_src = bpy.data.objects.get(r.box_name)
#     nm = (nm_src.name if nm_src else "Splat") + "_surface"
#
#     mesh = bpy.data.meshes.new(nm)
#     mesh.vertices.add(len(pts))
#     mesh.vertices.foreach_set("co", np.ascontiguousarray(pts, np.float32).ravel())
#     # The per-point radius travels into geometry nodes as a named attribute.
#     have_radii = False
#     try:
#         mesh.attributes.new("splat_radius", 'FLOAT', 'POINT').data.foreach_set(
#             "value", radii)
#         have_radii = True
#     except Exception as e:
#         print("[SplatBake] per-splat radius attribute failed:", e)
#     mesh.update()
#     obj = bpy.data.objects.new(nm, mesh)
#     context.collection.objects.link(obj)
#
#     ng = None
#     try:
#         ng = bpy.data.node_groups.new(nm + "_gn", 'GeometryNodeTree')
#         ng.interface.new_socket("Geometry", in_out='INPUT',
#                                 socket_type='NodeSocketGeometry')
#         ng.interface.new_socket("Geometry", in_out='OUTPUT',
#                                 socket_type='NodeSocketGeometry')
#         n_in = ng.nodes.new('NodeGroupInput')
#         n_out = ng.nodes.new('NodeGroupOutput')
#         m2p = ng.nodes.new('GeometryNodeMeshToPoints')
#         try:
#             m2p.mode = 'VERTICES'
#         except Exception:
#             pass
#         p2v = ng.nodes.new('GeometryNodePointsToVolume')
#         try:
#             p2v.resolution_mode = 'VOXEL_SIZE'
#         except Exception:
#             pass
#         p2v.inputs["Voxel Size"].default_value = voxel
#         # Fallback constant if the attribute route is unavailable.
#         p2v.inputs["Radius"].default_value = float(np.median(radii))
#         if have_radii:
#             try:
#                 nattr = ng.nodes.new('GeometryNodeInputNamedAttribute')
#                 nattr.data_type = 'FLOAT'
#                 nattr.inputs["Name"].default_value = "splat_radius"
#                 ng.links.new(nattr.outputs["Attribute"],
#                              p2v.inputs["Radius"])
#             except Exception as e:
#                 print("[SplatBake] per-splat radius link failed, "
#                       "using a single radius:", e)
#         try:
#             p2v.inputs["Density"].default_value = 1.0
#         except Exception:
#             pass
#         v2m = ng.nodes.new('GeometryNodeVolumeToMesh')
#         try:
#             v2m.resolution_mode = 'GRID'
#         except Exception:
#             pass
#         try:
#             # Threshold is the isosurface level: low = the surface sits far
#             # out in each splat's falloff (puffy, closes holes), high = it
#             # hugs the dense core (tight, but can open gaps).
#             v2m.inputs["Threshold"].default_value = float(threshold)
#             v2m.inputs["Adaptivity"].default_value = 0.0
#         except Exception:
#             pass
#         ng.links.new(n_in.outputs[0], m2p.inputs["Mesh"])
#         ng.links.new(m2p.outputs["Points"], p2v.inputs["Points"])
#         ng.links.new(p2v.outputs["Volume"], v2m.inputs["Volume"])
#         ng.links.new(v2m.outputs["Mesh"], n_out.inputs[0])
#
#         mod = obj.modifiers.new("SplatSurface", 'NODES')
#         mod.node_group = ng
#
#         context.view_layer.update()
#         deps = context.evaluated_depsgraph_get()
#         new_mesh = bpy.data.meshes.new_from_object(obj.evaluated_get(deps))
#         if len(new_mesh.vertices) == 0:
#             bpy.data.meshes.remove(new_mesh)
#             raise RuntimeError(
#                 "surface came out empty - raise Detail, lower Min Opacity, "
#                 "or this Blender build lacks OpenVDB volume nodes")
#         obj.modifiers.clear()
#         old = obj.data
#         obj.data = new_mesh
#         bpy.data.meshes.remove(old)
#     finally:
#         if ng is not None:
#             try:
#                 bpy.data.node_groups.remove(ng)
#             except Exception:
#                 pass
#
#     # colour each surface vertex from its nearest splats (KD-tree, C-speed);
#     # averaging the 3 nearest smooths single-splat speckle off the surface
#     from mathutils import kdtree
#     kd = kdtree.KDTree(len(pts))
#     for i, pnt in enumerate(pts):
#         kd.insert(pnt.tolist(), i)
#     kd.balance()
#
#     # -- snap the shell onto the splats, BEFORE colours are sampled --------
#     snap_note = ""
#     if snap:
#         import time as _time
#         _ts = _time.perf_counter()
#         try:
#             from . import lighting as _ln
#             s_nrm = _ln.surface_normals(
#                 src["xyz"][idx].astype(np.float32),
#                 src["quat"][idx].astype(np.float32),
#                 src["scale"][idx].astype(np.float32))
#             try:
#                 # Normals transform by the inverse-transpose; these are row
#                 # vectors, so multiply by the plain inverse.
#                 s_nrm = s_nrm @ np.linalg.inv(M[:3, :3])
#             except np.linalg.LinAlgError:
#                 print("[SplatBake] singular model matrix - snapping with "
#                       "model-space normals")
#             s_nrm /= np.maximum(
#                 np.linalg.norm(s_nrm, axis=1, keepdims=True), 1e-8)
#             # Raw per-splat disc radii (voxel=0: no grid clamp) as the
#             # weight bandwidth - a pancake pulls from its own footprint.
#             sig = np.maximum(
#                 _surface_radii(wscale, 0.0, radius_scale, aniso), 1e-6)
#             op_w = (np.clip(op_full[idx].astype(np.float32), 0.05, 1.0)
#                     if op_full is not None
#                     else np.ones(len(idx), np.float32))
#             vco = np.empty(len(new_mesh.vertices) * 3, np.float32)
#             new_mesh.vertices.foreach_get("co", vco)
#             vco = vco.reshape(-1, 3)
#             n_mv, avg_d, max_d = _snap_to_splats(vco, kd, pts, s_nrm,
#                                                  sig, op_w)
#             new_mesh.vertices.foreach_set("co", vco.ravel())
#             new_mesh.update()
#             snap_note = (f", snapped {n_mv:,}/{len(vco):,} verts "
#                          f"(avg {avg_d:.4g}, max {max_d:.4g})")
#             print(f"[SplatBake] snap-to-splats:{snap_note[1:]} in "
#                   f"{_time.perf_counter() - _ts:.1f}s")
#         except Exception as e:
#             import traceback
#             traceback.print_exc()
#             print("[SplatBake] snap-to-splats failed, mesh left as "
#                   "extracted:", e)
#             snap_note = ", snap FAILED (see console)"
#
#     graded = _grade_np(src["rgb"][idx], sc)
#     vcount = len(new_mesh.vertices)
#     cols = np.ones((vcount, 4), np.float32)
#     for vi, v in enumerate(new_mesh.vertices):
#         hits = kd.find_n(v.co, 3)
#         if hits:
#             cols[vi, :3] = np.mean([graded[h[1]] for h in hits], axis=0)
#     new_mesh.color_attributes.new("Col", 'FLOAT_COLOR', 'POINT').data.foreach_set(
#         "color", cols.ravel())
#
#     if smooth:
#         new_mesh.polygons.foreach_set(
#             "use_smooth", np.ones(len(new_mesh.polygons), dtype=bool))
#         new_mesh.update()
#
#     new_mesh.materials.append(_surface_material(nm, emissive))
#
#     for so in context.selected_objects:
#         so.select_set(False)
#     obj.select_set(True)
#     context.view_layer.objects.active = obj
#     info = f"detail {int(detail)}, voxel {voxel:.4g} ({ratio:.1f}x a splat)"
#     if ratio > 4.0:
#         info += " - raise Detail to follow the contour more closely"
#     info += snap_note
#     return obj, vcount, len(new_mesh.polygons), info
#
#
# class FGS_OT_bake_surface(Operator):
#     bl_idname = "fgs.bake_surface"
#     bl_label = "Bake Surface Mesh (solid)"
#     bl_options = {'REGISTER', 'UNDO'}
#     bl_description = ("Reconstruct ONE solid mesh from the active model's point "
#                      "cloud: each splat contributes a blob sized by its own "
#                      "gaussian, the density is sampled on a voxel grid and the "
#                      "isosurface extracted, then UV-unwrapped and textured. "
#                      "Lit like a normal object; good for sculpt/retopo, F12 "
#                      "renders, and OBJ/glTF/STL export")
#     auto_detail: BoolProperty(
#         name="Match Splat Size (auto detail)", default=True,
#         description="Choose the grid resolution from the splats themselves, "
#                     "aiming for about two voxels per splat. The right value "
#                     "depends on how big the splats are relative to the scene, "
#                     "so a fixed number rarely follows the contour. Untick to "
#                     "set Detail by hand")
#     detail: IntProperty(
#         name="Detail (levels per axis)", default=100, min=10, max=8000,
#         description="Grid resolution along the model's largest axis, used "
#                     "when auto detail is off. If this is much coarser than "
#                     "the splats, every blob collapses to one voxel and the "
#                     "surface cannot follow the contour - the status bar "
#                     "reports the voxel size against the splat size")
#     min_opacity: FloatProperty(
#         name="Min Opacity", default=0.3, min=0.0, max=1.0,
#         description="Ignore splats fainter than this, so haze and floaters "
#                     "don't inflate the surface")
#     cap: IntProperty(
#         name="Max Points", default=300000, min=1000, max=1000000,
#         description="Upper limit of splat centres fed into the reconstruction. "
#                     "Splats over the limit are kept by opacity importance, so "
#                     "solid structure survives and haze goes first")
#     radius_scale: FloatProperty(
#         name="Blob Size", default=1.0, min=0.25, max=4.0,
#         description="Multiplies each splat's own radius. 1.0 follows the "
#                     "gaussian sizes as captured. Raise it to close holes in "
#                     "a sparse scan, lower it for a tighter, leaner surface "
#                     "that follows fine detail more closely")
#     threshold: FloatProperty(
#         name="Surface Tightness", default=0.12, min=0.01, max=0.9,
#         description="Isosurface level. LOW sits far out in each splat's "
#                     "falloff - puffier, but closes gaps. HIGH hugs the dense "
#                     "core - tighter to the real contour, but can open holes "
#                     "in thin or sparse areas")
#     snap: BoolProperty(
#         name="Snap to Splats", default=True,
#         description="After the isosurface is extracted, pull every vertex "
#                     "onto the weighted plane of its nearest splats. A "
#                     "density isosurface can only sit somewhere out in the "
#                     "summed falloff, inflated by an amount that varies with "
#                     "splat overlap; this projection removes that bias so "
#                     "the shell sits ON the splats. Untick to compare "
#                     "against the raw volume mesh")
#     emissive: BoolProperty(
#         name="Emissive (unlit)", default=False,
#         description="Use an unlit emission material instead of a lit "
#                     "Principled surface")
#     smooth: BoolProperty(
#         name="Shade Smooth", default=True,
#         description="Smooth-shade the reconstructed surface")
#     do_uv: BoolProperty(
#         name="Generate UV Map", default=True,
#         description="Unwrap the reconstructed surface (Smart UV Project) so "
#                     "it can carry a texture, be painted on, or be exported "
#                     "with its colours intact")
#     do_texture: BoolProperty(
#         name="Bake Colours to Texture", default=True,
#         description="Rasterise the splat colours into an image through the "
#                     "new UV layout and use it as the surface texture. Colour "
#                     "detail is then set by the texture, not by how dense the "
#                     "mesh is - and unlike vertex colours it survives export "
#                     "to glTF / FBX / OBJ")
#     tex_size: bpy.props.EnumProperty(
#         name="Texture Size", default='2048',
#         description="Resolution of the baked texture",
#         items=[('1024', "1024 x 1024", "Fast, for small or distant models"),
#                ('2048', "2048 x 2048", "Good default"),
#                ('4096', "4096 x 4096", "Sharp; slower to bake and heavier")])
#
#     def invoke(self, context, event):
#         return context.window_manager.invoke_props_dialog(self)
#
#     def execute(self, context):
#         r = state.active_renderer(context)
#         if r is None:
#             self.report({'WARNING'}, "No active model - load or select one first")
#             return {'CANCELLED'}
#         try:
#             obj, nv, nf, info = _bake_surface(
#                 r, context, self.detail, self.min_opacity, self.cap,
#                 self.emissive, self.smooth, self.radius_scale,
#                 self.threshold, self.auto_detail, self.snap)
#         except Exception as e:
#             import traceback
#             traceback.print_exc()
#             self.report({'ERROR'}, f"Surface bake failed: {e}")
#             return {'CANCELLED'}
#
#         # UV + texture live in uvtools.py; a failure there must not throw away
#         # the surface that was just reconstructed, so it degrades to the
#         # vertex-coloured mesh instead of cancelling.
#         extra = ""
#         if self.do_uv:
#             try:
#                 from . import uvtools
#                 how = uvtools.smart_unwrap(obj)
#                 extra = f", UV ({how})"
#                 if self.do_texture:
#                     img, cov = uvtools.bake_texture(obj, int(self.tex_size))
#                     uvtools.apply_texture(obj, img, self.emissive)
#                     extra += f" + {self.tex_size}px texture ({cov * 100:.0f}% used)"
#             except Exception as e:
#                 import traceback
#                 traceback.print_exc()
#                 self.report({'WARNING'},
#                             f"Surface built, but UV/texture step failed: {e}")
#                 return {'FINISHED'}
#         self.report({'INFO'},
#                     f"Built '{obj.name}': {nv:,} verts / {nf:,} faces - "
#                     f"{info}{extra}")
#         return {'FINISHED'}
#
#
#
class FGS_OT_click_select(Operator):
    bl_idname = "fgs.click_select"
    bl_label = "Splat Click Select"
    bl_description = ("Select the splat model under the cursor. Selection only "
                     "- move it afterwards with Blender's own tools (G / R / "
                     "S). Clicks that miss pass through to Blender's normal "
                     "selection, box-select and gizmos untouched")

    def invoke(self, context, event):
        # Bound to plain LMB in Object Mode (see _register_keymaps), so bail
        # out transparently whenever this click is not on a splat model:
        # PASS_THROUGH hands the event straight back to Blender's own
        # select / box-select / cursor bindings.
        if context.mode != 'OBJECT' or not state.RENDERERS:
            return {'PASS_THROUGH'}
        region = context.region
        rv3d = context.region_data
        if region is None or rv3d is None:
            region, rv3d = _area_under(context, event)
        self._hit = False
        if region is not None and rv3d is not None:
            mx = event.mouse_x - region.x
            my = event.mouse_y - region.y
            r, t_splat = state.pick_renderer_under_cursor_ray(
                region, rv3d, mx, my)
            # (1.20.12) An ordinary Blender object IN FRONT of the splats wins.
            # This operator owns plain LMB, and it used to claim any click
            # that landed on a splat regardless of what stood between the
            # cursor and it - so a cube parked in front of a capture could not
            # be clicked at all. Blender's own ray_cast settles the depth
            # question; if it hits something nearer, hand the click back.
            if r is not None and t_splat is not None:
                try:
                    from bpy_extras import view3d_utils
                    org = view3d_utils.region_2d_to_origin_3d(
                        region, rv3d, (mx, my))
                    dr = view3d_utils.region_2d_to_vector_3d(
                        region, rv3d, (mx, my))
                    dg = context.evaluated_depsgraph_get()
                    ok, loc, _n, _i, ob, _m = context.scene.ray_cast(
                        dg, org, dr)
                    if ok and ob is not None:
                        handles = {rr.box_name for rr in state.RENDERERS}
                        if ob.name not in handles and \
                                float((loc - org).dot(dr)) < t_splat:
                            r = None
                except Exception as e:
                    print("[SplatBake] occlusion test skipped:", e)
            if r is not None:
                box = bpy.data.objects.get(r.box_name)
                if box is not None:
                    for o in context.selected_objects:
                        o.select_set(False)
                    box.select_set(True)
                    context.view_layer.objects.active = box
                    state.set_active(r)
                    state.tag_redraw(context)
                    self._hit = True
        if not self._hit:
            return {'PASS_THROUGH'}
        # Selection only. No modal handler, so the click never turns into a
        # drag-move: transforms happen through Blender's own G / R / S (or the
        # gizmos), exactly as they do for any other object.
        return {'FINISHED'}


class FGS_OT_snapshot(Operator):
    bl_idname = "fgs.snapshot_render"
    bl_label = "Snapshot Viewport (same as splats)"
    bl_description = ("Save an image of the viewport - INCLUDING the live splats - "
                     "at the scene render resolution. This is the 1:1 way to get "
                     "an image that matches the splat look exactly")
    use_camera: BoolProperty(
        name="Through Scene Camera", default=True,
        description="Frame through the scene camera if one exists (untick to "
                    "capture the current view angle instead)")
    animation: BoolProperty(
        name="Animation", default=False,
        description="Render the whole frame range to the output path instead "
                    "of a single image")

    def execute(self, context):
        area = context.area if (context.area is not None
                                and context.area.type == 'VIEW_3D') else None
        if area is None:
            for a in context.screen.areas:
                if a.type == 'VIEW_3D':
                    area = a
                    break
        if area is None:
            self.report({'ERROR'}, "No 3D viewport found")
            return {'CANCELLED'}
        region = next((rg for rg in area.regions if rg.type == 'WINDOW'), None)
        rv3d = area.spaces.active.region_3d
        prev = rv3d.view_perspective
        switched = False
        if self.use_camera and context.scene.camera is not None \
                and prev != 'CAMERA':
            rv3d.view_perspective = 'CAMERA'
            switched = True
        try:
            with context.temp_override(area=area, region=region):
                bpy.ops.render.opengl(animation=self.animation,
                                      view_context=True)
        except Exception as e:
            if switched:
                rv3d.view_perspective = prev
            self.report({'ERROR'}, f"Viewport render failed: {e}")
            return {'CANCELLED'}
        if switched:
            rv3d.view_perspective = prev
        if not self.animation:
            self.report({'INFO'},
                        "Done - image is in the Render Result (Image > Save As)")
        else:
            self.report({'INFO'},
                        f"Frames written to: {context.scene.render.filepath}")
        return {'FINISHED'}


class FGS_TOOL_splat(bpy.types.WorkSpaceTool):
    bl_space_type = 'VIEW_3D'
    bl_context_mode = 'OBJECT'
    bl_idname = "fgs.splat_tool"
    bl_label = "Move Splats"
    bl_description = ("Click a splat model to select it, drag to move it. "
                     "G / R / S with X / Y / Z work as normal afterwards")
    bl_icon = "ops.transform.translate"
    bl_keymap = (
        ("fgs.click_select", {"type": 'LEFTMOUSE', "value": 'PRESS'}, None),
        ("fgs.pick_orbit", {"type": 'LEFTMOUSE', "value": 'DOUBLE_CLICK'}, None),
        ("fgs.dolly_cursor", {"type": 'WHEELUPMOUSE', "value": 'PRESS'},
         {"properties": [("delta", -1.0)]}),
        ("fgs.dolly_cursor", {"type": 'WHEELDOWNMOUSE', "value": 'PRESS'},
         {"properties": [("delta", 1.0)]}),
        ("fgs.frame_scene", {"type": 'F', "value": 'PRESS'}, None),
    )



def _view_pivot_and_dist(rv3d):
    """rv3d.view_location is the orbit pivot; view_distance is dolly range."""
    return rv3d.view_location.copy(), rv3d.view_distance


class FGS_OT_dolly_cursor(Operator):
    bl_idname = "fgs.dolly_cursor"
    bl_label = "Dolly To Cursor"
    bl_description = "Zoom toward the point under the cursor (splat tool)"
    delta: bpy.props.FloatProperty(default=0.0)

    def invoke(self, context, event):
        rv3d = context.region_data
        region = context.region
        if rv3d is None or region is None:
            return {'CANCELLED'}
        # same exponential factor as the web viewer's wheel handler
        notch = -1.0 if self.delta == 0.0 else self.delta
        f = math.exp(notch * (0.16 if event.ctrl else 0.16))
        mx = event.mouse_x - region.x
        my = event.mouse_y - region.y
        if f < 1.0:
            p = state.pick_splat_point(region, rv3d, mx, my)
            if p is None:
                from bpy_extras import view3d_utils
                o = view3d_utils.region_2d_to_origin_3d(region, rv3d, (mx, my))
                d = view3d_utils.region_2d_to_vector_3d(region, rv3d, (mx, my))
                p = o + d * rv3d.view_distance
            # slide pivot toward the target by (1-f), exactly like the web app
            rv3d.view_location = rv3d.view_location.lerp(p, 1.0 - f)
        rv3d.view_distance *= f
        context.area.tag_redraw()
        return {'FINISHED'}


class FGS_OT_frame(Operator):
    bl_idname = "fgs.frame_scene"
    bl_label = "Frame Splats"
    bl_description = ("Frame the models on their trimmed 2%-98% bounds, "
                     "ignoring sky/floater splats (splat tool: F)")

    def execute(self, context):
        rv3d = context.region_data
        tb = state._tight_world_bounds()
        if rv3d is None or tb is None:
            return {'CANCELLED'}
        mn, mx = tb
        rv3d.view_location = (mn + mx) * 0.5
        rv3d.view_distance = max((mx - mn).length * 0.85, 0.05)
        try:
            sp = context.space_data
            raw_diag = state.raw_world_diag()
            if sp is not None and raw_diag > 0 and sp.clip_end < raw_diag * 2.0:
                sp.clip_end = min(raw_diag * 2.0, 200000.0)
        except Exception:
            pass
        if context.area:
            context.area.tag_redraw()
        return {'FINISHED'}


class FGS_OT_pick_orbit(Operator):
    bl_idname = "fgs.pick_orbit"
    bl_label = "Orbit Here"
    bl_description = ("Double-click a splat to re-anchor the orbit there, "
                     "camera keeps its position (splat tool)")

    def invoke(self, context, event):
        rv3d = context.region_data
        region = context.region
        if rv3d is None or region is None:
            return {'CANCELLED'}
        mx = event.mouse_x - region.x
        my = event.mouse_y - region.y
        p = state.pick_splat_point(region, rv3d, mx, my)
        if p is None:
            from bpy_extras import view3d_utils
            o = view3d_utils.region_2d_to_origin_3d(region, rv3d, (mx, my))
            d = view3d_utils.region_2d_to_vector_3d(region, rv3d, (mx, my))
            p = o + d * rv3d.view_distance
        # keep the eye fixed: new view_distance = |eye - p|, pivot = p
        eye = rv3d.view_matrix.inverted().translation
        rv3d.view_location = p
        rv3d.view_distance = max((eye - p).length, 0.01)
        context.area.tag_redraw()
        return {'FINISHED'}



class FGS_OT_selftest(Operator):
    bl_idname = "fgs.selftest"
    bl_label = "Run Colour Self-Test"
    bl_description = ("Render known colours through the splat shader's colour "
                     "path and report expected vs actual pixel values, to "
                     "verify the pipeline on this GPU")

    def execute(self, context):
        from . import selftest
        try:
            rows, worst_disp, worst_lin = selftest.run()
        except Exception as e:
            self.report({'ERROR'}, "Self-test could not run on this GPU: " + str(e))
            print("[SplatBake] self-test error:", e)
            return {'CANCELLED'}
        print("\n=== SplatBake colour self-test ===")
        print(">>> RUNNING ADD-ON VERSION", state.VERSION, "<<<")
        print("colour(sRGB)      shown->expect            linear->expect")
        ok = True
        for (col, shown, ed, lin, el) in rows:
            flag = "" if (max(abs(a-b) for a, b in zip(shown, ed)) < 0.02) else "  <-- MISMATCH"
            if flag:
                ok = False
            print(f"{col}  {shown} -> {ed}   {lin} -> {el}{flag}")
        print(f"worst display error {worst_disp:.4f} | worst linear error {worst_lin:.4f}")
        print("=========================================\n")
        verdict = ("PASS - colour maths correct on this GPU"
                   if (worst_disp < 0.02 and worst_lin < 0.02)
                   else "FAIL - see System Console for the mismatch table")
        # store a short summary for the panel
        context.window_manager.fgs_selftest_msg = (
            f"v{state.VERSION}: {verdict} (err {max(worst_disp, worst_lin):.3f})")
        level = 'INFO' if ok else 'WARNING'
        self.report({level}, verdict + " - full table in System Console")
        return {'FINISHED'}


class FGS_OT_test_splat(Operator):
    bl_idname = "fgs.test_splat"
    bl_label = "Load Colour Test Card"
    bl_description = ("Load a synthetic splat card of known colours (no file "
                     "needed) so you can visually confirm reds/greens/greys "
                     "look correct, not washed out")

    def execute(self, context):
        import numpy as np
        cols = np.array([
            [0.9, 0.1, 0.1], [0.1, 0.8, 0.1], [0.15, 0.35, 0.9],
            [0.9, 0.9, 0.1], [0.9, 0.1, 0.9], [0.1, 0.9, 0.9],
            [0.05, 0.05, 0.05], [0.5, 0.5, 0.5], [0.95, 0.95, 0.95],
        ], dtype=np.float32)
        n = len(cols)
        # spaced 3x3 grid in the X/Z plane so nothing overlaps into a wash
        gap = 2.0
        xyz = np.zeros((n, 3), np.float32)
        for i in range(n):
            xyz[i, 0] = ((i % 3) - 1) * gap
            xyz[i, 2] = ((i // 3) - 1) * gap
        data = {
            "xyz": xyz,
            "rgb": cols,
            "opacity": np.ones(n, np.float32),
            "scale": np.full((n, 3), 0.6, np.float32),
            "quat": np.tile([1.0, 0.0, 0.0, 0.0], (n, 1)).astype(np.float32),
        }
        try:
            box_name, rest_inv = boxes.make_box(context, data["xyz"],
                                                show=False, name="ColourTestCard")
            r = SplatRenderer(data, box_name, rest_inv)
        except Exception as e:
            self.report({'ERROR'}, "Test card failed: " + str(e))
            return {'CANCELLED'}
        state.add_renderer(r)
        try:
            context.scene.view_settings.view_transform = 'Standard'
            context.scene.view_settings.look = 'None'
        except Exception:
            pass
        try:
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    rv3d = area.spaces.active.region_3d
                    if rv3d is not None:
                        rv3d.view_location = (0.0, 0.0, 0.0)
                        rv3d.view_distance = 12.0
                        rv3d.view_rotation = (1.0, 0.0, 0.0, 0.0)  # top-ish
        except Exception:
            pass
        state.tag_redraw(context)
        self.report({'INFO'}, "Loaded colour test card (9 known colours)")
        return {'FINISHED'}


class FGS_OT_walk(Operator):
    bl_idname = "fgs.walk_navigation"
    bl_label = "Navigation"
    bl_description = ("Blender walk view - W A S D to move, mouse to look, "
                      "Q and E for down and up, Shift to go faster, Tab to "
                      "toggle gravity. Left-click or Enter to finish, Escape "
                      "to cancel and jump back")

    def execute(self, context):
        # walk() is modal and polls for a 3D viewport's WINDOW region. Run from
        # the N-panel the active region is the sidebar, so it must be
        # overridden onto the viewport itself or the operator refuses to start.
        win = area = region = None
        for w in context.window_manager.windows:
            for a in w.screen.areas:
                if a.type != 'VIEW_3D':
                    continue
                for rg in a.regions:
                    if rg.type == 'WINDOW':
                        win, area, region = w, a, rg
                        break
                if region:
                    break
            if region:
                break
        if region is None:
            self.report({'WARNING'}, "No 3D viewport to walk in")
            return {'CANCELLED'}
        try:
            with context.temp_override(window=win, area=area, region=region):
                bpy.ops.view3d.walk('INVOKE_DEFAULT')
        except Exception as e:
            self.report({'ERROR'}, f"Could not start walk navigation: {e}")
            return {'CANCELLED'}
        return {'FINISHED'}


class FGS_OT_apply_display_to_all(Operator):
    bl_idname = "fgs.apply_display_to_all"
    bl_label = "Apply Display Settings to All Models"
    bl_options = {'REGISTER', 'UNDO'}
    bl_description = ("Copy the active model's display settings onto every "
                      "other loaded model")

    def execute(self, context):
        r = state.active_renderer(context)
        if r is None:
            self.report({'WARNING'}, "No active model")
            return {'CANCELLED'}
        try:
            from . import permodel
            n = permodel.apply_to_all(context.scene, state.RENDERERS, r)
        except Exception as e:
            self.report({'ERROR'}, f"Could not apply: {e}")
            return {'CANCELLED'}
        state.tag_redraw(context)
        self.report({'INFO'},
                    f"Applied to {n} other model{'s' if n != 1 else ''}")
        return {'FINISHED'}


class FGS_OT_reload_lod(Operator):
    bl_idname = "fgs.reload_lod"
    bl_label = "Reload at Detail Level"
    bl_options = {'REGISTER'}
    bl_description = ("Re-read the active streamed-SOG model at a different "
                      "detail level, in place, without re-importing")
    lod: bpy.props.EnumProperty(
        name="Detail", default='FULL',
        items=[
            ('FULL', "Complete scene (recommended)",
             "The finest level in full, plus only those coarser splats "
             "covering ground it does not - which is where the sky and far "
             "backdrop live. The whole scene with the least overdraw"),
            ('FAST', "Foreground only (fast)",
             "The finest level alone. Lighter, but the background is stored "
             "as a few giant splats at the coarser levels, so it goes missing"),
            ('COARSE', "Coarse (preview)",
             "The coarsest level alone - fastest way to check a scene"),
            ('ALL', "Every level stacked (heaviest)",
             "All levels including the redundant ones, which paints most "
             "surfaces two or three times over. Complete, but heavier and "
             "hazier than the recommended merge"),
        ])

    def execute(self, context):
        r = state.active_renderer(context)
        if r is None:
            self.report({'WARNING'}, "No active model")
            return {'CANCELLED'}
        from . import persist
        box = bpy.data.objects.get(r.box_name)
        if box is None or persist.KEY not in box:
            self.report({'WARNING'}, "This model has no reload recipe")
            return {'CANCELLED'}
        rec = dict(box[persist.KEY])
        rec["lod"] = self.lod
        box[persist.KEY] = rec
        # Detach the old splats but KEEP the Empty: it carries the recipe the
        # reload is about to read, and it is the handle the user has selected.
        state.remove_renderer(r, recycle=False, keep_box=True)
        try:
            new_r = persist._restore_one(box)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, f"Reload failed: {e}")
            return {'CANCELLED'}
        state.set_active(new_r)
        state.tag_redraw(context)
        self.report({'INFO'}, f"Reloaded at {self.lod}: {new_r.N:,} splats")
        return {'FINISHED'}


class FGS_OT_best_quality(Operator):
    bl_idname = "fgs.best_quality"
    bl_label = "Max Detail"
    bl_description = ("Lock every viewport setting to source-viewer parity for "
                     "maximum crispness: reference kernel, de-spike "
                     "off (thin detail splats keep their sharpness), AA "
                     "compensation off, full-range size cap, per-frame sort, "
                     "full SH, 100% density, neutral grade")

    def execute(self, context):
        sc = context.scene
        sc.fgs_display_mode = 'SPLAT'
        sc.fgs_pc_gaussian = 'PC'     # normalised kernel + 1/255 clip: the
        sc.fgs_sharpness = 1.0        # web twin's defined, haze-free falloff
        sc.fgs_splat_scale = 1.0
        sc.fgs_opacity = 0.0
        sc.fgs_max_pixels = 10.0      # 1000 px cap ~= the reference viewer's 1024
        sc.fgs_despike = 0.0          # OFF: needle splats ARE the fine detail
        sc.fgs_antialias = False      # web viewer default (ON softens)
        sc.fgs_hq_sort = True
        sc.fgs_density = 100.0
        sc.fgs_lod = False
        sc.fgs_lod_points = False
        sc.fgs_exposure = 1.0
        sc.fgs_saturation = 1.0
        sc.fgs_gamma = 1.0
        sc.fgs_tint = (1.0, 1.0, 1.0)
        sc.fgs_sh_quality = 'FULL'
        if any(r.has_sh for r in state.RENDERERS):
            sc.fgs_use_sh = True
        sc.fgs_raw_tones = False      # update callback sets Standard transform
        try:
            sc.view_settings.view_transform = 'Standard'
        except Exception:
            pass
        state.tag_redraw(context)
        self.report({'INFO'},
                    "Max detail: source-viewer parity settings applied")
        return {'FINISHED'}


classes = (FGS_OT_load, FGS_OT_clear, FGS_OT_reset_transform, FGS_OT_remove_active,
           FGS_OT_duplicate, FGS_OT_copy, FGS_OT_paste, FGS_OT_best_quality,
           FGS_OT_apply_display_to_all, FGS_OT_walk, FGS_OT_reload_lod,
           FGS_OT_snapshot, FGS_OT_bake,
           # FGS_OT_bake_surface, FGS_OT_light_setup,  # parked -
           # experimental solid bake + its lighting test (1.20.4)
           FGS_OT_selftest, FGS_OT_test_splat,
           FGS_OT_click_select, FGS_OT_dolly_cursor,
           FGS_OT_frame, FGS_OT_pick_orbit, FGS_OT_select_splat,
           FGS_OT_move_splat, FGS_OT_delete_mode, FGS_OT_undo_delete,
           FGS_OT_restore)


def _menu_import(self, context):
    self.layout.operator(FGS_OT_load.bl_idname,
                         text="Gaussian Splat (.ply / .splat / .sog / .zip)")


# Click-anywhere selection with the NORMAL tools. fgs.click_select returns
# PASS_THROUGH whenever the click is not on a splat model, so these bindings
# are invisible to everything else: native select, box-select drags, gizmos
# and 3D-cursor placement all behave exactly as stock Blender.
#   - "Object Mode" catches plain LMB under the transform tools etc.
#   - the two built-in select tools have their OWN keymaps that run before
#     "Object Mode", so we also insert ourselves there to be checked first.
_addon_keymaps = []


def _register_keymaps():
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc is None:
        return
    for km_name, space in (("Object Mode", 'EMPTY'),
                           ("3D View Tool: Tweak", 'VIEW_3D'),
                           ("3D View Tool: Select Box", 'VIEW_3D')):
        try:
            km = kc.keymaps.new(name=km_name, space_type=space)
            kmi = km.keymap_items.new("fgs.click_select",
                                      'LEFTMOUSE', 'PRESS')
            _addon_keymaps.append((km, kmi))
        except Exception as e:
            print("[SplatBake] keymap registration failed:", km_name, e)

    # Ctrl+C / Ctrl+V. Both operators return PASS_THROUGH when they have
    # nothing of ours to act on (no splat selected / empty splat clipboard),
    # so Blender's own object copy-paste keeps working untouched.
    try:
        km = kc.keymaps.new(name="Object Mode", space_type='EMPTY')
        for idname, key in (("fgs.copy_splat", 'C'), ("fgs.paste_splat", 'V')):
            kmi = km.keymap_items.new(idname, key, 'PRESS', ctrl=True)
            _addon_keymaps.append((km, kmi))
    except Exception as e:
        print("[SplatBake] copy/paste keymap registration failed:", e)


def _unregister_keymaps():
    for km, kmi in _addon_keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    _addon_keymaps.clear()


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.TOPBAR_MT_file_import.append(_menu_import)
    if hasattr(bpy.utils, "register_tool"):
        try:
            bpy.utils.register_tool(FGS_TOOL_splat, separator=True, group=False)
        except Exception as e:
            print("[SplatBake] tool registration failed:", e)
    _register_keymaps()


def unregister():
    _unregister_keymaps()
    if hasattr(bpy.utils, "unregister_tool"):
        try:
            bpy.utils.unregister_tool(FGS_TOOL_splat)
        except Exception:
            pass
    bpy.types.TOPBAR_MT_file_import.remove(_menu_import)
    SplatRenderer._shared_shader = None
    SplatRenderer._shared_point_shader = None
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
