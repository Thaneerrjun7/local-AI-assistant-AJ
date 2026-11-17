import QtQuick

// A stand-in voice level for the lab: syllables of 120-250 ms, short gaps between words and a
// longer pause at the end of a sentence, followed with a fast rise (15 ms) and a slower fall
// (120 ms), as the real level meter will be.
QtObject {
    id: voice

    property bool running: false
    property real level: 0
    readonly property real out: level * (0.86 + 0.14 * Math.sin(Motion.now * 37))

    property real _target: 0
    property real _until: 0

    function _step(dt) {
        if (!running) {
            _target = 0
        } else if (Motion.now >= _until) {
            const r = Math.random()
            if (_target > 0 && r < 0.07) {          // end of a sentence
                _target = 0
                _until = Motion.now + 0.5 + Math.random() * 0.4
            } else if (_target > 0 && r < 0.32) {   // between words
                _target = 0
                _until = Motion.now + 0.06 + Math.random() * 0.09
            } else {                                 // a syllable
                _target = 0.4 + 0.6 * Math.random()
                _until = Motion.now + 0.12 + 0.13 * Math.random()
            }
        }
        const rate = _target > level ? 1 / 0.015 : 1 / 0.12
        let next = level + (_target - level) * (1 - Math.exp(-dt * rate))
        if (_target === 0 && next < 0.002)
            next = 0
        if (next !== level)
            level = next
    }

    property Connections _clock: Connections {
        target: Motion
        function onTick(dt) { voice._step(dt) }
    }
}
