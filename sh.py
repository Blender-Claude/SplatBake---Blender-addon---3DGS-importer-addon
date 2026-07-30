"""View-dependent colour via spherical harmonics.

Evaluated on the CPU (vectorised numpy) at the renderer's sort cadence, so the
GPU buffers stay small. Matches the 3DGS reference SH basis up to degree 3.
"""

import numpy as np

_C0 = 0.28209479177387814
_C1 = 0.4886025119029199


def eval_sh(centers, dc, sh, cam_local):
    """Return per-splat RGB (N,3) in [0,1].

    centers   (N,3) splat positions in the SH's own frame
    dc        (N,3) degree-0 coefficients
    sh        (N,K,3) higher-order coefficients (K = 3, 8, or 15)
    cam_local (3,)   camera position in the same frame
    """
    d = centers - cam_local
    nrm = np.linalg.norm(d, axis=1, keepdims=True)
    nrm[nrm == 0] = 1.0
    d = d / nrm
    x = d[:, 0:1]; y = d[:, 1:2]; z = d[:, 2:3]

    k = sh.shape[1]
    res = _C0 * dc
    if k >= 3:
        res = res - _C1 * y * sh[:, 0, :] + _C1 * z * sh[:, 1, :] - _C1 * x * sh[:, 2, :]
    if k >= 8:
        xx = x * x; yy = y * y; zz = z * z
        xy = x * y; yz = y * z; xz = x * z
        res = (res
               + 1.0925484305920792 * xy * sh[:, 3, :]
               - 1.0925484305920792 * yz * sh[:, 4, :]
               + 0.31539156525252005 * (2.0 * zz - xx - yy) * sh[:, 5, :]
               - 1.0925484305920792 * xz * sh[:, 6, :]
               + 0.5462742152960396 * (xx - yy) * sh[:, 7, :])
    if k >= 15:
        res = (res
               - 0.5900435899266435 * y * (3.0 * xx - yy) * sh[:, 8, :]
               + 2.890611442640554 * xy * z * sh[:, 9, :]
               - 0.4570457994644658 * y * (4.0 * zz - xx - yy) * sh[:, 10, :]
               + 0.3731763325901154 * z * (2.0 * zz - 3.0 * xx - 3.0 * yy) * sh[:, 11, :]
               - 0.4570457994644658 * x * (4.0 * zz - xx - yy) * sh[:, 12, :]
               + 1.445305721320277 * z * (xx - yy) * sh[:, 13, :]
               - 0.5900435899266435 * x * (xx - 3.0 * yy) * sh[:, 14, :])
    return np.clip(res + 0.5, 0.0, 1.0).astype(np.float32)
