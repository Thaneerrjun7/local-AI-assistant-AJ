#version 440
// The orb: a clear glass core inside a lattice of dots wired like neurons, with a few dotted
// rings outside. Everything is ray-traced per pixel from the uniforms; there is no geometry.
//
// Dots sit on a spherical Fibonacci lattice (even spacing, no bunching at the poles). For each
// pixel the nearest dot is found directly with the inverse mapping of Keinert et al. 2015,
// "Spherical Fibonacci Mapping", so the cost doesn't grow with the number of dots.
//
// The lattice is a membrane: speaking, hearing and dictating push it in and out (`membrane`).
// With no voice it rests, so the orb is still when nobody is talking.
//
// Every clock arrives already wrapped (Orb.qml works them out in 64-bit JavaScript), so nothing
// here loses precision however long the app runs.

layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;

layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    float px;          // one physical pixel, in orb units (the item spans -1..1)
    float spin;        // rad about the vertical axis
    float tiltX;       // extra turn toward the pointer, rad
    float tiltY;
    float band;        // the Working state's scrolling bands, 0..1
    // Phases, each wrapped: the firing clock's (sparks, pulses on the synapses, the speaking
    // wave and its lobes) and the plain clock's (hearing, dictating, asleep, loading, mic failed).
    float phSpark;
    float phPulse;
    float phWave;
    float phLobe;
    float phHear;
    float phDict;
    float phBreath;
    float phScan;
    float phFail;
    float phBlink;
    float phAlarm;     // error's slow throb
    vec3 coreAng;      // the core's swirl: three angles
    vec3 ringZ;        // each ring's two turning angles and its comet
    vec3 ringX;
    vec3 ringC;
    float ringP;       // the rings' dot pattern
    float dots;        // how many neurons
    float dotSize;     // dot radius, as a fraction of the spacing between dots
    float dim;         // overall brightness
    float linkAlpha;   // the resting synapse lines
    float fire;        // cascade strength
    float spark;       // chance a neuron sparks on its own, per epoch
    float glow;        // halo around dots and the lattice
    float shell;       // lattice radius scale
    float assemble;    // 0 = collapsed into the core, 1 = the whole lattice
    float coreGlow;
    float voice;       // AJ's voice, 0..1
    float mic;         // yours, 0..1
    float wSpeak;      // status weights, 0..1, sprung so statuses blend
    float wHear;
    float wDictate;
    float wWork;
    float wLoad;
    float wFail;
    float wError;
    float wSleep;
    float ringAlpha;
    float ringTight;   // 0 = wide rings, 1 = gyroscope rings around the core
    float dispersion;
    vec4 c0;           // the three cascades: origin (xyz) and seed (w), from Orb.qml
    vec4 c1;
    vec4 c2;
    vec3 taus;         // how far through its life each cascade is, 0..1
};

const float PI = 3.14159265359;
const float PHI = 1.61803398875;
const float CORE = 0.23;
// The lookup always includes a neuron among a pixel's four candidates within 0.51 of a
// lattice spacing of its lattice point (measured, 2026-10-23). A nudge of up to 0.15 leaves
// 0.36 around the dot itself; bodies and halos end inside REACH so none is cut off.
const float NUDGE = 0.3;
const float REACH = 0.34;
// Its colours: ice white, and the two alarms (the user, 2026-10-23): error turns the whole orb
// a bright red; a mic that won't work sends orange out of it.
const vec3 ICE = vec3(0.96, 0.98, 1.0);
const vec3 RED = vec3(1.0, 0.16, 0.12);
const vec3 ORANGE = vec3(1.0, 0.47, 0.14);

mat3 Vt;  // view -> object

uint pcg(uint v) {
    uint s = v * 747796405u + 2891336453u;
    uint w = ((s >> ((s >> 28u) + 4u)) ^ s) * 277803737u;
    return (w >> 22u) ^ w;
}

