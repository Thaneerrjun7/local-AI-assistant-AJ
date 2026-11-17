import QtQuick

// A liquid-glass button. It lights up under the pointer and reaches toward it a little; on
// press (not release) it squashes and brightens, then springs back when let go.
Item {
    id: button

    property real radius: height / 2
    property bool checked: false
    property real reach: 0.06            // how far it stretches toward the pointer, as a share of its size
    readonly property real _reach: Theme.reducedMotion ? 0 : reach
    default property alias content: holder.data
    signal clicked

    readonly property bool hovered: hover.hovered
    readonly property bool pressed: tap.pressed
    opacity: enabled ? 1 : 0.45

    Spring { id: hoverS; target: hover.hovered && button.enabled ? 1 : 0; response: 0.28 }
    Spring {
        id: pressS
        target: tap.pressed ? 1 : 0
        response: tap.pressed ? 0.12 : 0.42
        damping: tap.pressed ? 1.0 : 0.62
    }
    Spring { id: checkS; target: button.checked ? 1 : 0; response: 0.3 }
    Spring {
        id: pullX
        target: hover.hovered && button.enabled
                ? Math.max(-1, Math.min(1, (hover.point.position.x - button.width / 2) / (button.width / 2))) * button.width * button._reach
                : 0
        response: 0.38
        damping: 0.72
    }
    Spring {
        id: pullY
        target: hover.hovered && button.enabled
                ? Math.max(-1, Math.min(1, (hover.point.position.y - button.height / 2) / (button.height / 2))) * button.height * button._reach
                : 0
        response: 0.38
        damping: 0.72
    }

    Glass {
        id: glass
        anchors.fill: parent
        anchors.margins: -margin
        radius: button.radius
        hover: hoverS.value
        press: pressS.value
        inset: pressS.value * Math.min(button.width, button.height) * 0.025
        pull: Qt.point(pullX.value, pullY.value)
        pointer: Qt.point(hover.point.position.x + margin, hover.point.position.y + margin)
        tint: Theme.tint + checkS.value * 0.13
    }

    Item {
        id: holder
        anchors.fill: parent
        scale: 1 - 0.05 * pressS.value
        transform: Translate { x: pullX.value * 0.35; y: pullY.value * 0.35 }
    }

    HoverHandler { id: hover; cursorShape: Qt.PointingHandCursor }
    TapHandler {
        id: tap
        gesturePolicy: TapHandler.ReleaseWithinBounds
        onTapped: button.clicked()
    }
}
