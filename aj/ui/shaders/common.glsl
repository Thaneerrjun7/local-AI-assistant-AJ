// Hashes, noise, the liquid and the background. Included after the uniform block, so these
// functions read the scene uniforms (scene_uniforms.glsl) directly.

const float PI = 3.14159265359;

uint pcg(uint v) {
    uint s = v * 747796405u + 2891336453u;
    uint w = ((s >> ((s >> 28u) + 4u)) ^ s) * 277803737u;
    return (w >> 22u) ^ w;
}

float hash1(float x) {
    return float(pcg(uint(int(floor(x))))) * (1.0 / 4294967295.0);
}

float hash2(vec2 p) {
    ivec2 i = ivec2(floor(p));
    return float(pcg(uint(i.x) + pcg(uint(i.y) + 0x9e3779b9u))) * (1.0 / 4294967295.0);
}

float vnoise(vec2 p) {
    vec2 i = floor(p);
    vec2 f = fract(p);
    vec2 u = f * f * (3.0 - 2.0 * f);
    float a = hash2(i);
    float b = hash2(i + vec2(1.0, 0.0));
    float c = hash2(i + vec2(0.0, 1.0));
    float d = hash2(i + vec2(1.0, 1.0));
    return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

float fbm(vec2 p) {
    float sum = 0.0;
    float amp = 0.5;
    for (int i = 0; i < 4; i++) {
        sum += amp * vnoise(p);
        p = p * 2.03 + vec2(17.1, 9.2);
        amp *= 0.5;
    }
    return sum;
}

// The warp at window point p, as a displacement (xy, px; z is unused). Only the pointer's wake:
// what's under the pointer is dragged along with it, with a pair of eddies either side, and it
// settles back the moment the pointer stops (its velocity is sprung down to zero in QML). The
// rings of water that used to spread from it are gone (the user, 2026-10-23: the warp of the
// dot grid round the mouse stays; "take out the water ripple").
vec3 liquid(vec2 p) {
    float speed = length(vel);
    if (smear <= 0.0 || speed <= 1.0)
        return vec3(0.0);
    vec2 v = p - ptr;
    const float sig = 72.0;
    float g = exp(-dot(v, v) / (2.0 * sig * sig));
    vec2 dir = vel / speed;
    float side = (dir.x * v.y - dir.y * v.x) / sig;
    vec2 drag = vel * 0.03 * g;
    vec2 swirl = vec2(-v.y, v.x) / sig * side * g * min(speed, 1500.0) * 0.012;
    return vec3((drag + swirl) * smear, 0.0);
}

// The background at window point q. `detail` (0..1) is how much fine detail shows: the bare
// background uses 1, frosted glass less.
vec3 scene(vec2 q, float detail) {
    vec2 uv = q / win;
    vec2 a = vec2(uv.x * win.x / win.y, uv.y);
    float tt = t * 0.02;
    vec2 w = vec2(fbm(a * 1.6 + vec2(0.0, tt)), fbm(a * 1.6 + vec2(5.2, -tt)));
    float f = fbm(a * 1.2 + w * 0.95 + vec2(tt * 0.7, 0.0));

    vec3 c0 = vec3(0.050, 0.022, 0.019);  // warm near-black, #0E0605
    vec3 c1 = vec3(0.180, 0.050, 0.036);  // oxblood
    vec3 c2 = vec3(0.270, 0.092, 0.043);  // burnt orange
    vec3 c3 = vec3(0.380, 0.140, 0.060);  // ember, only at the peaks
    vec3 col = mix(c0, c1, smoothstep(0.28, 0.62, f) * warmth);
    col = mix(col, c2, smoothstep(0.52, 0.78, f) * warmth);
    col = mix(col, c3, smoothstep(0.74, 0.95, f) * 0.55 * warmth);

    // The orb's light pooling on the background behind it.
    vec2 od = (q - orbC) / max(orbR, 1.0);
    col += orbTint * orbE * exp(-dot(od, od) * 0.6) * 0.30;

    vec2 vc = uv - vec2(0.5, 0.45);
    col *= 1.0 - dot(vc, vc) * 1.15;

    // A faint grid of dots, mainly there so the liquid has something to bend.
    if (grid > 0.0 && detail > 0.0) {
        const float cell = 18.0;
        vec2 g = (fract(q / cell) - 0.5) * cell;
        float dotA = 1.0 - smoothstep(0.5, 1.3, length(g));
        col += vec3(1.0, 0.88, 0.82) * dotA * grid * 0.06 * detail;
    }
    return col;
}

// Light catching the warp, from the top left.
float liquidLight(vec2 disp) {
    return clamp(dot(disp, normalize(vec2(-0.6, -0.8))) * shine * 0.01, -0.05, 0.14);
}
