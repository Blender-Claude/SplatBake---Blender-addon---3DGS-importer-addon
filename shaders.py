"""GPU shader for screen-space gaussian splatting (EWA projection)."""

import gpu

VERT_SRC = """
float vals[48];
vec3 shv(int k) { return vec3(vals[k], vals[sh_k + k], vals[2 * sh_k + k]); }

void main()
{
    vec4 cam = view * vec4(center, 1.0);
    vec4 pos2d = projection * cam;

    // Behind-camera cull only applies in perspective (ortho w is constant = 1).
    if (is_persp > 0.5 && pos2d.w <= 0.0001) {
        gl_Position = vec4(0.0, 0.0, 2.0, 1.0);
        return;
    }
    float wclip = max(abs(pos2d.w), 1e-6);
    float clip = 1.2 * wclip;
    if (pos2d.x < -clip || pos2d.x > clip || pos2d.y < -clip || pos2d.y > clip) {
        gl_Position = vec4(0.0, 0.0, 2.0, 1.0);
        return;
    }

    float w = quat.x, x = quat.y, y = quat.z, z = quat.w;
    mat3 R = mat3(
        1.0 - 2.0*(y*y + z*z), 2.0*(x*y + w*z),       2.0*(x*z - w*y),
        2.0*(x*y - w*z),       1.0 - 2.0*(x*x + z*z), 2.0*(y*z + w*x),
        2.0*(x*z + w*y),       2.0*(y*z - w*x),       1.0 - 2.0*(x*x + y*y)
    );
    // De-spike: clamp each axis to (aniso x median axis) so long "needle"
    // gaussians become disc-like. Surface discs (max ~= median) are untouched.
    vec3 s = scl;
    if (aniso > 0.0) {
        float mn = min(s.x, min(s.y, s.z));
        float mx = max(s.x, max(s.y, s.z));
        float md = s.x + s.y + s.z - mn - mx;
        s = min(s, vec3(aniso * max(md, 1e-9)));
    }
    mat3 S = mat3(s.x, 0.0, 0.0, 0.0, s.y, 0.0, 0.0, 0.0, s.z);
    // 3D covariance Sigma = R S^2 R^T. R here is the true rotation (column
    // vectors), so M = R*S and Vrk = M*M^T. (Using S*R with M^T*M silently
    // inverts the rotation and skews every anisotropic splat.)
    mat3 M = R * S;
    mat3 Vrk = M * transpose(M);

    // EWA projection Jacobian. Perspective divides by depth; orthographic is a
    // constant linear map (no depth divide) -- using the perspective form in an
    // ortho view is what makes splats explode into giant smears.
    mat3 J;
    if (is_persp > 0.5) {
        J = mat3(
            focal.x / cam.z, 0.0, -(focal.x * cam.x) / (cam.z * cam.z),
            0.0, focal.y / cam.z, -(focal.y * cam.y) / (cam.z * cam.z),
            0.0, 0.0, 0.0
        );
    } else {
        J = mat3(
            focal.x, 0.0, 0.0,
            0.0, focal.y, 0.0,
            0.0, 0.0, 0.0
        );
    }
    mat3 Wm = transpose(mat3(view));
    mat3 T = Wm * J;
    mat3 cov2d = transpose(T) * Vrk * T;

    float a0 = cov2d[0][0];
    float c0 = cov2d[1][1];
    float b = cov2d[0][1];
    // Web viewer reference: dilate the 2D covariance by +0.3 px and keep
    // opacity untouched. (Mip-style opacity compensation is NOT applied - it
    // fades sub-pixel splats to near-zero and washes out large scenes.)
    float a = a0 + 0.3;                 // PC dilates unconditionally
    float c = c0 + 0.3;
    float D = a * c - b * b;
    float op = opacity;
    if (antialias > 0.5) {
        // Web viewer AA opacity compensation (engine default: OFF).
        // The source viewer runs without it; enabling softens distant splats.
        float det_orig = a0 * c0 - b * b;
        op *= sqrt(clamp(det_orig / max(D, 1e-9), 0.0, 1.0));
    }
    float mid = 0.5 * (a + c);
    float disc = sqrt(max(mid * mid - D, 0.0));   // always >= 0 mathematically
    float lambda1 = mid + disc;
    float lambda2 = max(mid - disc, 0.1);   // min thickness, as the web viewer

    float cap = (max_pixels < 1.0) ? 100000.0 : max_pixels;  // 0 = no limit
    float r1 = min(sqrt(2.0 * lambda1), cap);
    float r2 = min(sqrt(2.0 * lambda2), cap);
    // Web viewer minPixelSize=2.0: splats smaller than ~2px are DISCARDED,
    // not blended -- this keeps the source viewer's distance defined instead of
    // accumulating sub-pixel splats into mush.
    if (pc_kernel > 0.5 && max(r1, r2) * 2.0 < min_pixel_size) {
        gl_Position = vec4(0.0, 0.0, 2.0, 1.0);
        return;
    }

    // Major-axis eigenvector, with a guard for circular/degenerate splats
    // (far away the covariance becomes round and the vector collapses to 0).
    vec2 dir;
    if (disc < 1e-6) {
        dir = vec2(1.0, 0.0);
    } else {
        vec2 e = vec2(b, lambda1 - a);
        float el = length(e);
        dir = (el > 1e-9) ? (e / el) : vec2(1.0, 0.0);
    }
    vec2 majorAxis = r1 * dir;
    vec2 minorAxis = r2 * vec2(-dir.y, dir.x);   // eigenvectors are orthogonal

    vec3 base = col;
    if (sh_on > 0.5 && sh_k > 0) {
        int sidx = gl_VertexID / 4;             // 4 verts per splat, data order
        int sx = sidx % sh_w;
        int sy = (sidx / sh_w) * sh_texels;
        for (int t = 0; t < sh_texels; t++) {
            vec4 tx = texelFetch(sh_tex, ivec2(sx, sy + t), 0);
            vals[t*4+0] = tx.x; vals[t*4+1] = tx.y;
            vals[t*4+2] = tx.z; vals[t*4+3] = tx.w;
        }
        vec3 sdir = center - vec3(cam_x, cam_y, cam_z);
        float sl = length(sdir);
        sdir = (sl > 0.0) ? sdir / sl : vec3(0.0, 0.0, 1.0);
        float dx = sdir.x, dy = sdir.y, dz = sdir.z;
        base += 0.4886025119029199 * (-dy * shv(0) + dz * shv(1) - dx * shv(2));
        if (sh_k >= 8) {
            float xx = dx*dx, yy = dy*dy, zz = dz*dz;
            float xy = dx*dy, yz = dy*dz, xz = dx*dz;
            base += 1.0925484305920792 * xy * shv(3)
                  - 1.0925484305920792 * yz * shv(4)
                  + 0.31539156525252005 * (2.0*zz - xx - yy) * shv(5)
                  - 1.0925484305920792 * xz * shv(6)
                  + 0.5462742152960396 * (xx - yy) * shv(7);
            if (sh_k >= 15) {
                base += -0.5900435899266435 * dy * (3.0*xx - yy) * shv(8)
                      +  2.890611442640554  * xy * dz * shv(9)
                      -  0.4570457994644658 * dy * (4.0*zz - xx - yy) * shv(10)
                      +  0.3731763325901154 * dz * (2.0*zz - 3.0*xx - 3.0*yy) * shv(11)
                      -  0.4570457994644658 * dx * (4.0*zz - xx - yy) * shv(12)
                      +  1.445305721320277  * dz * (xx - yy) * shv(13)
                      -  0.5900435899266435 * dx * (xx - 3.0*yy) * shv(14);
            }
        }
    }
    v_color = vec4(clamp(base, 0.0, 1.0), op);
    v_pos = corner;

    // Droplet reveal: splats materialise behind an expanding radial front.
    // wave_r < 0 disables (normal drawing).
    float grow = 1.0;
    if (wave_r >= 0.0) {
        float dw = distance(center, vec3(wave_x, wave_y, wave_z));
        if (dw > wave_r) {                       // still ahead of the front
            gl_Position = vec4(0.0, 0.0, 2.0, 1.0);
            return;
        }
        grow = clamp((wave_r - dw) / max(wave_soft, 1e-4), 0.0, 1.0);
        grow = grow * grow * (3.0 - 2.0 * grow);     // smoothstep ease
    }
    vec2 offset = (corner.x * majorAxis + corner.y * minorAxis)
                  * splat_scale * grow;
    vec2 ndc_center = pos2d.xy / pos2d.w;
    vec2 ndc_offset = offset / viewport * 2.0;
    gl_Position = vec4(ndc_center + ndc_offset, pos2d.z / pos2d.w, 1.0);
}
"""

