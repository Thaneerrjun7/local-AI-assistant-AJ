import QtQuick

// What the AJ window shows: the wordmark and status, Close, the orb, the mic button, the
// conversation and the box to type in. It only shows what it's given and says what was clicked;
// the real window (Main.qml, driven by aj/ui/window.py) and the lab (Lab.qml, a pretend
// conversation) decide what that means.
Item {
    id: face

    property string name: "AJ"
    property string mode: "loading"      // the orb's status (OrbStates.js)
    property string statusText: ""
    property string statusTone: ""       // a colour for an alarm (error, mic failed); "" for the usual
    property bool micOn: true
    property bool ready: false           // the mic button and the text box work only once loaded
    property bool closeEnabled: true
    property string micCaption: ""
    property string footnote: ""
    property var model                   // the conversation: who, words, times
    property real voiceLevel: 0
    property real micLevel: 0
    property string orbKind: "shader"    // the lab can show the Qt Quick 3D candidate instead
    readonly property alias orb: orb
    readonly property alias entryText: input.text

    signal micClicked()
    signal sent(string text)
    signal closeClicked()

    // The same as clicking (Python's tests call these too, so they go through the QML).
    function pressMic() {
        if (ready)
            micClicked()
    }
    function pressClose() {
        if (closeEnabled)
            closeClicked()
    }
    function type(text) { input.text = text }
    // Enter or the send button. Blank text isn't sent, and is left where it is.
    function submit() {
        const text = input.text.trim()
        if (!ready || !text.length)
            return
        sent(text)
        input.clear()
    }

    // Loading is over: typing works straight away, without a click.
    onReadyChanged: if (ready) input.forceActiveFocus()

    AJText {
        id: wordmark
        kind: "wordmark"
        text: face.name
        x: 24
        y: 20
    }
    StatusLine {
        id: statusLine
        objectName: "statusLine"
        text: face.statusText
        tone: face.statusTone.length ? face.statusTone : Theme.text2
        anchors.left: wordmark.left
        anchors.top: wordmark.bottom
        anchors.topMargin: 2
    }
    GlassButton {
        id: closeButton
        objectName: "closeButton"
        width: 78
        height: 34
        anchors.right: parent.right
        anchors.rightMargin: 22
        y: 24
        enabled: face.closeEnabled
        onClicked: face.pressClose()
        AJText { kind: "button"; text: "Close"; anchors.centerIn: parent }
    }

    Orb {
        id: orb
        objectName: "orb"
        // The hero, but the conversation needs room too: it grows with the window, 210 to 300 px.
        width: Math.round(Math.min(300, Math.max(210, face.height * 0.37)))
        height: width
        anchors.horizontalCenter: parent.horizontalCenter
        y: 72
        mode: face.mode
        voiceLevel: face.voiceLevel
        micLevel: face.micLevel
        visible: Theme.hidden.indexOf("orb") < 0 && face.orbKind === "shader"

        // Qt's software renderer can't run the orb's shader: a still ring and core instead.
        Rectangle {
            visible: Theme.software
            anchors.centerIn: parent
            width: parent.width * 0.56
            height: width
            radius: width / 2
            color: "transparent"
            border.width: 1
            border.color: Qt.rgba(1, 1, 1, 0.45)
            Rectangle {
                anchors.centerIn: parent
                width: parent.width * 0.41
                height: width
                radius: width / 2
                color: Qt.rgba(1, 1, 1, 0.10)
                border.width: 1
                border.color: Qt.rgba(1, 1, 1, 0.6)
            }
        }
    }

    // The lab's Qt Quick 3D candidate, over the shader orb and driven by its values. Never
    // loaded in the real window.
    Loader {
        x: orb.x
        y: orb.y
        width: orb.width
        height: orb.height
        active: face.orbKind === "3d"
        visible: Theme.hidden.indexOf("orb") < 0
        source: active ? "Orb3D.qml" : ""
        readonly property var shared: ["spin", "tiltX", "tiltY", "band", "dots", "dotSize", "dim",
            "linkAlpha", "fire", "spark", "glow", "shell", "assemble", "coreGlow", "voice", "mic",
            "wSpeak", "wHear", "wDictate", "wWork", "wLoad", "wFail", "wError", "wSleep",
            "ringAlpha", "ringTight", "dispersion"]
        onLoaded: {
            for (const name of shared)
                item[name] = Qt.binding(() => orb[name])
            item.t = Qt.binding(() => orb._t)
            item.flow = Qt.binding(() => orb._flow)
            item.coreSpin = Qt.binding(() => orb._core[0])
            item.ringSpin = Qt.binding(() => orb._ring[9] * 5)
        }
    }

    GlassButton {
        id: micButton
        objectName: "micButton"
        width: 72
        height: 72
        anchors.horizontalCenter: parent.horizontalCenter
        y: orb.y + orb.height - 4
        enabled: face.ready
        onClicked: face.pressMic()
        MicGlyph {
            width: 30
            height: 30
            anchors.centerIn: parent
            off: !face.micOn
        }
    }
    AJText {
        id: micCaption
        objectName: "micCaption"
        kind: "caption"
        text: face.micCaption
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.top: micButton.bottom
        anchors.topMargin: 10
        height: 16  // blank while loading; the layout mustn't jump
    }

    // The conversation, on one big glass panel. Nothing glass sits on it.
    Item {
        id: conversation
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.leftMargin: 18
        anchors.rightMargin: 18
        anchors.top: micCaption.bottom
        anchors.topMargin: 16
        anchors.bottom: composer.top
        anchors.bottomMargin: 12

        Glass {
            anchors.fill: parent
            anchors.margins: -margin
            radius: 22
            frost: Math.min(1, Theme.frost + 0.25)  // a bigger surface reads as thicker
            shadow: Theme.shadow * 1.3
        }
        AJText {
            kind: "caption"
            text: "Talk, or type below. The conversation shows up here."
            color: Theme.text4
            anchors.centerIn: parent
            visible: messages.count === 0
        }
        ListView {
            id: messages
            objectName: "messages"
            anchors.fill: parent
            anchors.margins: 16
            clip: true
            spacing: 12
            model: face.model
            // The newest line stays in view unless you've scrolled up to read something.
            property bool follow: true
            onMovementEnded: follow = atYEnd
            onCountChanged: if (follow) Qt.callLater(positionViewAtEnd)
            // A new message's height is only known once it's laid out (wrapped): follow that too.
            onContentHeightChanged: if (follow && !moving) Qt.callLater(positionViewAtEnd)
            delegate: Column {
                id: message
                width: ListView.view.width
                spacing: 2
                required property string who
                required property string words
                required property string times
                readonly property bool isError: who === "Error"
                AJText {
                    kind: "caption"
                    text: message.who
                    color: message.isError ? "#ff5a4e" : message.who === "You" ? Theme.text3 : Theme.text2
                }
                AJText {
                    kind: "body"
                    text: message.words
                    width: parent.width
                    wrapMode: Text.Wrap
                    textFormat: Text.PlainText  // what was said shows literally, and can never restyle the window
                }
                AJText { kind: "foot"; text: message.times; visible: message.times.length > 0; textFormat: Text.PlainText }
            }
        }
    }

    // The box to type in: a glass capsule; the send button is solid white (no glass on glass).
    Item {
        id: composer
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.leftMargin: 18
        anchors.rightMargin: 18
        anchors.bottom: footnoteText.top
        anchors.bottomMargin: 10
        height: 46
        opacity: face.ready ? 1 : 0.55

        Spring { id: focusS; target: Math.max(composerHover.hovered ? 0.6 : 0, input.activeFocus ? 1 : 0); response: 0.3 }
        Glass {
            anchors.fill: parent
            anchors.margins: -margin
            radius: 23
            hover: focusS.value
            pointer: Qt.point(composerHover.point.position.x + margin, composerHover.point.position.y + margin)
        }
        HoverHandler { id: composerHover; cursorShape: face.ready ? Qt.IBeamCursor : Qt.ArrowCursor }

        TextInput {
            id: input
            objectName: "entry"
            anchors.left: parent.left
            anchors.right: send.left
            anchors.leftMargin: 18
            anchors.rightMargin: 8
            anchors.verticalCenter: parent.verticalCenter
            enabled: face.ready
            color: Theme.text
            selectionColor: Qt.rgba(1, 1, 1, 0.3)
            selectedTextColor: Theme.text
            selectByMouse: true
            font.family: Theme.family
            font.pixelSize: 15
            font.letterSpacing: -0.15
            renderType: Text.NativeRendering
            clip: true
            onAccepted: face.submit()
            AJText {
                kind: "body"
                text: "Message " + face.name
                color: Theme.text4
                visible: input.text.length === 0
                anchors.verticalCenter: parent.verticalCenter
            }
        }

        Spring { id: sendS; target: input.text.trim().length > 0 && face.ready ? 1 : 0; response: 0.32; damping: 0.7 }
        Rectangle {
            id: send
            width: 32
            height: 32
            radius: 16
            anchors.right: parent.right
            anchors.rightMargin: 7
            anchors.verticalCenter: parent.verticalCenter
            color: "white"
            scale: Math.max(sendS.value, 0.001) * (1 - 0.08 * sendPress.value)
            opacity: Math.min(1, sendS.value * 1.5)
            Arrow { anchors.centerIn: parent; width: 14; height: 14 }
            TapHandler { id: sendTap; enabled: sendS.target > 0; onTapped: face.submit() }
            Spring {
                id: sendPress
                target: sendTap.pressed ? 1 : 0
                response: sendTap.pressed ? 0.12 : 0.42
                damping: sendTap.pressed ? 1.0 : 0.62
            }
        }
    }

    AJText {
        id: footnoteText
        objectName: "disclaimer"
        kind: "foot"
        text: face.footnote
        width: parent.width - 32
        horizontalAlignment: Text.AlignHCenter
        wrapMode: Text.Wrap
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: 12
    }
}
