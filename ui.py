"""SplatBake: minimal N-panel.

The essentials (import, clear, frame, render) plus
a small Detail box: the few shader settings that decide crispness vs softness
(sharpness, de-spike, size cap, AA compensation) and a one-click preset that
locks everything to source-viewer parity. All other engine props stay registered
at spec defaults so the shared renderer keeps working.
"""

import bpy
from bpy.props import FloatProperty, BoolProperty, EnumProperty, FloatVectorProperty
from bpy.types import Panel, AddonPreferences

from . import state


class FGS_Prefs(AddonPreferences):
    """Global defaults, in Edit > Preferences > Add-ons > SplatBake."""
    bl_idname = __package__

    wave_default: bpy.props.BoolProperty(
        name="Reveal Animation by Default",
        default=True,
        description="Default for the point-cloud -> gaussian reveal in every "
                    ".blend file. Each scene's own tickbox (N-panel) can "
                    "still override it per file")

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "wave_default")
        col.label(text="Applied when a file is opened; the N-panel tickbox "
                       "overrides it per scene.", icon="INFO")


@bpy.app.handlers.persistent
def _fgs_apply_pref_defaults(_dummy=None):
    """On file load: scenes that never chose a value inherit the preference."""
    try:
        prefs = bpy.context.preferences.addons[__package__].preferences
    except Exception:
        return
    for sc in bpy.data.scenes:
        try:
            if not sc.is_property_set("fgs_wave_on"):
                sc.fgs_wave_on = bool(prefs.wave_default)
        except Exception:
            pass


class FGS_PT_main(Panel):
    bl_label = "Splatbake (WIP)"
    bl_idname = "FGS_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "SplatBake"

    def draw(self, context):
        sc = context.scene
        self.layout.label(text="version " + state.VERSION, icon="INFO")
        col = self.layout.column(align=True)
        col.scale_y = 1.35
        col.operator("fgs.load_splat", text="Import Splat (.ply / .sog)",
                     icon="IMPORT")

        self.layout.prop(sc, "fgs_wave_on")

        row = self.layout.row(align=True)
        row.operator("fgs.frame_scene", text="Frame", icon="VIEWZOOM")
        row.operator("fgs.clear_splat", text="Clear", icon="TRASH")

        col2 = self.layout.column(align=True)
        col2.label(text="Gaussian Mode:")
        col2.prop(sc, "fgs_pc_gaussian", text="")
        # TEMPORARILY HIDDEN - the "Web-Exact Tones" colour button. Everything
        # behind it is intact: the fgs_raw_tones property, its _tones_toggled
        # update callback (which switches the view transform), and the
        # renderer's use of it. Un-comment these two lines to bring the button
        # back; nothing else needs changing.
        # col2.prop(sc, "fgs_raw_tones", toggle=True, icon="COLOR",
        #           invert_checkbox=True)
        drow = self.layout.row(align=True)
        drow.prop(sc, "fgs_display_mode", expand=True)
        if sc.fgs_display_mode != 'SPLAT':
            self.layout.prop(sc, "fgs_point_size", slider=True)
        self.layout.prop(sc, "fgs_density", slider=True)
        self.layout.prop(sc, "fgs_sh_quality", text="SH")

        dbox = self.layout.box()
        dbox.label(text="Detail:", icon="SHADING_RENDERED")
        dbox.operator("fgs.best_quality",
                      text="Max Detail", icon="CHECKMARK")
        dcol = dbox.column(align=True)
        dcol.prop(sc, "fgs_sharpness", slider=True)
        dcol.prop(sc, "fgs_despike", slider=True)
        dcol.prop(sc, "fgs_max_pixels", slider=True)
        dbox.prop(sc, "fgs_antialias")
        pcol = dbox.column(align=True)
        pcol.prop(sc, "fgs_solid_fast")
        sub = pcol.row(align=True)
        sub.enabled = sc.fgs_solid_fast
        sub.prop(sc, "fgs_solid_density", slider=True)

        rbox = self.layout.box()
        rbox.label(text="Render:", icon="RESTRICT_RENDER_OFF")
        rcol = rbox.column(align=True)
        op = rcol.operator("fgs.snapshot_render", text="Snapshot Still",
                           icon="RENDER_STILL")
        op.animation = False
        op = rcol.operator("fgs.snapshot_render", text="Snapshot Animation",
                           icon="RENDER_ANIMATION")
        op.animation = True
        hint = rbox.column(align=True)
        hint.scale_y = 0.75
        hint.label(text="Exact splat look (viewport capture)")
        bcol = rbox.column(align=True)
        bcol.operator("fgs.bake_mesh", text="Bake Discs (for F12)",
                      icon="OUTLINER_OB_POINTCLOUD")
        bcol.operator("fgs.bake_surface", text="Bake Solid Surface (F12)",
                      icon="MESH_ICOSPHERE")
        hint2 = rbox.column(align=True)
        hint2.scale_y = 0.75
        hint2.label(text="Real meshes: render in Cycles / EEVEE")
        hint2.label(text="Bake dialog: tick 'React to Scene Lights'")



        box = self.layout.box()
        box.scale_y = 0.85
        box.label(text="Click any model = select it")
        box.label(text="G / R / S = move, rotate, scale")
        box.label(text="Ctrl+C / Ctrl+V = copy / paste")
        box.label(text="H / Alt+H = hide / unhide it")

        self.layout.separator()
        vcol = self.layout.column(align=True)
        vcol.label(text="Verify:", icon="CHECKMARK")
        vcol.operator("fgs.selftest", text="Run Colour Self-Test")
        vcol.operator("fgs.test_splat", text="Load Colour Test Card")
        msg = getattr(context.window_manager, "fgs_selftest_msg", "")
        if msg:
            box = vcol.box()
            box.scale_y = 0.8
            box.label(text=msg)

        if state.RENDERERS:
            total = sum(r.N for r in state.RENDERERS)
            n = len(state.RENDERERS)
            self.layout.label(
                text=f"{total:,} splats in {n} model{'s' if n > 1 else ''}",
                icon="OUTLINER_OB_POINTCLOUD")
            hidden = n - len(state.visible_renderers(context.space_data))
            if hidden:
                self.layout.label(text=f"{hidden} hidden (Alt+H to restore)",
                                  icon="HIDE_ON")
        else:
            self.layout.label(text="Import a splat file to begin")