COLOR_GLSL = """
vec3 grade(vec3 c, vec3 tint)
{
    c *= exposure;
    c *= tint;
    float l = dot(c, vec3(0.2126, 0.7152, 0.0722));   // luminance
    c = mix(vec3(l), c, saturation);                  // saturation
    c = pow(max(c, vec3(0.0)), vec3(1.0 / gamma));     // gamma
    return max(c, vec3(0.0));
}

vec3 srgb_to_linear(vec3 c)
{
    // Splat colours are display-referred (sRGB); the viewport buffer is
    // scene-linear. Decode here so the view transform re-encode is a no-op.
    vec3 lo = c / 12.92;
    vec3 hi = pow((c + vec3(0.055)) / 1.055, vec3(2.4));
    return mix(lo, hi, step(vec3(0.04045), c));
}
"""

FRAG_SRC = COLOR_GLSL + """
void main()
{
    float A = dot(v_pos, v_pos) * sharpness;   // corner +/-2 -> A in [0,4]
    if (A > 4.0) { discard; }
    float B;
    if (pc_kernel > 1.5) {
        // reference normalised gaussian (web viewer spec): zero at the
        // quad edge, weaker tails, 1/255 alpha clip -> defined distance.
        float EXP4 = exp(-4.0);
        B = ((exp(-A) - EXP4) / (1.0 - EXP4)) * v_color.a;
        if (B < 0.0039215687) { discard; }
    } else {
        B = exp(-A) * v_color.a;                 // classic soft falloff
    }
    if (B < opacity_cutoff) { discard; }
    vec3 c = grade(v_color.rgb, vec3(tint_r, tint_g, tint_b));
    if (linearize > 0.5) { c = srgb_to_linear(c); }
    fragColor = vec4(B * c, B);
}
"""


