import QtQuick

// A slider for the lab: a hairline track, a white fill and a white knob that grows on press.
Item {
    id: slider

    property string label: ""
    property real from: 0
    property real to: 1
    property real value: 0
    property real stepSize: 0
    property int decimals: 2
    signal moved(real value)

    implicitWidth: 190
    implicitHeight: 36

    readonly property real _frac: Math.max(0, Math.min(1, (value - from) / (to - from)))

    AJText {
        kind: "caption"
        text: slider.label
        anchors.left: parent.left
        anchors.top: parent.top
    }
    AJText {
        kind: "caption"
        color: Theme.text2
        text: slider.value.toFixed(slider.decimals)
        anchors.right: parent.right
        anchors.top: parent.top
        font.features: { "tnum": 1 }
    }

    Rectangle {
        id: track
        x: 8
        width: parent.width - 16
        height: 4
        radius: 2
        y: 25
        color: Qt.rgba(1, 1, 1, 0.16)
        Rectangle {
            width: track.width * slider._frac
            height: parent.height
            radius: 2
            color: Qt.rgba(1, 1, 1, 0.78)
        }
    }

    Spring { id: grow; target: area.pressed ? 1 : 0; response: 0.22; damping: area.pressed ? 1 : 0.65 }

    Rectangle {  // the knob's shadow
        x: knob.x
        y: knob.y + 2
        width: knob.width
        height: knob.height
        radius: knob.radius
        scale: knob.scale
        color: Qt.rgba(0, 0, 0, 0.35)
    }
    Rectangle {
        id: knob
        width: 16
        height: 16
        radius: 8
        x: track.x + track.width * slider._frac - width / 2
        y: track.y + track.height / 2 - height / 2
        color: "white"
        scale: 1 + 0.2 * grow.value
    }

    MouseArea {
        id: area
        anchors.fill: parent
        anchors.topMargin: 14
        preventStealing: true
        cursorShape: Qt.PointingHandCursor
        function set(mx) {
            let f = Math.max(0, Math.min(1, (mx - track.x) / track.width))
            let v = slider.from + f * (slider.to - slider.from)
            if (slider.stepSize > 0)
                v = Math.round(v / slider.stepSize) * slider.stepSize
            slider.moved(v)
        }
        onPressed: (mouse) => set(mouse.x)
        onPositionChanged: (mouse) => set(mouse.x)
    }
}
