import QtQuick
import QtQuick.Window
import "OrbStates.js" as States

// The orb and glass lab (Phase 0 of docs/UI_REDESIGN_PLAN.md). Left: the new window at its real
// width, with a pretend conversation. Right: every status, a voice level, and the tuning.
Item {
    id: root
    width: 940
    height: 760

    property string status: "loading"
    property bool micOn: true
    property bool manualVoice: false
    property real manualLevel: 0.6
    property string savedNote: ""
    // Which orb is drawn: "shader" (shaders/orb.frag) or "3d" (Orb3D.qml, the Qt Quick 3D
    // candidate). The shader orb's controller (its springs and clocks) drives both.
    property string orbKind: "shader"
    function setOrbKind(kind) {
        if (kind === "3d" && (typeof lab === "undefined" || !lab.enable3d()))
            return
        orbKind = kind
    }
    readonly property real voiceLevel: manualVoice ? manualLevel : speaker.out
    readonly property real micLevel: manualVoice ? manualLevel : listener.out

    readonly property int frameCount: Motion.frames
    readonly property int hitchCount: Motion.hitches
    readonly property int dropCount: Motion.drops
    readonly property real lastHitchMs: Motion.lastHitchMs
    property string _defaults: ""

    // For Python: the bench and screenshot runs switch statuses with this; Windows' settings.
    function setStatus(s) { status = s }
    function setHidden(parts, noCache) {
        Theme.hidden = parts
        Theme.noCache = noCache
    }
    function setFont(family) { if (family) Theme.family = family }
    function setSoftware(on) { Theme.software = on }
    function setAccessibility(lessMotion, solid) {
        Theme.reducedMotion = lessMotion
        Theme.solid = solid
    }
    function relayout() { Liquid.layoutChanged() }

    onWidthChanged: Qt.callLater(relayout)
    onHeightChanged: Qt.callLater(relayout)

    FakeVoice { id: speaker; running: root.status === "speaking" }
    FakeVoice { id: listener; running: root.status === "hearing" || root.status === "dictating" }

    LiquidBackground { anchors.fill: parent; objectName: "background" }

    // Nothing ticks while the window is hidden or minimized (asleep, the real window is in the tray).
    Binding {
        target: Motion
        property: "running"
        value: root.Window.visibility !== Window.Hidden && root.Window.visibility !== Window.Minimized
    }

    HoverHandler {
        id: pointer
        onPointChanged: if (hovered) Liquid.moved(point.position.x, point.position.y)
        onHoveredChanged: if (!hovered) Liquid.left()
    }

    Component.onCompleted: {
        _defaults = Theme.save()
        if (typeof lab !== "undefined")
            Theme.load(lab.load())
        Qt.callLater(relayout)
        loadedTimer.start()
    }
    Timer { id: loadedTimer; interval: 2600; onTriggered: if (root.status === "loading") root.status = "listening" }

    // ------------------------------------------------------------------ the window preview
    // The real window's own content (Face.qml), at its real width, with a pretend conversation.
    ListModel {
        id: chat
        ListElement { who: "You"; words: "What's the weather tomorrow?"; times: "" }
        ListElement { who: "AJ"; words: "Cloudy, fourteen degrees. Bring the grey jacket."; times: "heard 210 · first token 265 · first sound 610 ms" }
        ListElement { who: "You"; words: "Remind me at six to call Mum."; times: "" }
        ListElement { who: "AJ"; words: "Done. Six o'clock, call Mum."; times: "heard 190 · first token 240 · first sound 580 ms" }
    }

    Face {
        id: preview
        width: 420
        height: parent.height
        name: "AJ"
        mode: root.status
        statusText: States.labels[root.status] || root.status
        statusTone: root.status === "error" ? "#ff5a4e" : root.status === "mic failed" ? "#ffa25c" : ""
        micOn: root.micOn
        ready: true
        micCaption: root.micOn ? "Mic on · click to mute" : "Mic off · click to listen"
        footnote: "⚠ Some lines are quotes from fiction, said in character, not real intent."
        model: chat
        voiceLevel: root.voiceLevel
        micLevel: root.micLevel
        orbKind: root.orbKind
        onMicClicked: {
            root.micOn = !root.micOn
            root.status = root.micOn ? "listening" : "mic off"
        }
        onSent: (text) => root.ask(text)
        onCloseClicked: root.status = "turning off"
    }

    // A typed question walks through the statuses as the real thing would.
    property var _script: []
    function ask(text) {
        text = text.trim()
        if (!text.length)
            return
        chat.append({ who: "You", words: text, times: "" })
        const tool = /weather|calculat|search|open|remind|time/i.test(text)
        _script = [["thinking", 1100]]
        if (tool)
            _script.push(["working", 1300])
        _script.push(["speaking", 2600], ["listening", 0])
        _next()
    }
    function _next() {
        if (!_script.length)
            return
        const [s, ms] = _script.shift()
        status = s
        if (s === "listening")
            chat.append({ who: "AJ", words: "This is the lab, so that's a pretend answer.", times: "heard 200 · first token 250 · first sound 600 ms" })
        if (ms > 0) {
            scriptTimer.interval = ms
            scriptTimer.restart()
        }
    }
    Timer { id: scriptTimer; onTriggered: root._next() }

    // ------------------------------------------------------------------ the lab's controls
    Rectangle {
        x: preview.width + 10
        y: 24
        width: 1
        height: parent.height - 48
        color: Qt.rgba(1, 1, 1, 0.08)
    }

    Flickable {
        id: panel
        x: preview.width + 30
        y: 0
        width: parent.width - x - 20
        height: parent.height
        contentHeight: controls.height + 40
        clip: true
        boundsBehavior: Flickable.DragOverBounds
        onContentYChanged: root.relayout()

        Column {
            id: controls
            y: 22
            width: panel.width
            spacing: 14

            Column {
                spacing: 2
                AJText { kind: "wordmark"; text: "Lab"; font.pixelSize: 22; font.letterSpacing: -0.4 }
                AJText { kind: "caption"; text: "Phase 0 · the orb, the warp and the glass" }
            }

            AJText { kind: "section"; text: "Status" }
            Flow {
                width: parent.width
                spacing: 8
                Repeater {
                    model: States.order
                    GlassButton {
                        id: chip
                        required property string modelData
                        width: chipText.implicitWidth + 26
                        height: 30
                        checked: root.status === modelData
                        reach: 0.04
                        onClicked: {
                            root.status = modelData
                            root.micOn = modelData !== "mic off"
                        }
                        AJText {
                            id: chipText
                            kind: "button"
                            font.pixelSize: 13
                            text: chip.modelData
                            anchors.centerIn: parent
                        }
                    }
                }
            }

            AJText { kind: "section"; text: "Voice" }
            Row {
                spacing: 12
                GlassButton {
                    width: 132
                    height: 30
                    checked: !root.manualVoice
                    onClicked: root.manualVoice = !root.manualVoice
                    AJText { kind: "button"; font.pixelSize: 13; text: "Fake voice"; anchors.centerIn: parent }
                }
                LabSlider {
                    width: 200
                    label: "Level (sets it by hand)"
                    value: root.manualLevel
                    onMoved: (v) => { root.manualLevel = v; root.manualVoice = true }
                }
            }

            AJText { kind: "section"; text: "Orb" }
            Row {
                spacing: 8
                GlassButton {
                    width: 120; height: 30
                    checked: root.orbKind === "shader"
                    onClicked: root.setOrbKind("shader")
                    AJText { kind: "button"; font.pixelSize: 13; text: "Shader orb"; anchors.centerIn: parent }
                }
                GlassButton {
                    width: 120; height: 30
                    checked: root.orbKind === "3d"
                    onClicked: root.setOrbKind("3d")
                    AJText { kind: "button"; font.pixelSize: 13; text: "3D orb"; anchors.centerIn: parent }
                }
            }
            Grid {
                columns: 2
                columnSpacing: 18
                rowSpacing: 4
                LabSlider { label: "Dots"; from: 300; to: 3000; stepSize: 50; decimals: 0; value: Theme.dots; onMoved: (v) => Theme.dots = v }
                LabSlider { label: "Dot size"; from: 0.06; to: 0.32; value: Theme.dotSize; onMoved: (v) => Theme.dotSize = v }
                LabSlider { label: "Glow"; from: 0; to: 2; value: Theme.glow; onMoved: (v) => Theme.glow = v }
                LabSlider { label: "Synapses"; from: 0; to: 2.5; value: Theme.links; onMoved: (v) => Theme.links = v }
                LabSlider { label: "Firing"; from: 0; to: 1.5; value: Theme.fire; onMoved: (v) => Theme.fire = v }
                LabSlider { label: "Sparks"; from: 0; to: 4; value: Theme.sparks; onMoved: (v) => Theme.sparks = v }
                LabSlider { label: "Speed"; from: 0.2; to: 2.5; value: Theme.speed; onMoved: (v) => Theme.speed = v }
                LabSlider { label: "Rings"; from: 0; to: 2; value: Theme.rings; onMoved: (v) => Theme.rings = v }
                LabSlider { label: "Core light"; from: 0; to: 2; value: Theme.core; onMoved: (v) => Theme.core = v }
                LabSlider { label: "Core prism"; from: 0; to: 0.8; value: Theme.dispersionOrb; onMoved: (v) => Theme.dispersionOrb = v }
            }

            AJText { kind: "section"; text: "Background warp" }
            Grid {
                columns: 2
                columnSpacing: 18
                rowSpacing: 4
                LabSlider { label: "Wake"; from: 0; to: 1.5; value: Theme.smear; onMoved: (v) => Theme.smear = v }
                LabSlider { label: "Sheen"; from: 0; to: 2; value: Theme.shine; onMoved: (v) => Theme.shine = v }
                LabSlider { label: "Dot grid"; from: 0; to: 2; value: Theme.grid; onMoved: (v) => Theme.grid = v }
                LabSlider { label: "Ember"; from: 0; to: 1.5; value: Theme.warmth; onMoved: (v) => Theme.warmth = v }
            }

            AJText { kind: "section"; text: "Glass" }
            Grid {
                columns: 2
                columnSpacing: 18
                rowSpacing: 4
                LabSlider { label: "Tint"; from: 0; to: 0.3; value: Theme.tint; onMoved: (v) => Theme.tint = v }
                LabSlider { label: "Frost"; from: 0; to: 1; value: Theme.frost; onMoved: (v) => Theme.frost = v }
                LabSlider { label: "Lens"; from: 0; to: 1.5; value: Theme.refraction; onMoved: (v) => Theme.refraction = v }
                LabSlider { label: "Rim light"; from: 0; to: 1.5; value: Theme.rim; onMoved: (v) => Theme.rim = v }
                LabSlider { label: "Shadow"; from: 0; to: 1.5; value: Theme.shadow; onMoved: (v) => Theme.shadow = v }
                LabSlider { label: "Prism"; from: 0; to: 0.5; value: Theme.dispersion; onMoved: (v) => Theme.dispersion = v }
            }

            AJText { kind: "section"; text: "Accessibility" }
            Row {
                spacing: 8
                GlassButton {
                    width: 132; height: 30
                    checked: Theme.reducedMotion
                    onClicked: Theme.reducedMotion = !Theme.reducedMotion
                    AJText { kind: "button"; font.pixelSize: 13; text: "Reduce motion"; anchors.centerIn: parent }
                }
                GlassButton {
                    width: 132; height: 30
                    checked: Theme.solid
                    onClicked: Theme.solid = !Theme.solid
                    AJText { kind: "button"; font.pixelSize: 13; text: "Solid glass"; anchors.centerIn: parent }
                }
            }

            AJText { kind: "section"; text: "Running" }
            AJText {
                id: meters
                kind: "caption"
                color: Theme.text2
                font.features: { "tnum": 1 }
                property int fps: 0
                property int _last: 0
                text: fps + " fps"
                      + (typeof lab !== "undefined" ? "  ·  CPU " + lab.cpu.toFixed(1) + "%"
                         + "  ·  GPU " + (lab.gpu < 0 ? "n/a" : lab.gpu.toFixed(1) + "%") : "")
                Timer {
                    interval: 1000
                    running: true
                    repeat: true
                    onTriggered: {
                        meters.fps = Motion.frames - meters._last
                        meters._last = Motion.frames
                    }
                }
            }
            Row {
                spacing: 8
                GlassButton {
                    width: 120; height: 30
                    onClicked: {
                        if (typeof lab !== "undefined")
                            root.savedNote = "Saved to " + lab.save(Theme.save())
                    }
                    AJText { kind: "button"; font.pixelSize: 13; text: "Save tuning"; anchors.centerIn: parent }
                }
                GlassButton {
                    width: 90; height: 30
                    onClicked: {
                        Theme.load(root._defaults)
                        root.savedNote = "Back to the defaults (not saved)"
                    }
                    AJText { kind: "button"; font.pixelSize: 13; text: "Reset"; anchors.centerIn: parent }
                }
            }
            AJText {
                kind: "foot"
                text: root.savedNote
                visible: text.length > 0
                width: parent.width
                wrapMode: Text.Wrap
            }
        }
    }
}
