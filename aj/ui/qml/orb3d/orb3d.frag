// Dots: a crisp disc antialiased over one device pixel, with a soft halo scaled by glow.
// Links: a line ~1 px wide, antialiased across. Alpha stays 0 so the light is added to
// whatever is behind the orb.

VARYING vec2 vCorner;
VARYING vec3 vLight;
VARYING float vPx;
VARYING float vKind;

void MAIN()
{
    float cover;
    if (vKind > 1.5) {
        float d = abs(vCorner.y) * vPx;   // px from the line's centre
        cover = clamp(1.05 - d, 0.0, 1.0);
    } else {
        float u = length(vCorner) * 3.0;  // in dot radii (the quad reaches 3)
        float disc = clamp((1.0 - u) * vPx + 0.5, 0.0, 1.0);
        float halo = (exp(-u * u * 0.9) * 0.22 + exp(-u * 1.8) * 0.06) * glow;
        cover = disc + halo;
    }
    FRAGCOLOR = vec4(vLight * cover, 0.0);
}
