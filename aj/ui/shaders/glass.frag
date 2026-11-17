#version 440
// Liquid glass: one rounded shape (a button, the text box, a panel) that redraws the background
// behind it bent like a thick lens, with a lit rim, a highlight that follows the pointer, and a
// soft shadow. It reaches a little toward the pointer and springs back (`pull`).
//
// The background is drawn by a function (common.glsl), so the glass recomputes it at the bent
// positions instead of copying the screen: no extra render pass, and no seam at the edge.

layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;

layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
#include "scene_uniforms.glsl"
    vec2 origin;       // this item's top-left in the window, px
    vec2 size;         // this item's size, px (the shape plus room for the shadow)
    vec4 shape;        // the glass shape inside the item: x, y, width, height, px
    float radius;      // corner radius, px (half the height makes a capsule)
    vec2 pointer;      // the pointer, in this item's px
    float hover;       // 0..1, sprung
    float press;       // 0..1, sprung
    vec2 pull;         // how far the shape reaches toward the pointer, px
    float tint;        // white in the glass, 0..1
    float frost;       // 0 = clear, 1 = the background's fine detail is gone
    float refraction;  // lens strength at the rim
    float rim;         // rim light strength
    float shadow;      // shadow strength
    float dispersion;  // colour split at the rim
    float solid;       // 1 = Windows' transparency effects are off: a plain frosted panel
                       // (QML then stops feeding it the liquid, so it isn't redrawn for it)
};

#include "common.glsl"

float sdRoundBox(vec2 p, vec2 b, float r) {
    vec2 q = abs(p) - b + r;
    return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - r;
}

void main() {
    vec2 local = qt_TexCoord0 * size;
    vec2 c = shape.xy + shape.zw * 0.5;
    vec2 hs = shape.zw * 0.5;
    float r = min(radius, min(hs.x, hs.y));

    // Reach toward the pointer: stretch along the pull and shift half of it.
    vec2 lp = local - c;
    float pl = length(pull);
    if (pl > 0.001) {
        vec2 pd = pull / pl;
        float s = 1.0 + pl / max(hs.x, hs.y);
        lp -= pd * dot(lp, pd) * (1.0 - 1.0 / s);
        lp -= pull * 0.5;
    }

    float sd = sdRoundBox(lp, hs, r);
    const vec2 e = vec2(0.5, 0.0);
    vec2 n = vec2(sdRoundBox(lp + e.xy, hs, r) - sdRoundBox(lp - e.xy, hs, r),
                  sdRoundBox(lp + e.yx, hs, r) - sdRoundBox(lp - e.yx, hs, r));
    n /= max(length(n), 1e-5);  // outward normal of the edge

    vec2 wp = origin + local;
    float cover = 1.0 - smoothstep(-0.7, 0.7, sd);

    // Shadow, outside the shape: soft, a little below.
    float sdS = sdRoundBox(lp - vec2(0.0, 5.0), hs, r);
    float shA = shadow * 0.42 * exp(-max(sdS, 0.0) / 11.0) * (1.0 - cover);

    if (cover <= 0.0) {
        fragColor = vec4(0.0, 0.0, 0.0, shA) * qt_Opacity;
        return;
    }

    float depth = max(-sd, 0.0);                      // px in from the edge
    float bevel = max(min(r, min(hs.x, hs.y)) * 0.85, 6.0);
    float edge = 1.0 - clamp(depth / bevel, 0.0, 1.0);  // 1 at the rim, 0 in the flat middle
    float lens = edge * edge * (3.0 - 2.0 * edge);
    // At the rim the glass shows what is just beyond its edge, squeezed in, as a thick lens does.
    vec2 off = n * lens * refraction * bevel * 0.9;

    vec3 liq = liquid(wp);
    vec2 q = wp + liq.xy * 0.6;                       // the warp carries on under the glass, softened
    vec3 col = scene(q + off, 1.0 - frost);
    col += vec3(1.0, 0.92, 0.88) * liquidLight(liq.xy) * 0.6;
    // Dispersion: the lens splits colour at the rim, warm on one side and cool on the other.
    // Drawn as a fringe rather than sampling the background three times (a third of the cost).
    float side = dot(n, normalize(vec2(-0.55, -0.85)));
    col += vec3(0.10, 0.03, -0.04) * dispersion * lens * max(side, 0.0) * 4.0
         + vec3(-0.04, 0.02, 0.10) * dispersion * lens * max(-side, 0.0) * 4.0;

    // Reduced transparency: a plain frosted panel in place of what's behind. The tint, hover,
    // press and light below still apply, so a checked or pressed button still shows it.
    col = mix(col, vec3(0.20, 0.13, 0.12), solid);

    // The body: a touch of white, brighter when hovered and more when pressed.
    col = mix(col, vec3(1.0), tint + hover * 0.03 + press * 0.08);
    col *= 1.05 + press * 0.05;

    // Light: a crisp rim lit from the top left (and, weaker, the bottom right, as Apple's glass
    // is), a soft sheen down the bevel, and a glow under the pointer.
    vec2 L = normalize(vec2(-0.55, -0.85));
    float facing = dot(n, L);
    float rimBand = 1.0 - smoothstep(0.0, 1.6, depth);
    float rimLight = rimBand * (0.22 + 0.78 * max(facing, 0.0) + 0.45 * max(-facing, 0.0));
    float sheen = lens * 0.10 * max(facing, 0.0);
    vec2 pd = lp - (pointer - c);
    float spread = 0.55 * max(hs.x, hs.y) + 18.0;
    float spot = exp(-dot(pd, pd) / (2.0 * spread * spread)) * hover * 0.10;
    col += vec3(1.0) * (rimLight * rim + sheen + spot);

    // A slight shade toward the bottom, so it reads as thick.
    col *= 1.0 - 0.07 * smoothstep(-hs.y, hs.y, lp.y);

    vec4 glass = vec4(max(col, 0.0), 1.0);
    fragColor = (glass * cover + vec4(0.0, 0.0, 0.0, shA)) * qt_Opacity;
}