// 0..1 for a whole number (a neuron's index, an epoch) and a salt: integer hashing, so a
// compiler's rounding can't change the result.
float rnd(float i, uint salt) {
    return float(pcg(uint(int(i)) + pcg(salt))) * (1.0 / 4294967295.0);
}

float rnd2(float a, float b, uint salt) {
    return float(pcg(uint(int(a)) * 7919u + pcg(uint(int(b)) + pcg(salt)))) * (1.0 / 4294967295.0);
}

mat3 rotX(float a) {
    float c = cos(a), s = sin(a);
    return mat3(1.0, 0.0, 0.0, 0.0, c, s, 0.0, -s, c);
}

mat3 rotY(float a) {
    float c = cos(a), s = sin(a);
    return mat3(c, 0.0, -s, 0.0, 1.0, 0.0, s, 0.0, c);
}

mat3 rotZ(float a) {
    float c = cos(a), s = sin(a);
    return mat3(c, s, 0.0, -s, c, 0.0, 0.0, 0.0, 1.0);
}

vec3 sfPoint(float i, float n) {
    float phi = 2.0 * PI * fract(i * PHI);
    float ct = 1.0 - (2.0 * i + 1.0) / n;
    float st = sqrt(max(0.0, 1.0 - ct * ct));
    return vec3(cos(phi) * st, sin(phi) * st, ct);
}

// One candidate of the inverse mapping: its index, or -1 if out of range. Keeps the nearest.
float sfCandidate(vec2 F, vec2 c, vec2 o, vec3 p, float n, inout float best, inout float j) {
    float i = dot(F, o + c);
    if (i < 0.0 || i > n - 1.0)
        return -1.0;
    vec3 q = sfPoint(i, n);
    float d = dot(q - p, q - p);
    if (d < best) {
        best = d;
        j = i;
    }
    return i;
}

// The nearest of n lattice points to unit vector p. `cands` gets the four candidates the
// mapping looks at (-1 where out of range): the nearest and its close neighbours. Written out
// one by one: Direct3D can't index into a vector it is writing.
float sfNearest(vec3 p, float n, out vec4 cands) {
    float m = 1.0 - 1.0 / n;
    float phi = min(atan(p.y, p.x), PI);
    float ct = clamp(p.z, -1.0, 1.0);
    float k = max(2.0, floor(log(n * PI * sqrt(5.0) * max(1.0 - ct * ct, 1e-7)) / log(PHI + 1.0)));
    float Fk = pow(PHI, k) / sqrt(5.0);
    vec2 F = vec2(round(Fk), round(Fk * PHI));
    vec2 ka = 2.0 * F / n;
    vec2 kb = 2.0 * PI * (fract((F + 1.0) * PHI) - (PHI - 1.0));
    mat2 iB = mat2(ka.y, -ka.x, kb.y, -kb.x) / (ka.y * kb.x - ka.x * kb.y);
    vec2 c = floor(iB * vec2(phi, ct - m));
    float best = 8.0;
    float j = -1.0;
    cands.x = sfCandidate(F, c, vec2(0.0, 0.0), p, n, best, j);
    cands.y = sfCandidate(F, c, vec2(1.0, 0.0), p, n, best, j);
    cands.z = sfCandidate(F, c, vec2(0.0, 1.0), p, n, best, j);
    cands.w = sfCandidate(F, c, vec2(1.0, 1.0), p, n, best, j);
    return j;
}

// One cascade's wavefront at q: a ring spreading over the sphere from origin c.xyz, tau of the
// way through its life.
float wave(vec3 q, vec4 c, float tau) {
    float ang = acos(clamp(dot(q, c.xyz), -1.0, 1.0));
    float x = (ang - tau * 3.4) / 0.22;
    return exp(-x * x) * (1.0 - tau);
}

