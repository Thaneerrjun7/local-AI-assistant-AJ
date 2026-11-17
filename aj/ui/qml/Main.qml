import QtQuick
import QtQuick.Window

// The AJ window. aj/ui/window.py sets these properties (only ever on the Qt thread) and listens
// to the three signals; `conversation` is its list model, set before this loads.
Item {
    id: root
    width: 420
    height: 720

    property alias name: face.name
    property alias mode: face.mode
    property alias statusText: face.statusText
    property alias statusTone: face.statusTone
    property alias micOn: face.micOn
    property alias ready: face.ready
    property alias closeEnabled: face.closeEnabled
    property alias micCaption: face.micCaption
    property alias footnote: face.footnote
    property alias voiceLevel: face.voiceLevel
    property alias micLevel: face.micLevel
    readonly property alias entryText: face.entryText

    signal micClicked()
    signal sent(string text)
    signal closeClicked()

    // Closing (the title-bar X, Alt+F4, Close). Until the engine has ended it may not really
    // close: AJ is turned off first, and window.py sets mayClose once the engine is done. Handled
    // here, in QML, so Python never sits in the window's event path: an override of event() in
    // Python froze the window at start (2026-10-23; Qt delivers some window events from its own
    // threads, which then waited on Python's lock while the main thread waited on them).
    property bool mayClose: false
    signal closeRefused()   // asked to close before the engine has ended: turn AJ off
    signal closedForReal()  // allowed, and closing now
    Connections {
        target: root.Window.window
        function onClosing(close) {
            close.accepted = root.mayClose
            if (root.mayClose)
                root.closedForReal()
            else
                root.closeRefused()
        }
    }

    // For window.py: the font Qt loaded, Windows' accessibility settings, the saved tuning, and
    // whether Qt is drawing without a GPU.
    function setFont(family) { if (family) Theme.family = family }
    function setAccessibility(lessMotion, solid) {
        Theme.reducedMotion = lessMotion
        Theme.solid = solid
    }
    function loadTuning(json) { Theme.load(json) }
    function setSoftware(on) { Theme.software = on }
    // What a click or a key does, for the tests (they go through the same QML as the real thing).
    function pressMic() { face.pressMic() }
    function pressClose() { face.pressClose() }
    function type(text) { face.type(text) }
    function submit() { face.submit() }
    function relayout() { Liquid.layoutChanged() }

    onWidthChanged: Qt.callLater(relayout)
    onHeightChanged: Qt.callLater(relayout)
    Component.onCompleted: Qt.callLater(relayout)

    LiquidBackground { anchors.fill: parent; objectName: "background" }

    // Nothing ticks while the window is hidden or minimized (asleep, it is in the tray).
    Binding {
        target: Motion
        property: "running"
        value: root.Window.visibility !== Window.Hidden && root.Window.visibility !== Window.Minimized
    }

    HoverHandler {
        onPointChanged: if (hovered) Liquid.moved(point.position.x, point.position.y)
        onHoveredChanged: if (!hovered) Liquid.left()
    }

    Face {
        id: face
        anchors.fill: parent
        model: typeof conversation !== "undefined" ? conversation : null
        onMicClicked: root.micClicked()
        onSent: (text) => root.sent(text)
        onCloseClicked: root.closeClicked()
    }
}