# -- self-test shader: SAME colour maths, on a full-target triangle ---------
# Colour comes in as a vertex attribute (not a push constant) so it can never
# collide with the push-constant size limit on low-end GPUs.
TEST_VERT = """
void main() {
    v_test = test_color;
    gl_Position = vec4(pos, 0.0, 1.0);
}
"""

TEST_FRAG = COLOR_GLSL + """
void main()
{
    vec3 c = grade(v_test, vec3(tint_r, tint_g, tint_b));
    if (linearize > 0.5) { c = srgb_to_linear(c); }
    fragColor = vec4(c, 1.0);
}
"""


def build_test_shader():
    iface = gpu.types.GPUStageInterfaceInfo("fgs_test_iface")
    iface.smooth('VEC3', "v_test")
    info = gpu.types.GPUShaderCreateInfo()
    info.push_constant('FLOAT', "exposure")
    info.push_constant('FLOAT', "saturation")
    info.push_constant('FLOAT', "gamma")
    info.push_constant('FLOAT', "linearize")
    info.push_constant('FLOAT', "tint_r")
    info.push_constant('FLOAT', "tint_g")
    info.push_constant('FLOAT', "tint_b")
    info.vertex_in(0, 'VEC2', "pos")
    info.vertex_in(1, 'VEC3', "test_color")
    info.vertex_out(iface)
    info.fragment_out(0, 'VEC4', "fragColor")
    info.vertex_source(TEST_VERT)
    info.fragment_source(TEST_FRAG)
    return gpu.shader.create_from_info(info)