// How strongly neuron i (at q) is firing: the cascades light only some of the neurons they
// pass (c.w seeds which), plus lone sparks.
float activation(float i, vec3 q) {
    float a = 0.0;
    if (fire > 0.0) {
        float g0 = step(0.42, rnd2(i, c0.w, 1u));
        float g1 = step(0.42, rnd2(i, c1.w, 1u));
        float g2 = step(0.42, rnd2(i, c2.w, 1u));
        a = max(wave(q, c0, taus.x) * g0, max(wave(q, c1, taus.y) * g1, wave(q, c2, taus.z) * g2)) * fire;
    }
    float sp = phSpark + rnd(i, 2u) * 13.0;
    float on = step(1.0 - spark, rnd2(i, floor(sp), 3u));
    return max(a, on * exp(-fract(sp) * 5.0));
}

// How far the membrane is pushed out at a direction (uv: view space, uo: object space).
float membrane(vec3 uv, vec3 uo) {
    float lat = asin(clamp(uo.y, -1.0, 1.0));
    float lon = atan(uo.z, uo.x);
    float d = 0.0;
    // Speaking: waves run from the equator to the poles, as loud as the voice.
    float sw = 0.5 + 0.5 * sin(abs(lat) * 9.0 - phWave);
    float lobes = 0.5 + 0.5 * sin(lon * 3.0 + phLobe);
    d += wSpeak * voice * (0.035 + 0.075 * sw * (0.6 + 0.4 * lobes));
    // Hearing: rings run from the point facing you toward the back.
    float ang = acos(clamp(uv.z, -1.0, 1.0));
    d += wHear * mic * 0.07 * sin(ang * 7.0 - phHear) * exp(-ang * 0.9);
    // Dictating: a waveform round the equator.
    d += wDictate * mic * 0.10 * exp(-lat * lat / 0.03) * (0.5 + 0.5 * sin(lon * 12.0 - phDict));
    // Asleep: a slow breath.
    d += wSleep * 0.018 * sin(phBreath);
    // Mic failed: one side sags.
    d -= wFail * 0.09 * smoothstep(0.1, 0.9, uo.x);
    // Error: it drifts apart.
    d += wError * 0.06;
    return d;
}

// Where the ray through p meets the membrane on one side (side 1: front, -1: back).
bool hitShell(vec2 p, float side, float Rs, out vec3 hv, out vec3 ho, out float Rh) {
    float r2 = dot(p, p);
    float rr = min(sqrt(r2), Rs * 0.999);
    vec3 u = normalize(vec3(p, side * sqrt(max(Rs * Rs - rr * rr, 0.0))));
    Rh = Rs;
    for (int i = 0; i < 2; i++) {
        Rh = Rs * (1.0 + membrane(u, Vt * u));
        u = normalize(vec3(p, side * sqrt(max(Rh * Rh - r2, 0.0))));
    }
    hv = vec3(p, side * sqrt(max(Rh * Rh - r2, 0.0)));
    ho = Vt * normalize(hv);
    return r2 <= Rh * Rh;
}

struct Neuron {
    float i;     // index, -1 for none
    vec3 q;      // position on the unit sphere
    float a;     // firing, 0..1+
    float vis;   // 0 = dark
    float size;  // radius scale
    float h;     // its own random number
};

// Neuron i: nudged off the perfect lattice so the network reads as grown rather than placed, a
// few big cell bodies among many small ones, and some that stay dark.
Neuron neuron(float i, float n, float spacing) {
    Neuron u;
    u.i = i;
    u.q = vec3(0.0, 0.0, 10.0);
    u.a = 0.0;
    u.vis = 0.0;
    u.size = 0.0;
    u.h = 0.0;
    if (i < 0.0)
        return u;
    vec3 q = sfPoint(i, n);
    vec3 t1 = normalize(cross(q, abs(q.y) < 0.99 ? vec3(0.0, 1.0, 0.0) : vec3(1.0, 0.0, 0.0)));
    vec3 t2 = cross(q, t1);
    u.q = normalize(q + (t1 * (rnd(i, 4u) - 0.5) + t2 * (rnd(i, 5u) - 0.5)) * spacing * NUDGE);
    u.h = rnd(i, 6u);
    u.a = activation(i, u.q);
    float s = rnd(i, 7u);
    u.size = 0.55 + 1.1 * s * s;
    u.vis = smoothstep(u.h * 0.8, u.h * 0.8 + 0.2, assemble) * step(0.16, rnd(i, 8u));
    u.vis *= mix(1.0, step(0.35, rnd(i, 9u)), wError);
    return u;
}

