// A clear glass sphere, worked out per pixel so its edge stays smooth: a fresnel rim, a
// faint rainbow fringe just inside it, a highlight at the top left and a soft inner glow
// that turns with coreSpin. White and clear only, added to what is behind it.

VARYING vec2 vUV;

vec3 rotY(vec3 p, float a)
{
    float c = cos(a), s = sin(a);
    return vec3(c * p.x + s * p.z, p.y, -s * p.x + c * p.z);
}

void MAIN()
{
    float r = length(vUV);
    float aa = fwidth(r);
    float inside = 1.0 - smoothstep(1.0 - aa, 1.0 + aa, r);
    vec3 N = vec3(vUV, sqrt(max(0.0, 1.0 - r * r)));
    float ndv = N.z;

    vec3 col = vec3(0.86, 0.91, 1.0) * pow(1.0 - ndv, 2.6) * 0.75;
    float ring = smoothstep(0.78, 0.92, r) * (1.0 - smoothstep(0.94, 1.0, r));
    vec3 hue = 0.5 + 0.5 * cos(6.2832 * (r * 2.5 + vec3(0.0, 0.33, 0.67)));
    col += hue * ring * 0.05;

    vec3 H = normalize(normalize(vec3(-0.5, 0.6, 0.62)) + vec3(0.0, 0.0, 1.0));
    float nh = max(dot(N, H), 0.0);
    col += vec3(1.0) * (pow(nh, 90.0) * 1.3 + pow(nh, 14.0) * 0.08);
    float nb = max(dot(N, normalize(vec3(0.55, -0.6, 0.6))), 0.0);
    col += vec3(0.8, 0.88, 1.0) * pow(nb, 30.0) * 0.12;

    vec3 q = rotY(vec3(vUV, ndv), coreSpin);
    float swirl = 0.55 + 0.45 * sin(q.x * 4.0 + q.y * 3.0 + coreSpin * 0.4) * sin(q.z * 3.5 - q.y * 2.5);
    col += vec3(0.88, 0.93, 1.0) * coreGlow * (0.10 + 0.22 * swirl) * ndv * ndv;
    col += vec3(0.9, 0.95, 1.0) * coreGlow * exp(-r * r * 4.0) * 0.18;
    col *= inside;

    // A faint halo just outside the glass.
    float out1 = max(r - 1.0, 0.0);
    col += vec3(0.85, 0.9, 1.0) * exp(-out1 * 9.0) * (1.0 - inside) * 0.05 * (0.4 + coreGlow) * glow;

    col *= mix(0.5, 1.0, dim) * mix(0.6, 1.0, assemble);
    FRAGCOLOR = vec4(col, 0.0);
}
