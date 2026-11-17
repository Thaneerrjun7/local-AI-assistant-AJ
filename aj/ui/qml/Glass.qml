import QtQuick

// One liquid-glass surface (shaders/glass.frag). The item is bigger than the glass by `margin`
// on every side, to leave room for the shadow; place it with negative anchor margins.
SceneEffect {
    id: glass
    objectName: "glass"

    property real margin: 24
    property real radius: 18
    property real inset: 0          // shrinks the shape (a press squashes it a little)
    property point pointer: Qt.point(-10000, -10000)  // in the glass's own px, not the item's
    property real hover: 0
    property real press: 0
    property point pull: Qt.point(0, 0)
    property real tint: Theme.tint
    property real frost: Theme.frost
    property real refraction: Theme.refraction
    property real rim: Theme.rim
    property real shadow: Theme.shadow
    property real dispersion: Theme.dispersion
    property real solid: Theme.solid ? 1 : 0
    still: Theme.reducedMotion || Theme.solid  // solid glass shows nothing of the liquid

    property point origin: Qt.point(0, 0)
    property size size: Qt.size(width, height)
    property rect shape: Qt.rect(margin + inset, margin + inset,
                                 Math.max(width - 2 * (margin + inset), 1), Math.max(height - 2 * (margin + inset), 1))

    fragmentShader: "../shaders/glass.frag.qsb"

    function place() {
        const p = glass.mapToItem(null, 0, 0)
        origin = Qt.point(p.x, p.y)
    }

    onXChanged: Qt.callLater(place)
    onYChanged: Qt.callLater(place)
    onWidthChanged: Qt.callLater(place)
    onHeightChanged: Qt.callLater(place)
    Component.onCompleted: Qt.callLater(place)

    property Connections _layout: Connections {
        target: Liquid
        function onLayoutChanged() { glass.place() }
    }

    // Qt's software renderer (no usable GPU) can't run shaders: a plain frosted shape instead.
    Rectangle {
        visible: Theme.software
        x: glass.shape.x
        y: glass.shape.y
        width: glass.shape.width
        height: glass.shape.height
        radius: Math.min(glass.radius, height / 2)
        color: Qt.rgba(1, 1, 1, 0.08 + glass.hover * 0.04 + glass.press * 0.08 + (glass.tint - Theme.tint))
        border.width: 1
        border.color: Qt.rgba(1, 1, 1, 0.28)
    }
}
