import QtQuick

// A spring with Apple's two parameters (the apple-design skill): `response`, how quickly it
// gets there in seconds, and `damping`, 1 for no overshoot and below 1 to bounce. Set `target`;
// `value` follows from wherever it is now and keeps its speed, so it can be redirected at any
// moment without a jump.
QtObject {
    id: spring

    property real target: 0
    property real value: 0
    property real velocity: 0
    property real response: 0.35
    property real damping: 1.0

    onTargetChanged: Motion.add(spring)
    Component.onCompleted: value = target
    Component.onDestruction: Motion.remove(spring)

    function snap(to) {
        if (to !== undefined)
            target = to
        value = target
        velocity = 0
    }

    // Returns true once it has come to rest.
    function step(dt) {
        const resp = Theme.reducedMotion ? Math.min(response, 0.2) : response
        const zeta = Theme.reducedMotion ? 1.0 : damping
        const k = Math.pow(2 * Math.PI / resp, 2)
        const c = 4 * Math.PI * zeta / resp
        const n = Math.max(1, Math.ceil(dt / 0.004))
        const h = dt / n
        let x = value
        let v = velocity
        for (let i = 0; i < n; i++) {
            v += (k * (target - x) - c * v) * h
            x += v * h
        }
        if (Math.abs(target - x) < 1e-4 && Math.abs(v) < 1e-3) {
            value = target
            velocity = 0
            return true
        }
        value = x
        velocity = v
        return false
    }
}
