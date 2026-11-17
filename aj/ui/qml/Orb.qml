import QtQuick
import QtQuick.Window
import "OrbStates.js" as States

// The orb (shaders/orb.frag), driven by `mode` (the status) and two levels: AJ's voice and yours.
// Each status is a set of target values (OrbStates.js); every value is a spring, so a status
// change part way through an animation morphs into the next. The clocks that run at a status's
// own speed (spin, the firing clock, the core's swirl, the rings) are integrated here per frame,
// so a change of speed never jumps the animation, and wrapped here in 64-bit JavaScript before
// they reach the shader, so a session of days loses no precision.
ShaderEffect {
    id: orb

    property string mode: "loading"
    property real voiceLevel: 0  // 0..1, AJ's voice
    property real micLevel: 0    // 0..1, yours

    // ---- uniforms (orb.frag) ----
    property real px: 2 / Math.max(width * Screen.devicePixelRatio, 1)
    property real spin: 0
    property real tiltX: sTiltX.value
    property real tiltY: sTiltY.value
    property real band: 0
    property real phSpark: 0
    property real phPulse: 0
    property real phWave: 0
    property real phLobe: 0
    property real phHear: 0
    property real phDict: 0
    property real phBreath: 0
    property real phScan: 0
    property real phFail: 0
    property real phBlink: 0
    property real phAlarm: 0
    property vector3d coreAng: Qt.vector3d(0, 0, 0)
    property vector3d ringZ: Qt.vector3d(0, 0, 0)
    property vector3d ringX: Qt.vector3d(0, 0, 0)
    property vector3d ringC: Qt.vector3d(0, 0, 0)
    property real ringP: 0
    property real dots: Math.round(Theme.dots)
    property real dotSize: Theme.dotSize * sDotScale.value
    property real dim: sDim.value
    property real linkAlpha: sLink.value * Theme.links
    property real fire: sFire.value * Theme.fire
    property real spark: Math.min(sSpark.value * Theme.sparks, 1)
    property real glow: sGlow.value * Theme.glow
    property real shell: sShell.value
    property real assemble: sAssemble.value
    property real coreGlow: sCore.value * Theme.core
    property real voice: _voiceS * (Theme.reducedMotion ? 0.3 : 1)
    property real mic: _micS * (Theme.reducedMotion ? 0.3 : 1)
    // The levels arrive 30 times a second (window.py); smoothed here every frame so the
    // membrane glides between readings, and exactly 0 again in a silence, so the orb rests.
    property real _voiceS: 0
    property real _micS: 0
    function _glide(from, to, dt) {
        const next = from + (to - from) * (1 - Math.exp(-dt / 0.045))
        return to <= 0 && next < 0.002 ? 0 : next
    }
    property real wSpeak: sSpeak.value
    property real wHear: sHear.value
    property real wDictate: sDictate.value
    property real wWork: sWork.value
    property real wLoad: sLoad.value
    property real wFail: sFail.value
    property real wError: sError.value
    property real wSleep: sSleep.value
    property real ringAlpha: sRingA.value * Theme.rings
    property real ringTight: sRingT.value
    property real dispersion: Theme.dispersionOrb
    // The three firing cascades: where each starts (a random point on the sphere, new each
    // time one dies out) and how far through its life it is. The same for every pixel, so
    // worked out here once a frame instead of in the shader.
    property vector4d c0: Qt.vector4d(0, 1, 0, 0)
    property vector4d c1: Qt.vector4d(1, 0, 0, 1)
    property vector4d c2: Qt.vector4d(0, 0, 1, 2)
    property vector3d taus: Qt.vector3d(0, 0.33, 0.66)

    fragmentShader: "../shaders/orb.frag.qsb"

    // ---- the clocks, in JavaScript doubles ----
    property real _flow: 0  // the firing clock
    property real _t: 0     // runs only for the statuses that move with time
    property var _core: [0, 0, 0]
    property var _ring: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]  // three turn-about-z, three turn-about-x, three comets, the dot pattern
    property var _epochs: [-1, -1, -1]

    readonly property real _tau: 2 * Math.PI
    function _wrap(x, period) { return x - Math.floor(x / period) * period }

    function _advance(dt) {
        const k = dt * Theme.speed * (Theme.reducedMotion ? 0.15 : 1)
        // Speaking turns and fires only with the voice: in a silence it is as still as listening.
        _voiceS = _glide(_voiceS, voiceLevel, dt)
        _micS = _glide(_micS, micLevel, dt)
        const talk = sSpeak.value * _voiceS
        spin = _wrap(spin + (sSpinRate.value + talk * 0.15) * k, _tau)
        _flow += (sFlowRate.value + talk * 0.9) * k
        if (sHear.value + sDictate.value + sLoad.value + sFail.value + sSleep.value + sError.value > 0.001)
            _t += dt * (Theme.reducedMotion ? 0.15 : 1)
        const cr = sCoreRate.value * k
        _core = [_wrap(_core[0] + cr, _tau), _wrap(_core[1] + cr * 0.37, _tau), _wrap(_core[2] + cr * 0.7, _tau)]
        const rr = sRingRate.value * k
        const speeds = [0.2, 0.35, 0.5, 0.3, 0.2, 0.1, 1.0, 1.4, 1.8, 0.2]
        _ring = _ring.map((a, i) => _wrap(a + rr * speeds[i], _tau))
        band = _wrap(band + sWork.value * 0.5 * k, 1)

        phSpark = _wrap(_flow * 1.7, 4096)
        phPulse = _wrap(_flow * 1.5, 1)
        phWave = _wrap(_flow * 7, _tau)
        phLobe = _wrap(_flow * 1.3, _tau)
        phHear = _wrap(_t * 10, _tau)
        phDict = _wrap(_t * 7, _tau)
        phBreath = _wrap(_t * _tau / 6, _tau)
        phScan = _wrap(_t * 1.6, _tau)
        phFail = _wrap(_t * Math.PI, _tau)
        phBlink = _wrap(_t * 1.1, 1)
        phAlarm = _wrap(_t * _tau * 0.8, _tau)
        coreAng = Qt.vector3d(_core[0], _core[1], _core[2])
        ringZ = Qt.vector3d(_ring[0], _ring[1], _ring[2])
        ringX = Qt.vector3d(_ring[3], _ring[4], _ring[5])
        ringC = Qt.vector3d(_ring[6], _ring[7], _ring[8])
        ringP = _ring[9]
        _cascades()
    }

    function _cascades() {
        const tau = [0, 0, 0]
        for (let s = 0; s < 3; s++) {
            const fs = _flow * 0.55 + s / 3
            const ep = Math.floor(fs)
            tau[s] = fs - ep
            if (ep !== _epochs[s]) {
                _epochs[s] = ep
                const z = Math.random() * 2 - 1
                const a = Math.random() * _tau
                const r = Math.sqrt(1 - z * z)
                orb["c" + s] = Qt.vector4d(r * Math.cos(a), z, r * Math.sin(a), (ep * 3 + s) % 4096)
            }
        }
        taus = Qt.vector3d(tau[0], tau[1], tau[2])
    }

    readonly property real _morph: 0.55  // s: how long a status takes to blend into the next

    Spring { id: sShell; response: orb._morph }
    Spring { id: sDim; response: orb._morph }
    Spring { id: sDotScale; response: orb._morph }
    Spring { id: sFire; response: 0.4 }
    Spring { id: sSpark; response: 0.4 }
    Spring { id: sLink; response: orb._morph }
    Spring { id: sSpinRate; response: 0.9 }
    Spring { id: sFlowRate; response: 0.6 }
    Spring { id: sCore; response: 0.45 }
    Spring { id: sCoreRate; response: 0.8 }
    Spring { id: sRingA; response: orb._morph }
    Spring { id: sRingT; response: 0.7; damping: 0.82 }
    Spring { id: sRingRate; response: 0.8 }
    Spring { id: sGlow; response: orb._morph }
    Spring { id: sAssemble; response: 1.4 }
    Spring { id: sSpeak; response: 0.35 }
    Spring { id: sHear; response: 0.35 }
    Spring { id: sDictate; response: orb._morph }
    Spring { id: sWork; response: 0.45 }
    Spring { id: sLoad; response: orb._morph }
    Spring { id: sFail; response: 0.8 }
    Spring { id: sError; response: 0.8 }
    Spring { id: sSleep; response: 1.2 }
    // A gentle turn toward the pointer, up to ~8 degrees; none with reduced motion.
    Spring {
        id: sTiltX
        target: Theme.reducedMotion || Math.abs(Liquid.ptr.x) > 5000 ? 0
                : Math.max(-1, Math.min(1, (Liquid.ptr.x - Liquid.orbC.x) / 420)) * 0.14
        response: 0.6
    }
    Spring {
        id: sTiltY
        target: Theme.reducedMotion || Math.abs(Liquid.ptr.y) > 5000 ? 0
                : Math.max(-1, Math.min(1, (Liquid.ptr.y - Liquid.orbC.y) / 420)) * 0.14
        response: 0.6
    }

    function apply(snap) {
        const g = States.targets(mode)
        const pairs = [[sShell, g.shell], [sDim, g.dim], [sDotScale, g.dotScale], [sFire, g.fire],
                       [sSpark, g.spark], [sLink, g.linkAlpha], [sSpinRate, g.spinRate],
                       [sFlowRate, g.flowRate], [sCore, g.coreGlow], [sCoreRate, g.coreRate],
                       [sRingA, g.ringAlpha], [sRingT, g.ringTight], [sRingRate, g.ringRate],
                       [sGlow, g.glow], [sSpeak, g.wSpeak], [sHear, g.wHear], [sDictate, g.wDictate],
                       [sWork, g.wWork], [sLoad, g.wLoad], [sFail, g.wFail], [sError, g.wError],
                       [sSleep, g.wSleep]]
        for (const [s, v] of pairs) {
            if (snap)
                s.snap(v)
            else
                s.target = v
        }
        // Loading assembles out of the core; turning off goes back in the same way, a little
        // faster. Loading again from a whole lattice first pulls it in quickly (not in one
        // frame: everything here moves from where it is).
        reassemble.stop()
        if (mode === "loading" && snap) {
            sAssemble.snap(0)
        } else if (mode === "loading" && sAssemble.value > 0.5) {
            sAssemble.response = 0.25
            sAssemble.target = 0
            reassemble.start()
            return
        }
        sAssemble.response = mode === "turning off" ? 0.9 : 1.4
        sAssemble.target = g.assemble
    }

    Timer {
        id: reassemble
        interval: 320
        onTriggered: {
            sAssemble.response = 1.4
            sAssemble.target = 1
        }
    }

    onModeChanged: apply(false)
    Component.onCompleted: {
        apply(true)
        _advance(0)
    }

    function place() {
        const p = orb.mapToItem(null, orb.width / 2, orb.height / 2)
        Liquid.orbC = Qt.point(p.x, p.y)
        Liquid.orbR = orb.width * 0.42
    }
    onXChanged: Qt.callLater(place)
    onYChanged: Qt.callLater(place)
    onWidthChanged: Qt.callLater(place)
    onHeightChanged: Qt.callLater(place)

    // The orb's light on the background, in steps of 1/24: the background and every glass
    // surface redraw when it changes, so it changes a handful of times in a status change, not
    // on every frame of the blend (and not with the voice).
    Binding {
        target: Liquid
        property: "orbE"
        value: Math.round(orb.dim * (0.35 + 0.45 * orb.coreGlow) * 24) / 24
    }
    // Its colour, the same way: warm white, red in error, orange when the mic has failed.
    readonly property real _redStep: Math.round(sError.value * 8) / 8
    readonly property real _orangeStep: Math.round(sFail.value * 8) / 8
    Binding {
        target: Liquid
        property: "orbTint"
        value: Qt.vector3d(0.32 + 0.48 * orb._redStep + 0.33 * orb._orangeStep,
                           0.20 - 0.14 * orb._redStep + 0.04 * orb._orangeStep,
                           0.17 - 0.12 * orb._redStep - 0.10 * orb._orangeStep)
    }

    Connections {
        target: Motion
        function onTick(dt) { orb._advance(dt) }
    }

    Connections {
        target: Liquid
        function onLayoutChanged() { orb.place() }
    }
}
