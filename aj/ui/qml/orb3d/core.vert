// The glass core is drawn on a camera-facing square; vUV is in core radii.

VARYING vec2 vUV;

void MAIN()
{
    vUV = VERTEX.xy / 50.0 * 1.4;
    POSITION = MODELVIEWPROJECTION_MATRIX * vec4(VERTEX, 1.0);
}
