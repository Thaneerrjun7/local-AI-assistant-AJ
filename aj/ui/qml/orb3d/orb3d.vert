// Lattice dots, their synapse links and the ring dots, told apart by UV1.x (0, 2 and 1).
// All the motion is worked out here from each dot's unit lattice position, so the mesh is
// built once. The camera is orthographic and axis-aligned: object x and y are screen x and
// y (one unit is half the view) and z points at the viewer.

VARYING vec2 vCorner;   // dots: where in the quad, -1..1; links: (0, side)
VARYING vec3 vLight;    // the light this vertex adds
VARYING float vPx;      // dot radius, or the link quad's half-width, in device px
VARYING float vKind;

const float PI = 3.14159265359;
const float HALO = 3.0;                     // dot quads reach this many dot radii, for the halo
const vec3 ICE = vec3(0.957, 0.973, 1.0);   // #F4F8FF

uint pcg(uint v)
{
    uint s = v * 747796405u + 2891336453u;
    uint w = ((s >> ((s >> 28u) + 4u)) ^ s) * 277803737u;
    return (w >> 22u) ^ w;
}

float hash(float x)
{
    return float(pcg(uint(int(floor(x))))) * (1.0 / 4294967295.0);
}

vec3 rotX(vec3 p, float a)
{
    float c = cos(a), s = sin(a);
    return vec3(p.x, c * p.y - s * p.z, s * p.y + c * p.z);
}

vec3 rotY(vec3 p, float a)
{
    float c = cos(a), s = sin(a);
    return vec3(c * p.x + s * p.z, p.y, -s * p.x + c * p.z);
}

vec3 rotZ(vec3 p, float a)
{
    float c = cos(a), s = sin(a);
    return vec3(c * p.x - s * p.y, s * p.x + c * p.y, p.z);
}

vec3 orient(vec3 p)
{
    return rotX(rotY(p, spin + tiltX), 0.38 + tiltY);
}

float radius()
{
    return mix(0.20, 0.56 * shell, assemble);
}

// How strongly neuron i at unit position q is firing: three cascade fronts that sweep out
// from random points, plus neurons that spark on their own.
float firing(vec3 q, float i)
{
    float a = 0.0;
    for (int s = 0; s < 3; s++) {
        float fs = flow * 0.55 + float(s) / 3.0;
        float ep = floor(fs);
        float tau = fract(fs);
        float seed = ep * 3.0 + float(s);
        vec3 o = normalize(vec3(hash(seed * 7.0 + 1.0), hash(seed * 7.0 + 2.0),
                                hash(seed * 7.0 + 3.0)) - 0.5);
        float ang = acos(clamp(dot(q, o), -1.0, 1.0));
        float x = (ang - tau * 3.4) / 0.22;
        float gate = step(0.42, hash(i * 1.37 + seed * 101.0));
        a = max(a, exp(-x * x) * (1.0 - tau) * gate);
    }
    a *= fire;
    float sp = flow * 1.7 + hash(i) * 13.0;
    float on = step(1.0 - spark, hash(i * 3.1 + floor(sp) * 17.0)) * step(1e-5, spark);
    return max(a, on * exp(-fract(sp) * 5.0));
}

// Where lattice dot i (unit position q, hash h) is drawn, how bright, and how active.
vec3 place(vec3 q, float i, float h, out float light, out float act)
{
    float R = radius();
    float lat = asin(clamp(q.y, -1.0, 1.0));
    float lon = atan(q.z, q.x);
    vec3 d = orient(q);
    float angV = acos(clamp(d.z, -1.0, 1.0));   // from the point facing the viewer

    // The voice-driven part: zero when voice and mic are, so the orb is still then.
    float dyn = wSpeak * voice * (0.035 + 0.075 * (0.5 + 0.5 * sin(abs(lat) * 9.0 - flow * 7.0))
                * (0.6 + 0.4 * (0.5 + 0.5 * sin(lon * 3.0 + flow * 1.3))));
    dyn += wHear * mic * 0.07 * sin(angV * 7.0 - t * 10.0) * exp(-angV * 0.9);
    dyn += wDictate * mic * 0.10 * exp(-lat * lat / 0.03) * (0.5 + 0.5 * sin(lon * 12.0 - t * 7.0));
    float del = dyn + wSleep * 0.018 * sin(t * 2.0 * PI / 6.0);
    del -= wFail * 0.09 * smoothstep(0.1, 0.9, q.x);
    del += wError * 0.06;

    // Error drifts apart: mostly outward, a little sideways.
    vec3 jit = vec3(hash(i * 5.0 + 1.0), hash(i * 5.0 + 2.0), hash(i * 5.0 + 3.0)) - 0.5;
    float drift = dispersion * 0.22 * hash(i * 5.0 + 4.0);
    vec3 p = orient(q * (1.0 + del + drift) + jit * (dispersion * 0.08)) * R;
    // Mic failed: the loose side also sags a little.
    p.y -= wFail * smoothstep(0.1, 0.9, q.x) * 0.05 * hash(i * 9.0 + 5.0);

    act = firing(q, i);
    light = (0.45 + 0.35 * h) * dim + 1.6 * act;
    // Seen head-on the push is along the view, so it also shows as light: dots pushed out
    // brighten. Without this the hearing rings are invisible on the front.
    light *= clamp(1.0 + 6.0 * dyn, 0.45, 1.8);
    act = max(act, dyn * 4.0);

    // Working: alternating latitude bands that scroll with band.
    float stripe = clamp(sin((lat / PI * 5.0 + band) * 2.0 * PI) * 4.0, -1.0, 1.0) * 0.5 + 0.5;
    light *= mix(1.0, mix(0.12, 1.9, stripe), wWork);
    // Loading: a scan band sweeping up and down, lighting the dots it passes.
    float sy = (p.y - cos(t * 1.6) * R) / 0.045;
    float scan = exp(-sy * sy);
    light += wLoad * 1.5 * scan;
    act = max(act, wLoad * scan * 0.8);
    // Dictating: dots away from the equator dim.
    light *= mix(1.0, 0.1 + 0.9 * exp(-lat * lat / 0.05), wDictate);
    // Error: about a third go dark.
    light *= 1.0 - wError * 0.95 * step(hash(i * 7.0 + 3.0), 0.35);
    // Mic failed: the +x side flickers.
    light *= 1.0 - wFail * smoothstep(0.1, 0.9, q.x) * 0.8
                 * step(0.5, hash(i * 2.0 + floor(t * 12.0) * 31.0));
    // The far side is dimmer, and dimmer again seen through the glass core.
    light *= mix(0.3, 1.0, smoothstep(-0.4, 0.4, p.z / max(R, 1e-3)));
    light *= 1.0 - 0.35 * step(p.z, 0.0) * (1.0 - smoothstep(0.20, 0.24, length(p.xy)));
    // Dots appear in hash order as the lattice assembles.
    float order = hash(i * 1.91 + 7.0);
    light *= smoothstep(order, order + 0.06, assemble * 1.06);
    return p;
}

