#version 440
// The window's background: a dull ember field that liquefies under the pointer and settles back.

layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;

layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
#include "scene_uniforms.glsl"
};

#include "common.glsl"

void main() {
    vec2 p = qt_TexCoord0 * win;
    vec3 liq = liquid(p);
    vec3 col = scene(p + liq.xy, 1.0);
    col += vec3(1.0, 0.92, 0.88) * liquidLight(liq.xy);
    // Dither: the dark gradients would band at 8 bits per channel.
    col += (hash2(gl_FragCoord.xy) - 0.5) * (1.5 / 255.0);
    fragColor = vec4(max(col, 0.0), 1.0) * qt_Opacity;
}
