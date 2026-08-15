"""splatcore - Gaussian-splat data handling, independent of Blender.

Everything here is plain Python and numpy. Nothing in this package imports
bpy, gpu, mathutils or any other part of the Blender API, and nothing in it
may: that is the whole point of the boundary.

    loaders   .ply / .splat parsing, outlier trimming, subsampling, upright
    sog       SOG / compressed-scene decoding (see IMAGE_READER)
    sh        spherical-harmonic evaluation, degrees 0-3
    spatial   BucketGrid - culling, normal smoothing, ambient occlusion
    lod       streamed level-of-detail merging

WHY IT IS SEPARATE

The Blender Foundation's position is that add-ons calling Blender's Python
API must be published under a GPL-compatible license. That reasoning applies
to code that uses the API - not to code that merely happens to ship beside
it. These modules parse files and do arithmetic; they would work identically
inside a web viewer, a converter or a render farm, so they are offered under
Apache 2.0 (see LICENSE in this directory) while the Blender half of the
add-on remains GPL-3.0-or-later.

Practical consequence: adding an `import bpy` anywhere in this package would
silently destroy that separation. If a feature here needs something from
Blender, take it as an argument or a hook - the way sog.IMAGE_READER does -
and let the Blender side supply it.

USING IT WITHOUT BLENDER

    from splatcore import loaders
    data = loaders.load_any("capture.ply", want_sh=True)
    # -> dict of numpy arrays: xyz, rgb, opacity, scale, quat (+ dc/sh)

SOG scenes additionally need an image decoder; Pillow is used automatically
if it is installed, or set sog.IMAGE_READER to your own callable.
"""

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Blender-Claude

from . import loaders, sog, sh, spatial, lod   # noqa: F401

__all__ = ["loaders", "sog", "sh", "spatial", "lod"]