def _redraw(self, context):
    state.tag_redraw(context)


def _tones_toggled(self, context):
    """Switch between the two colour paths instantly (no re-import needed).
    OFF: Standard view transform, splats linearised (the classic look).
    ON : Raw view transform, splats accumulate in sRGB like the web twin."""
    try:
        vs = context.scene.view_settings
        if context.scene.fgs_raw_tones:
            try:
                vs.view_transform = 'Raw'
            except Exception:
                vs.view_transform = 'Standard'
        else:
            vs.view_transform = 'Standard'
        vs.look = 'None'
        vs.exposure = 0.0
        vs.gamma = 1.0
    except Exception:
        pass
    state.tag_redraw(context)



_PANELS = (FGS_Prefs, FGS_PT_main)


def register():
    bpy.types.WindowManager.fgs_selftest_msg = bpy.props.StringProperty(default="")
    S = bpy.types.Scene
    # Engine properties, fixed to the source-viewer spec (not shown in the panel).
    S.fgs_density = FloatProperty(
        name="Density", default=100.0, min=1.0, max=100.0, subtype='PERCENTAGE',
        update=_redraw,
        description="Fraction of splats drawn (uniform random subset). Drop "
                    "for faster navigation on heavy scenes; sorting and "
                    "drawing both scale with it")
    S.fgs_splat_scale = FloatProperty(
        name="Splat Size", default=1.0, min=0.0, max=10.0, update=_redraw)
    S.fgs_use_sh = BoolProperty(name="View-Dependent Colour", default=True,
                                update=_redraw)
    S.fgs_pc_gaussian = EnumProperty(
        name="Gaussian Mode", default='PC', update=_redraw,
        description="How each splat's gaussian is shaped and culled",
        items=[
            ('SOFT', "Soft (classic Blender)",
             "Plain exp falloff, every splat drawn however tiny - the "
             "original soft/mushy distance"),
            ('V215', "Defined (v2.15)",
             "Plain exp falloff plus the web viewer 2px minimum-size "
             "discard - the first 'This is it!' look"),
            ('PC', "Web Viewer Exact (v2.18)",
             "Full engine-source treatment: 2px discard, normalised "
             "falloff (zero at the edge) and 1/255 alpha clip - matches "
             "the web twin exactly"),
        ])
    S.fgs_wave_on = BoolProperty(
        name="Reveal Animation on Import", default=True,
        description="Play the point-cloud -> gaussian reveal when a model is "
                    "imported (this scene only). The default for all files "
                    "lives in Add-on Preferences. Purely cosmetic: the model "
                    "is fully loaded before it runs")
    S.fgs_sh_quality = EnumProperty(
        name="View Colour Quality", default='FULL', update=_redraw,
        description="Spherical-harmonics detail. Lower = fewer texture "
                    "fetches per splat = faster on integrated GPUs",
        items=[
            ('OFF', "Off (base colour)", "No view-dependent colour - fastest"),
            ('DEG1', "Low (degree 1)", "3 coefficients - most of the "
             "directional shading at a quarter of the cost"),
            ('DEG2', "Medium (degree 2)", "8 coefficients"),
            ('FULL', "Full (degree 3)", "All 15 coefficients - source-viewer "
             "quality, heaviest"),
        ])
    S.fgs_raw_tones = BoolProperty(
        name="Web-Exact Tones", default=False, update=_tones_toggled,
        description="ON: blend splats in sRGB space (Raw view transform), the "
                    "exact tone maths of the source viewer - darker, "
                    "punchier. OFF: Standard view transform with linear "
                    "blending (the classic soft look). Switches instantly")
    S.fgs_show_bbox = BoolProperty(name="Show Handle", default=False,
                                   update=_redraw)
    S.fgs_display_mode = EnumProperty(
        name="Display", default='SPLAT', update=_redraw,
        description="How the model is drawn",
        items=[('SPLAT', "Splats", "Full gaussian rendering"),
               ('POINTS', "Point Cloud", "Scattered dots - fastest, shows "
                "the scene's contours at a glance")])
    S.fgs_point_size = FloatProperty(name="Point Size", default=4.0,
                                     min=1.0, max=20.0, update=_redraw)
    S.fgs_sharpness = FloatProperty(name="Sharpness", default=1.0, min=0.2,
                                    soft_max=8.0, update=_redraw)
    S.fgs_opacity = FloatProperty(name="Opacity Cutoff", default=0.0,
                                  min=0.0, max=0.5, update=_redraw)
    S.fgs_max_pixels = FloatProperty(name="Max Splat Size", default=10.0,
                                     min=0.0, max=10.0, update=_redraw)
    S.fgs_despike = FloatProperty(
        name="De-spike", default=0.0, min=0.0, max=10.0, update=_redraw,
        description="0 = off (source-viewer parity: thin anisotropic splats - "
                    "edges, wires, hair - keep their full sharpness). Raise "
                    "(~6-10) only if a scene shows needle spike artifacts; "
                    "clamping needles also rounds off fine linear detail")
    S.fgs_exposure = FloatProperty(name="Exposure", default=1.0, min=0.1,
                                   max=4.0, update=_redraw)
    S.fgs_saturation = FloatProperty(name="Saturation", default=1.0, min=0.0,
                                     max=3.0, update=_redraw)
    S.fgs_gamma = FloatProperty(name="Gamma", default=1.0, min=0.2, max=3.0,
                                update=_redraw)
    S.fgs_tint = FloatVectorProperty(name="Tint", subtype='COLOR', size=3,
                                     default=(1.0, 1.0, 1.0), min=0.0, max=1.0,
                                     update=_redraw)
    S.fgs_antialias = BoolProperty(
        name="AA Compensation", default=False, update=_redraw,
        description="The web viewer's 'antiAlias' opacity compensation. The source viewer "
                    "runs with this OFF (engine default); enabling softens "
                    "distant splats")
    S.fgs_hq_sort = BoolProperty(name="Per-frame Sort", default=True,
                                 update=_redraw)
    S.fgs_solid_fast = BoolProperty(
        name="Fast Solid-Mode Preview", default=True, update=_redraw,
        description="While viewport shading is Solid or Wireframe, draw fewer "
                    "splats, skip view-dependent colour and re-sort less "
                    "often, for smoother navigation before you bake. Material "
                    "Preview and Rendered stay at full quality")
    S.fgs_solid_density = FloatProperty(
        name="Solid Detail", default=25.0, min=1.0, max=100.0,
        subtype='PERCENTAGE', update=_redraw,
        description="Fraction of splats drawn in Solid/Wireframe shading. "
                    "25% is roughly 4x less to sort and draw; drop to 10% on "
                    "very heavy scenes, raise if the preview is too sparse")
    S.fgs_lod = BoolProperty(name="Distance LOD", default=False, update=_redraw)
    S.fgs_lod_points = BoolProperty(name="Point Cloud beyond 30u",
                                    default=False, update=_redraw)
    for c in _PANELS:
        bpy.utils.register_class(c)
    if _fgs_apply_pref_defaults not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_fgs_apply_pref_defaults)
    try:
        _fgs_apply_pref_defaults()      # also sync the currently open file
    except Exception:
        pass


def unregister():
    if _fgs_apply_pref_defaults in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_fgs_apply_pref_defaults)
    if hasattr(bpy.types.WindowManager, "fgs_selftest_msg"):
        del bpy.types.WindowManager.fgs_selftest_msg
    for c in reversed(_PANELS):
        bpy.utils.unregister_class(c)
    S = bpy.types.Scene
    for p in ("fgs_density", "fgs_pc_gaussian", "fgs_sh_quality",
              "fgs_wave_on", "fgs_raw_tones", "fgs_splat_scale", "fgs_sharpness", "fgs_opacity",
              "fgs_max_pixels", "fgs_exposure", "fgs_antialias", "fgs_use_sh",
              "fgs_display_mode", "fgs_point_size", "fgs_hq_sort", "fgs_despike",
              "fgs_lod", "fgs_lod_points", "fgs_show_bbox",
              "fgs_solid_fast", "fgs_solid_density",
              "fgs_saturation", "fgs_gamma", "fgs_tint"):
        if hasattr(S, p):
            delattr(S, p)
