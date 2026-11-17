import QtQuick

// The window's background: dull ember, liquefying under the pointer (shaders/background.frag).
SceneEffect {
    fragmentShader: "../shaders/background.frag.qsb"

    // Qt's software renderer (no usable GPU) can't run shaders: a plain gradient instead.
    Rectangle {
        anchors.fill: parent
        visible: Theme.software
        gradient: Gradient {
            GradientStop { position: 0.0; color: "#1f0907" }
            GradientStop { position: 1.0; color: Theme.base }
        }
    }
}
