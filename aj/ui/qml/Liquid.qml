pragma Singleton
import QtQuick

// What the pointer does to the background: where it is and its smoothed velocity, which drag the
// dot grid round it into a warp (common.glsl, `liquid`). The background and every glass surface
// read these, so the glass bends the same warp the background shows. Also where the orb's light
// falls. (The rings of water it used to drop are gone, 2026-10-23: the warp stays.)
QtObject {
    id: liquid

    property point ptr: Qt.point(-10000, -10000)
    property point vel: Qt.point(0, 0)  // px/s, smoothed
    // The liquid's own clock: it runs only while the warp moves. When it stops, nothing the
    // background and the glass draw from changes, so Qt keeps their cached textures and they
    // cost nothing (see SceneEffect).
    property real clock: 0
    property bool active: false
    property point orbC: Qt.point(220, 236)
    property real orbR: 130
    property real orbE: 0.5
    property vector3d orbTint: Qt.vector3d(0.32, 0.20, 0.17)

    signal layoutChanged  // glass surfaces re-measure where they are in the window

    property real _lastX: 0
    property real _lastY: 0
    property real _lastMs: -1
    property real _rawVx: 0
    property real _rawVy: 0
    property real _movedAt: 0

    function moved(x, y) {
        // Qt re-sends hover every frame while anything animates, even with the mouse still;
        // only a real move may wake the liquid (else the background redraws every frame).
        if (x === _lastX && y === _lastY && _lastMs >= 0)
            return
        const ms = Date.now()
        if (_lastMs >= 0) {
            const dt = Math.max((ms - _lastMs) / 1000, 0.004)
            let vx = (x - _lastX) / dt
            let vy = (y - _lastY) / dt
            const sp = Math.hypot(vx, vy)
            if (sp > 2500) {
                vx *= 2500 / sp
                vy *= 2500 / sp
            }
            _rawVx = vx
            _rawVy = vy
        }
        _lastX = x
        _lastY = y
        _lastMs = ms
        _movedAt = Motion.now
        ptr = Qt.point(x, y)
    }

    function left() {
        _lastMs = -1
        _rawVx = 0
        _rawVy = 0
        ptr = Qt.point(-10000, -10000)  // the orb turns back to face you
    }

    property Connections _clock: Connections {
        target: Motion
        function onTick(dt) {
            if (Motion.now - liquid._movedAt > 0.05) {  // no move events: the pointer has stopped
                liquid._rawVx = 0
                liquid._rawVy = 0
            }
            const a = 1 - Math.exp(-dt * 12)
            const vx = liquid.vel.x + (liquid._rawVx - liquid.vel.x) * a
            const vy = liquid.vel.y + (liquid._rawVy - liquid.vel.y) * a
            const settled = Math.abs(vx) < 0.5 && Math.abs(vy) < 0.5
            if (!settled || liquid.vel.x !== 0 || liquid.vel.y !== 0)
                liquid.vel = settled || Theme.reducedMotion ? Qt.point(0, 0) : Qt.point(vx, vy)
            const moving = Motion.now - liquid._movedAt < 0.12
            const flowing = liquid.vel.x !== 0 || liquid.vel.y !== 0
            liquid.active = moving || flowing
            if (liquid.active)
                liquid.clock += dt
        }
    }
}