// How bright a neuron is for the current status, its firing included. `facing`: 1 where the
// sphere faces you, 0 at the edge.
float shade(Neuron u, float facing) {
    float lat = asin(clamp(u.q.y, -1.0, 1.0));
    float b = (0.30 + 0.45 * u.h) * dim;
    // Working: latitude bands light up and scroll, like a dot-matrix display.
    float bands = step(0.5, fract(lat * 4.0 / PI - band));
    b = mix(b, b * (0.22 + 1.25 * bands), wWork);
    // Dictating: only the band round the equator stays lit.
    b *= mix(1.0, 0.18 + 0.82 * exp(-lat * lat / 0.05), wDictate);
    // Hearing: the side facing you leans in, a little brighter even before you speak.
    b *= 1.0 + wHear * (0.35 * facing + 0.4 * mic) + wSpeak * voice * 0.6 + wDictate * mic * 0.4;
    // Mic failed: the sagging side dims and slowly brightens again, once every 2 s.
    b *= 1.0 - wFail * smoothstep(0.1, 0.9, u.q.x) * (0.35 + 0.35 * (0.5 + 0.5 * sin(phFail)));
    // Error: a strong, slow throb (under 1 Hz: an alarm, not a flash).
    b *= 1.0 + wError * (0.25 + 0.35 * sin(phAlarm));
    return b + u.a * 1.7;
}

float dotLight(vec3 ho, Neuron u, float spacing, float rd, float aa, float extra, float facing) {
    if (u.vis <= 0.0)
        return 0.0;
    float r = min(rd * u.size * (1.0 + 0.5 * u.a + wSpeak * voice * 0.3), spacing * 0.24);
    float d = length(ho - u.q);
    float body = 1.0 - smoothstep(r - aa, r + aa, d);
    float hr = d / (r * 2.6);
    float halo = exp(-hr * hr) * (0.06 + 0.45 * u.a) * glow
               * (1.0 - smoothstep(REACH * 0.7, REACH, d / spacing));
    return (body + halo) * (shade(u, facing) + extra) * u.vis;
}

// The synapse between two neighbours: a thin line, with a pulse running along it while either
// end fires. Only near neighbours are wired, and not all of them (a network, not a mesh). Each
// line is drawn the same way from both ends (lower index first), so it doesn't change where the
// nearest dot changes.
float link(vec3 ho, Neuron a, Neuron b, float spacing, float rd, float aa) {
    if (a.i < 0.0 || b.i < 0.0 || a.i == b.i || min(a.vis, b.vis) <= 0.0)
        return 0.0;
    if (length(b.q - a.q) > spacing * 1.25)
        return 0.0;
    float lo = min(a.i, b.i);
    float hi = max(a.i, b.i);
    if (rnd2(lo, hi, 10u) < 0.38)
        return 0.0;
    vec3 qa = lo == a.i ? a.q : b.q;
    vec3 qb = lo == a.i ? b.q : a.q;
    vec3 ab = qb - qa;
    float u = clamp(dot(ho - qa, ab) / dot(ab, ab), 0.0, 1.0);
    float ds = length(ho - (qa + ab * u));
    float lw = rd * 0.3;
    float w = max(lw, aa * 0.75);
    float line = (1.0 - smoothstep(w - aa * 0.5, w + aa * 0.5, ds)) * (lw / w);
    if (line <= 0.0)
        return 0.0;
    float act = max(a.a, b.a);
    float pc = fract(phPulse + rnd2(lo, hi, 11u));
    float pu = (u - pc) / 0.12;
    float pulse = exp(-pu * pu);
    float rest = linkAlpha * dim * (0.5 + 0.5 * rnd(hi, 12u));
    return line * (rest + act * (0.35 + 1.8 * pulse)) * min(a.vis, b.vis);
}