def build_shader():
    iface = gpu.types.GPUStageInterfaceInfo("splat_iface")
    iface.smooth('VEC4', "v_color")
    iface.smooth('VEC2', "v_pos")

    info = gpu.types.GPUShaderCreateInfo()
    info.push_constant('MAT4', "view")
    info.push_constant('MAT4', "projection")
    info.push_constant('VEC2', "focal")
    info.push_constant('VEC2', "viewport")
    info.push_constant('FLOAT', "splat_scale")
    info.push_constant('FLOAT', "sharpness")
    info.push_constant('FLOAT', "opacity_cutoff")
    info.push_constant('FLOAT', "max_pixels")
    info.push_constant('FLOAT', "antialias")
    info.push_constant('FLOAT', "min_pixel_size")
    info.push_constant('FLOAT', "pc_kernel")
    info.push_constant('FLOAT', "wave_r")
    info.push_constant('FLOAT', "wave_soft")
    info.push_constant('FLOAT', "wave_x")
    info.push_constant('FLOAT', "wave_y")
    info.push_constant('FLOAT', "wave_z")
    info.push_constant('FLOAT', "exposure")
    info.push_constant('FLOAT', "saturation")
    info.push_constant('FLOAT', "gamma")
    info.push_constant('FLOAT', "tint_r")
    info.push_constant('FLOAT', "tint_g")
    info.push_constant('FLOAT', "tint_b")
    info.push_constant('FLOAT', "aniso")
    info.push_constant('FLOAT', "is_persp")
    info.push_constant('FLOAT', "linearize")
    info.push_constant('FLOAT', "sh_on")
    info.push_constant('INT', "sh_k")
    info.push_constant('INT', "sh_texels")
    info.push_constant('INT', "sh_w")
    info.push_constant('FLOAT', "cam_x")
    info.push_constant('FLOAT', "cam_y")
    info.push_constant('FLOAT', "cam_z")
    info.sampler(0, 'FLOAT_2D', "sh_tex")
    info.vertex_in(0, 'VEC2', "corner")
    info.vertex_in(1, 'VEC3', "center")
    info.vertex_in(2, 'VEC3', "col")
    info.vertex_in(3, 'FLOAT', "opacity")
    info.vertex_in(4, 'VEC3', "scl")
    info.vertex_in(5, 'VEC4', "quat")
    info.vertex_out(iface)
    info.fragment_out(0, 'VEC4', "fragColor")
    info.vertex_source(VERT_SRC)
    info.fragment_source(FRAG_SRC)
    return gpu.shader.create_from_info(info)


# ---- lightweight point modes (point cloud / solid points) -----------------

POINT_VERT = """
void main()
{
    // Two-stage reveal:
    //   wave_pr = point front  -- dots exist only INSIDE it (stage 1 fills in)
    //   wave_r  = splat front  -- dots are consumed INSIDE it (stage 2 eats)
    // A radius < 0 means that stage is inactive.
    if (wave_pr >= 0.0) {
        float dw = distance(center, vec3(wave_x, wave_y, wave_z));
        if (dw > wave_pr || (wave_r >= 0.0 && dw <= wave_r)) {
            gl_Position = vec4(0.0, 0.0, 2.0, 1.0);
            v_col = vec3(0.0);
            return;
        }
    }
    gl_Position = mvp * vec4(center, 1.0);
    v_col = col;
}
"""

POINT_FRAG = """
void main()
{
    vec3 c = v_col * exposure * vec3(tint_r, tint_g, tint_b);
    float l = dot(c, vec3(0.2126, 0.7152, 0.0722));
    c = mix(vec3(l), c, saturation);
    c = pow(max(c, vec3(0.0)), vec3(1.0 / gamma));
    c = max(c, vec3(0.0));
    vec3 lo = c / 12.92;
    vec3 hi = pow((c + vec3(0.055)) / 1.055, vec3(2.4));
    fragColor = vec4(mix(lo, hi, step(vec3(0.04045), c)), 1.0);
}
"""


def build_point_shader():
    iface = gpu.types.GPUStageInterfaceInfo("fgs_point_iface")
    iface.smooth('VEC3', "v_col")

    info = gpu.types.GPUShaderCreateInfo()
    info.push_constant('MAT4', "mvp")
    info.push_constant('FLOAT', "exposure")
    info.push_constant('FLOAT', "saturation")
    info.push_constant('FLOAT', "gamma")
    info.push_constant('FLOAT', "tint_r")
    info.push_constant('FLOAT', "tint_g")
    info.push_constant('FLOAT', "tint_b")
    info.push_constant('FLOAT', "wave_r")
    info.push_constant('FLOAT', "wave_pr")
    info.push_constant('FLOAT', "wave_x")
    info.push_constant('FLOAT', "wave_y")
    info.push_constant('FLOAT', "wave_z")
    info.vertex_in(0, 'VEC3', "center")
    info.vertex_in(1, 'VEC3', "col")
    info.vertex_out(iface)
    info.fragment_out(0, 'VEC4', "fragColor")
    info.vertex_source(POINT_VERT)
    info.fragment_source(POINT_FRAG)
    return gpu.shader.create_from_info(info)