// Ring k's dot at u (0..1 round it). Wide ambient rings at fixed tilts mix into three
// gyroscope rings tight round the core, each turning on its own axis.
vec3 ringDot(float k, float u, out float light, out float r)
{
    float dir = mod(k, 2.0) < 0.5 ? 1.0 : -1.0;
    float th = u * 2.0 * PI + ringSpin * (0.5 + 0.2 * k) * dir;
    vec3 c = vec3(cos(th), 0.0, sin(th));
    float A = ringSpin;
    vec3 wide = k < 0.5 ? vec3(1.30, 0.35, 0.0)
              : (k < 1.5 ? vec3(1.10, 2.40, 0.15) : vec3(1.45, -1.05, -0.1));
    vec3 gyro = k < 0.5 ? vec3(A * 1.1, 0.0, 0.0)
              : (k < 1.5 ? vec3(PI * 0.5, A * 1.3, 0.0) : vec3(PI * 0.5 + A * 0.9, 0.9, 0.0));
    vec3 ang = mix(wide + vec3(0.0, spin * 0.25, 0.0), gyro, ringTight);
    float rr = mix(0.80 + 0.07 * k, 0.29 + 0.035 * k, ringTight) * mix(0.4, 1.0, assemble);
    vec3 p = rotX(rotZ(rotY(rotX(c, ang.x), ang.y), ang.z), 0.38 + tiltY) * rr;

    float ph = fract(u * 2.0 - ringSpin * 0.2 - k * 0.37) - 0.5;
    float pulse = exp(-ph * ph * 36.0);
    light = ringAlpha * (0.45 + 1.4 * pulse) * mix(0.3, 1.0, smoothstep(-0.5, 0.5, p.z / rr))
          * smoothstep(0.5, 1.0, assemble);
    r = (0.0055 + 0.0035 * pulse) * mix(0.8, 1.0, ringTight);
    return p;
}

void MAIN()
{
    vKind = UV1.x;
    vCorner = UV0;
    float light;
    vec3 p;
    if (UV1.x < 0.5) {
        float act;
        p = place(VERTEX, COLOR.x, COLOR.y, light, act);
        float r = dotSize * sqrt(4.0 * PI / max(dots, 1.0)) * radius() * (1.0 + 0.7 * act);
        r = max(r, 0.9 / pxPerUnit);
        p.xy += UV0 * r * HALO;
        vPx = r * pxPerUnit;
    } else if (UV1.x < 1.5) {
        float r;
        p = ringDot(COLOR.x, COLOR.y, light, r);
        r = max(r, 0.9 / pxPerUnit);
        p.xy += UV0 * r * HALO;
        vPx = r * pxPerUnit;
    } else {
        // A synapse link: this end and the far end are both placed, then the vertex is
        // pushed sideways so the quad is a line ~1 px wide with 1 px either side for AA.
        float la, aa, lb, ab;
        p = place(VERTEX, COLOR.x, COLOR.y, la, aa);
        vec3 pb = place(NORMAL, COLOR.z, COLOR.w, lb, ab);
        vec2 along = pb.xy - p.xy;
        vec2 n = vec2(-along.y, along.x) / max(length(along), 1e-6);
        vPx = 1.6;
        p.xy += n * UV0.y * (vPx / pxPerUnit);
        light = linkAlpha * (la * 0.6 + aa * 1.2) * clamp(lb * 3.0, 0.0, 1.0);
    }
    vLight = ICE * light;
    POSITION = MODELVIEWPROJECTION_MATRIX * vec4(p, 1.0);
}