// Mic failed: orange rings run out of the core, through the lattice and past it, one a second.
// `r` is the distance from the centre on screen.
float failRings(float r) {
    float glow = 0.0;
    for (int k = 0; k < 2; k++) {
        float ph = fract(phFail / (2.0 * PI) + float(k) * 0.5);
        float rad = mix(CORE * 0.9, 0.98, ph);
        float x = (r - rad) / 0.05;
        glow += exp(-x * x) * (1.0 - ph) * (1.0 - ph) * smoothstep(0.0, 0.08, ph);
    }
    return glow * wFail;
}

// The colour of the lattice at a point: ice white; red in error; orange on a failed mic's
// sagging side and where its rings pass.
vec3 latticeTint(vec3 ho, float r) {
    vec3 tint = mix(ICE, RED, wError);
    float orange = wFail * max(0.35 + smoothstep(0.1, 0.9, ho.x) * 0.6, min(failRings(r) * 2.0, 1.0));
    return mix(tint, ORANGE, clamp(orange, 0.0, 1.0));
}

// The light of one side of the lattice at p: the neurons near it, the synapses between them,
// and the soft light of the cascades passing over.
vec3 lattice(vec2 p, float side, float Rs) {
    vec3 hv, ho;
    float Rh;
    if (!hitShell(p, side, Rs, hv, ho, Rh))
        return vec3(0.0);
    float n = dots;
    float spacing = sqrt(4.0 * PI / n);
    vec4 cands;
    if (sfNearest(ho, n, cands) < 0.0)
        return vec3(0.0);
    // One pixel on the sphere, wider toward the edge where the surface turns away.
    float facing = abs(hv.z) / Rh;
    float aa = px / (Rh * max(facing, 0.16));
    float rd = dotSize * spacing;
    Neuron n0 = neuron(cands.x, n, spacing);
    Neuron n1 = neuron(cands.y, n, spacing);
    Neuron n2 = neuron(cands.z, n, spacing);
    Neuron n3 = neuron(cands.w, n, spacing);
    // Loading: a scan sweeps up and down, lighting the dots it passes.
    float sc = (hv.y - cos(phScan) * Rs) / 0.035;
    float scan = wLoad * exp(-sc * sc) * 1.2;
    float light = dotLight(ho, n0, spacing, rd, aa, scan, facing) + dotLight(ho, n1, spacing, rd, aa, scan, facing)
                + dotLight(ho, n2, spacing, rd, aa, scan, facing) + dotLight(ho, n3, spacing, rd, aa, scan, facing);
    if (linkAlpha > 0.0 || fire > 0.0) {
        light += link(ho, n0, n1, spacing, rd, aa) + link(ho, n0, n2, spacing, rd, aa)
               + link(ho, n0, n3, spacing, rd, aa) + link(ho, n1, n2, spacing, rd, aa)
               + link(ho, n1, n3, spacing, rd, aa) + link(ho, n2, n3, spacing, rd, aa);
    }
    // The cascades' own soft light, drawn per pixel rather than per dot so it is never cut off.
    if (fire > 0.0) {
        float wv = max(wave(ho, c0, taus.x), max(wave(ho, c1, taus.y), wave(ho, c2, taus.z)));
        light += wv * fire * glow * 0.16 * dim;
    }
    // A failed mic's rings light the dots they pass.
    light *= 1.0 + failRings(length(p)) * 1.8;
    return light * latticeTint(ho, length(p));
}

