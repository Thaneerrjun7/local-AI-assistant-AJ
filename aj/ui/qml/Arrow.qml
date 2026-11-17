import QtQuick
import QtQuick.Shapes

// The send arrow (up), dark on the white send button.
Shape {
    id: arrow
    readonly property real u: width / 14
    preferredRendererType: Shape.CurveRenderer
    ShapePath {
        strokeColor: "#1a0b09"
        strokeWidth: 2 * arrow.u
        fillColor: "transparent"
        capStyle: ShapePath.RoundCap
        joinStyle: ShapePath.RoundJoin
        startX: 7 * arrow.u; startY: 12.5 * arrow.u
        PathLine { x: 7 * arrow.u; y: 1.8 * arrow.u }
        PathMove { x: 2.4 * arrow.u; y: 6.2 * arrow.u }
        PathLine { x: 7 * arrow.u; y: 1.6 * arrow.u }
        PathLine { x: 11.6 * arrow.u; y: 6.2 * arrow.u }
    }
}
