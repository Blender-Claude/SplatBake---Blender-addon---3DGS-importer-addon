"""SplatBake - import a Gaussian splat, bake it to real geometry, render it.

Casu fictus, claudo favente
    * Mars, Mattityahu, Yohanan and Claude

Copyright (C) 2026 Blender-Claude and MMJ
Home:    https://github.com/Blender-Claude
License: GPL-3.0-or-later (see LICENSE)

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version. It is distributed WITHOUT ANY WARRANTY; see the licence for details.


This is a Blender extension (manifest-based). Metadata lives in
blender_manifest.toml; this module only wires the pieces together.

Modules:
    loaders    - PLY / .splat parsing, SH extraction, trim / subsample / upright
    sog        - SOG v2 reader (bundled .sog, unbundled meta.json,
                 and streamed / LOD scenes via lod-meta.json)
    sh         - spherical-harmonic evaluation (view-dependent colour)
    shaders    - GLSL sources and the shader builder (EWA splatting)
    boxes      - proxy Empties that drive each model's transform
    renderer   - the per-instance SplatRenderer (GPU buffers, sort, draw)
    state      - the list of live instances, draw handler, box watcher
    lighting   - scene-lit bake material (opt-in, self-contained)
    uvtools    - UV unwrap + colour-to-texture rasteriser for the
                 solid surface bake (self-contained)
    permodel   - optional per-model display settings (self-contained)
    persist    - stores a reload recipe on each handle so models come back
                 when a .blend is reopened (self-contained)
    operators  - load / clear / reset / duplicate / delete / undo / restore
    ui         - the N-panel and the scene properties
"""

from . import state, operators, ui, persist, permodel


def register():
    # First: the per-model PropertyGroup must exist before the panel or the
    # draw callback can reference Object.splatbake_display.
    permodel.register()
    operators.register()
    ui.register()
    state.register()
    # Last: the load_post handler needs the properties and operators in place
    # before it can restore anything.
    persist.register()


def unregister():
    persist.unregister()
    state.unregister()
    ui.unregister()
    operators.unregister()
    permodel.unregister()