// The glass core: clear, with a thin bright rim split into colour at the very edge, a sharp
// highlight and a soft reflection, and inside a small hot nucleus with swirling filaments.
// Asleep the nucleus breathes; dictating it blinks like a text cursor.
vec4 core(vec2 p) {
    float r = length(p);
    float cover = 1.0 - smoothstep(CORE - px, CORE + px, r);
    if (cover <= 0.0)
        return vec4(0.0);
    vec3 nn = vec3(p / CORE, sqrt(max(0.0, 1.0 - r * r / (CORE * CORE))));
    float f = 1.0 - nn.z;
    vec3 rimC = vec3(pow(f, 4.0 + dispersion * 2.0), pow(f, 3.6), pow(f, 3.2 - dispersion * 2.0));  // a cool fringe outermost
    float spec = pow(max(dot(nn, normalize(vec3(-0.45, 0.55, 0.7))), 0.0), 80.0);
    float soft = smoothstep(0.55, 0.95, dot(nn, normalize(vec3(-0.5, 0.6, 0.62)))) * 0.16;
    vec3 rn = rotY(coreAng.x) * rotX(coreAng.y) * nn;
    float fil = abs(sin(rn.x * 5.0 + sin(rn.y * 4.0 + coreAng.z) * 1.8 + rn.z * 3.0));
    fil = pow(1.0 - fil, 6.0) * nn.z;
    float nucleus = exp(-r * r / (CORE * CORE * 0.10));
    float blink = smoothstep(0.0, 0.06, phBlink) * (1.0 - smoothstep(0.5, 0.56, phBlink));
    nucleus *= mix(1.0, 0.2 + 1.3 * blink, wDictate) * (1.0 + wSleep * 0.9 * sin(phBreath));
    float inner = coreGlow * (nucleus * 0.9 + fil * 0.35) * (1.0 + wError * 0.6 * sin(phAlarm));
    // The light inside takes the alarm's colour; the glass rim stays glass.
    vec3 innerC = mix(mix(vec3(0.92, 0.96, 1.0), RED, wError), ORANGE, wFail * 0.9);
    vec3 c = rimC * 0.9 + vec3(spec + soft) + innerC * inner;
    float a = clamp(0.03 + pow(f, 3.0) * 0.85 + spec + soft + inner * 0.6, 0.0, 1.0);
    return vec4(c, a) * cover * dim;
}

// A ring of dots round the centre: normal nrm, radius rr, `count` dots, a comet running round it.
float ring(vec2 p, vec3 nrm, float rr, float count, float phase, float comet) {
    vec3 e1 = normalize(abs(nrm.y) < 0.9 ? cross(nrm, vec3(0.0, 1.0, 0.0)) : cross(nrm, vec3(1.0, 0.0, 0.0)));
    vec3 e2 = cross(nrm, e1);
    mat2 M = mat2(e1.x, e1.y, e2.x, e2.y) * rr;
    float det = M[0][0] * M[1][1] - M[1][0] * M[0][1];
    if (abs(det) < 1e-4)
        return 0.0;
    mat2 Mi = inverse(M);
    vec2 cs = Mi * p;
    float l = length(cs);
    vec2 g = transpose(Mi) * (cs / max(l, 1e-4));
    float dist = (l - 1.0) / max(length(g), 1e-4);
    if (abs(dist) > px * 8.0)
        return 0.0;
    float th = atan(cs.y, cs.x);
    float cellW = 2.0 * PI / count;
    float best = 1e3;
    float zAt = 0.0;
    for (int s = -1; s <= 1; s++) {
        float tc = (floor((th + phase) / cellW) + 0.5 + float(s)) * cellW - phase;
        vec3 P = rr * (cos(tc) * e1 + sin(tc) * e2);
        float dd = length(p - P.xy);
        if (dd < best) {
            best = dd;
            zAt = P.z;
        }
    }
    float dotL = 1.0 - smoothstep(px * 0.9, px * 2.1, best);
    float line = (1.0 - smoothstep(px * 0.4, px * 1.4, abs(dist))) * 0.12;
    float ec = fract((th - comet) / (2.0 * PI));
    float head = exp(-(1.0 - ec) * 14.0) * 1.6;
    float behind = zAt < 0.0 ? (length(p) < CORE ? 0.18 : 0.38) : 1.0;
    return (dotL * (0.55 + head) + line) * behind;
}

