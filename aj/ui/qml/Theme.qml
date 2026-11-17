pragma Singleton
import QtQuick

// The look in one place: type, text colours, and every value the lab can tune. The lab's
// sliders write these; the real window will read the saved tuning at start.
QtObject {
    id: theme

    // Type: Inter (open licence, drawn in the image of Apple's SF Pro). Tracking is per size,
    // as Apple's is: tight on big text, about zero on body text, slightly open on captions.
    property string family: "Inter Variable"  // set from Python to the family Qt actually loaded
    readonly property color text: "#ffffff"
    readonly property color text2: Qt.rgba(1, 1, 1, 0.72)
    readonly property color text3: Qt.rgba(1, 1, 1, 0.55)
    readonly property color text4: Qt.rgba(1, 1, 1, 0.40)
    readonly property color base: "#0e0605"

    // Windows' accessibility settings (read in Python at start; the lab can flip them).
    property bool reducedMotion: false
    property bool solid: false  // transparency effects off
    property bool software: false  // Qt's software renderer: no shaders, plain shapes instead
    // For measuring what each layer costs (python -m aj.ui.lab --bench-parts).
    property var hidden: []
    property bool noCache: false

    // Defaults are the user's tuning from the lab (2026-10-23); a saved orb_tuning.json
    // still loads on top.

    // The orb (multipliers on each status's own values, except dots and dotSize).
    property real dots: 1200
    property real dotSize: 0.13
    property real glow: 1
    property real links: 1
    property real fire: 0.67
    property real sparks: 1
    property real speed: 1.89
    property real rings: 2
    property real core: 1.18
    property real dispersionOrb: 0.49

    // The background and its warp round the pointer.
    property real smear: 1.5
    property real shine: 0
    property real grid: 1.1
    property real warmth: 0.36

    // The glass.
    property real tint: 0.08
    property real frost: 0.47
    property real refraction: 0.55
    property real rim: 0.8
    property real shadow: 0.53
    property real dispersion: 0.31

    // Every tunable and its range (the lab's sliders use the same ranges). Saved tuning is
    // clamped to these when loaded, so a hand-edited file can't hide the lattice.
    readonly property var ranges: ({
        "dots": [300, 3000], "dotSize": [0.06, 0.32], "glow": [0, 2], "links": [0, 2.5], "fire": [0, 1.5],
        "sparks": [0, 4], "speed": [0.2, 2.5], "rings": [0, 2], "core": [0, 2], "dispersionOrb": [0, 0.8],
        "smear": [0, 1.5], "shine": [0, 2], "grid": [0, 2], "warmth": [0, 1.5],
        "tint": [0, 0.3], "frost": [0, 1], "refraction": [0, 1.5], "rim": [0, 1.5], "shadow": [0, 1.5],
        "dispersion": [0, 0.5]
    })
    readonly property var tunables: Object.keys(ranges)

    function save() {
        const out = {}
        for (const k of tunables)
            out[k] = theme[k]
        return JSON.stringify(out, null, 2)
    }

    function load(json) {
        if (!json)
            return
        try {
            const values = JSON.parse(json)
            for (const k of tunables) {
                const v = values[k]
                if (typeof v === "number" && isFinite(v))
                    theme[k] = Math.max(ranges[k][0], Math.min(ranges[k][1], v))
            }
        } catch (e) {
            console.warn("tuning not loaded:", e)
        }
    }
}
