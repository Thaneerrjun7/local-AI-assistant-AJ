import QtQuick

// The status words. A change cross-fades (out quickly, in a little slower) rather than snapping.
Item {
    id: line

    property string text: ""
    property color tone: Theme.text2  // red for an error, orange for a failed mic
    implicitWidth: label.implicitWidth
    implicitHeight: label.implicitHeight
    width: implicitWidth
    height: implicitHeight

    AJText { id: label; kind: "status"; color: line.tone; Behavior on color { ColorAnimation { duration: 240 } } }

    onTextChanged: swap.restart()
    Component.onCompleted: label.text = text

    SequentialAnimation {
        id: swap
        NumberAnimation { target: label; property: "opacity"; to: 0; duration: Theme.reducedMotion ? 60 : 110; easing.type: Easing.OutQuad }
        ScriptAction { script: label.text = line.text }
        NumberAnimation { target: label; property: "opacity"; to: 1; duration: Theme.reducedMotion ? 90 : 180; easing.type: Easing.OutQuad }
    }
}