void main() {
    vec2 p = (qt_TexCoord0 * 2.0 - 1.0) * vec2(1.0, -1.0);
    float r = length(p);
    Vt = transpose(rotX(0.38 + tiltY) * rotY(spin + tiltX));
    float Rs = mix(0.20, 0.56, assemble) * shell;

    vec3 col = vec3(0.0);
    float alpha = 0.0;

    if (r < Rs * 1.25) {
        // The far side, seen through the core, which magnifies it a little.
        float inCore = 1.0 - smoothstep(CORE - px, CORE + px, r);
        vec2 pb = p * (1.0 - 0.16 * inCore * (1.0 - r / CORE));
        vec3 back = lattice(pb, -1.0, Rs) * mix(0.22, 0.16, inCore);
        col += back;
        alpha += max(back.r, max(back.g, back.b));

        vec4 c = core(p);
        col = col * (1.0 - c.a * 0.5) + c.rgb;
        alpha = alpha * (1.0 - c.a) + c.a;

        vec3 front = lattice(p, 1.0, Rs);
        col += front;
        alpha += max(front.r, max(front.g, front.b));
    }

    // Rings: wide and faint at rest; tight, bright gyroscopes while working.
    if (ringAlpha > 0.0) {
        float rl = 0.0;
        for (int k = 0; k < 3; k++) {
            float fk = float(k);
            float az = k == 0 ? ringZ.x : (k == 1 ? ringZ.y : ringZ.z);
            float ax = k == 0 ? ringX.x : (k == 1 ? ringX.y : ringX.z);
            float ac = k == 0 ? ringC.x : (k == 1 ? ringC.y : ringC.z);
            vec3 nrm = rotZ(0.6 + fk * 1.1 + az * ringTight) * rotX(1.2 - fk * 0.5 + ax) * vec3(0.0, 1.0, 0.0);
            float rr = mix(0.70 + fk * 0.07, 0.32 + fk * 0.05, ringTight) * mix(1.0, shell, 0.5);
            rl += ring(p, normalize(nrm), rr, mix(96.0, 64.0, ringTight) + fk * 12.0, ringP, ac);
        }
        rl *= ringAlpha * dim;
        col += mix(ICE, RED, wError) * rl;
        alpha += rl;
    }

    // Halo round the lattice and the core's bloom, in the alarm's colour when there is one.
    vec3 glowC = mix(mix(ICE, RED, wError), ORANGE, wFail * 0.85);
    float hd = (r - Rs) / 0.13;
    float halo = glow * dim * (0.06 + 0.10 * fire + 0.12 * wSpeak * voice) * exp(-hd * hd);
    float bloom = coreGlow * dim * 0.12 * exp(-r * r / (CORE * CORE * 0.6));
    // Error: a red glow round the whole orb, throbbing with it.
    float he = (r - Rs) / 0.2;
    float alarm = wError * dim * 0.22 * (0.65 + 0.35 * sin(phAlarm)) * exp(-he * he);
    // Mic failed: the rings carry on past the lattice, fading as they go.
    float rings = failRings(r) * (r > Rs ? 0.9 : 0.35);
    col += glowC * (halo + bloom) + RED * alarm + ORANGE * rings;
    alpha += (halo + bloom + alarm + rings) * 0.6;

    alpha = clamp(alpha, 0.0, 1.0);
    fragColor = vec4(col, alpha) * qt_Opacity;
}
