import QtQuick
import QtQuick.Window

// A shader that draws the background (scene_uniforms.glsl): the background itself, and every
// glass surface, which redraws the background behind it bent. All of them bind the same values.
//
// Each one renders into a cached texture (layer), redrawn only when one of its values changes.
// What it draws from changes only while the pointer moves, so a still window costs only the
// orb. Measured 2026-10-23: drawing the background and glass afresh every frame was ~6 ms of
// the 6.5 ms a frame took on the Radeon 780M; the orb was ~0.4 ms.
//
// `still`: draw as if the pointer weren't moving, and don't follow it at all (reduced motion; the
// glass also when it is solid), so the pointer moving can't make it redraw for nothing.
ShaderEffect {
    id: effect

    property bool still: Theme.reducedMotion

    layer.enabled: !Theme.noCache && !Theme.software
    layer.smooth: true  // an item at a fractional pixel position would otherwise double its edges
    visible: Theme.hidden.indexOf(objectName) < 0
    property size win: Qt.size(Window.width, Window.height)
    property real t: still ? 0 : Liquid.clock
    property point ptr: still ? Qt.point(-10000, -10000) : Liquid.ptr
    property point vel: still ? Qt.point(0, 0) : Liquid.vel
    property real smear: still ? 0 : Theme.smear
    property real shine: Theme.shine
    property real grid: Theme.grid
    property real warmth: Theme.warmth
    property point orbC: Liquid.orbC
    property real orbR: Liquid.orbR
    property real orbE: Liquid.orbE
    property vector3d orbTint: Liquid.orbTint
}
