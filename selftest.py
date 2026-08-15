"""Colour-pipeline self-test.

Renders known sRGB colours through the SAME grade()/srgb_to_linear() GLSL the
splat shader uses, into a float offscreen buffer, and reads the pixel back.
This proves the shader's colour maths run correctly on the user's GPU and
produce the expected values - turning "it looks off" into hard numbers, since
the add-on can't be tested on the developer's machine.

What it covers: the shader output stage (grade + sRGB linearisation).
What it can't cover: Blender's View Transform re-encode at display time - that
is verified separately (the loader forces 'Standard', for which the linearise
round-trip is mathematically exact).
"""

# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Blender-Claude


import numpy as np
import gpu
from gpu_extras.batch import batch_for_shader
from .shaders import build_test_shader

TEST_COLORS = [
    (0.10, 0.10, 0.10),
    (0.50, 0.50, 0.50),
    (0.90, 0.90, 0.90),
    (0.80, 0.20, 0.20),
    (0.20, 0.70, 0.30),
    (0.25, 0.45, 0.85),
]
_SIZE = 8


def _srgb_to_linear(c):
    c = np.asarray(c, dtype=np.float64)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _render_center(shader, color, linearize):
    off = gpu.types.GPUOffScreen(_SIZE, _SIZE, format='RGBA32F')
    try:
        with off.bind():
            gpu.state.blend_set('NONE')
            batch = batch_for_shader(
                shader, 'TRIS',
                {"pos": [(-1.0, -1.0), (3.0, -1.0), (-1.0, 3.0)],
                 "test_color": [tuple(color)] * 3})
            shader.bind()
            shader.uniform_float("exposure", 1.0)
            shader.uniform_float("saturation", 1.0)
            shader.uniform_float("gamma", 1.0)
            shader.uniform_float("tint_r", 1.0)
            shader.uniform_float("tint_g", 1.0)
            shader.uniform_float("tint_b", 1.0)
            shader.uniform_float("linearize", 1.0 if linearize else 0.0)
            batch.draw(shader)
            fb = gpu.state.active_framebuffer_get()
            # read exactly one pixel at the centre - no reshape ambiguity
            buf = fb.read_color(_SIZE // 2, _SIZE // 2, 1, 1, 4, 0, 'FLOAT')
    finally:
        off.free()
    rgba = np.array(buf, dtype=np.float32).ravel()
    return rgba[:3]


def run():
    """Return (rows, worst_error, worst_linear_error) or raises on GPU failure.

    rows: list of (color, shown_no_linearize, expect_shown,
                   linear_out, expect_linear)
    """
    shader = build_test_shader()
    rows = []
    worst_disp = 0.0
    worst_lin = 0.0
    for col in TEST_COLORS:
        shown = _render_center(shader, col, linearize=False)   # should equal col
        lin = _render_center(shader, col, linearize=True)      # should equal s2l
        exp_disp = np.asarray(col, dtype=np.float64)
        exp_lin = _srgb_to_linear(col)
        ed = float(np.max(np.abs(shown - exp_disp)))
        el = float(np.max(np.abs(lin - exp_lin)))
        worst_disp = max(worst_disp, ed)
        worst_lin = max(worst_lin, el)
        rows.append((col,
                     tuple(round(float(x), 4) for x in shown),
                     tuple(round(float(x), 4) for x in exp_disp),
                     tuple(round(float(x), 4) for x in lin),
                     tuple(round(float(x), 4) for x in exp_lin)))
    return rows, worst_disp, worst_lin
