pragma Singleton
import QtQuick

// The one clock everything animated runs on, and the springs. It ticks once per frame Qt
// draws (FrameAnimation), in Qt's own thread and JavaScript: Python never runs per frame.
// `running` is off while the window is hidden or minimized, so nothing ticks in the tray.
QtObject {
    id: motion

    property bool running: true
    property real now: 0     // s since start, on the frame clock
    property int frames: 0   // frames ticked, for the lab's frame rate readout
    signal tick(real dt)

    property var _springs: []

    function add(spring) {
        if (_springs.indexOf(spring) < 0)
            _springs.push(spring)
    }

    function remove(spring) {
        const i = _springs.indexOf(spring)
        if (i >= 0)
            _springs.splice(i, 1)
    }

    // Slow frames, for the lab's stutter check (python -m aj.ui.lab --hitches): a frame that took
    // more than 25 ms, and when the last one was.
    property int hitches: 0
    property int drops: 0  // frames that took longer than two refreshes of a 165 Hz screen
    property real lastHitchAt: 0
    property real lastHitchMs: 0

    function _frame(dt) {
        if (dt > 0.0155)
            drops += 1
        if (dt > 0.025) {
            hitches += 1
            lastHitchAt = now
            lastHitchMs = dt * 1000
        }
        dt = Math.min(Math.max(dt, 0), 0.05)  // a stall (a dragged window) mustn't fling the springs
        now += dt
        frames += 1
        const moving = []
        for (const s of _springs) {
            // A spring destroyed mid-flight must not stop every other animation.
            if (!s || typeof s.step !== "function")
                continue
            try {
                if (!s.step(dt))
                    moving.push(s)
            } catch (e) {
                console.warn("a spring failed and was dropped:", e)
            }
        }
        _springs = moving
        tick(dt)
    }

    property FrameAnimation driver: FrameAnimation {
        running: motion.running
        onTriggered: motion._frame(frameTime)
    }
}
