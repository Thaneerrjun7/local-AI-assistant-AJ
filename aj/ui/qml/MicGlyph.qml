import QtQuick
import QtQuick.Shapes

// The microphone, drawn on a 24-unit grid like an SF Symbol, in white. `off` strikes it through.
Item {
    id: glyph

    property bool off: false
    property color color: Theme.text
    readonly property real u: width / 24

    Spring { id: slash; target: glyph.off ? 1 : 0; response: 0.3 }

    Shape {
        anchors.fill: parent
        preferredRendererType: Shape.CurveRenderer
        opacity: 1 - 0.35 * slash.value

        ShapePath {  // the capsule
            strokeWidth: -1
            fillColor: glyph.color
            PathRectangle {
                x: 8.5 * glyph.u; y: 2 * glyph.u; width: 7 * glyph.u; height: 12.5 * glyph.u
                radius: 3.5 * glyph.u
            }
        }
        ShapePath {  // the holder, the stem and the base
            strokeColor: glyph.color
            strokeWidth: 1.9 * glyph.u
            fillColor: "transparent"
            capStyle: ShapePath.RoundCap
            startX: 5.5 * glyph.u; startY: 11 * glyph.u
            PathArc { x: 18.5 * glyph.u; y: 11 * glyph.u; radiusX: 6.5 * glyph.u; radiusY: 6.5 * glyph.u; direction: PathArc.Counterclockwise }
            PathMove { x: 12 * glyph.u; y: 17.5 * glyph.u }
            PathLine { x: 12 * glyph.u; y: 21.2 * glyph.u }
            PathMove { x: 8.8 * glyph.u; y: 21.2 * glyph.u }
            PathLine { x: 15.2 * glyph.u; y: 21.2 * glyph.u }
        }
    }

    Shape {  // the strike, drawn in as the mic turns off
        anchors.fill: parent
        preferredRendererType: Shape.CurveRenderer
        visible: slash.value > 0.01
        ShapePath {
            strokeColor: glyph.color
            strokeWidth: 1.9 * glyph.u
            fillColor: "transparent"
            capStyle: ShapePath.RoundCap
            startX: 4 * glyph.u; startY: 3 * glyph.u
            PathLine { x: (4 + 16 * slash.value) * glyph.u; y: (3 + 18 * slash.value) * glyph.u }
        }
    }
}
