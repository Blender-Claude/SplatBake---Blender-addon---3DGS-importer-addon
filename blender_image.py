"""Blender's image decoder, handed to splatcore.sog at registration.

# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Blender-Claude

This is the Blender half of the one dependency splatcore could not avoid:
SOG scenes are PNG/WebP images, and something has to decode them. Blender
already ships a decoder that handles WebP with no extra Python packages, so
inside Blender it is the right one to use - it just cannot live in an
Apache-licensed package, because using it means calling bpy.

register() installs it; splatcore falls back to Pillow if nothing is
installed, which is what a standalone user gets.
"""

import os
import numpy as np


def read_u8_image(path):
    """Decode an image to a (H, W, 4) uint8 array, origin TOP-LEFT.

    Two settings are non-negotiable and both were bugs once:
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


def install():
    """Point splatcore.sog at Blender's decoder. Never fatal: without it the
    core falls back to Pillow, and .ply / .splat files do not need it at
    all - so a failure here must not stop the add-on from loading."""
    try:
        from .splatcore import sog
        sog.IMAGE_READER = read_u8_image
    except Exception as e:
        print("[SplatBake] could not install the Blender image reader:", e)
