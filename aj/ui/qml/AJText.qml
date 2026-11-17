import QtQuick

// Text in one of the type styles. Sizes, weights, tracking and leading follow the plan's type
// table: tracking is set per size (tight when big, open when small), never one value for all.
Text {
    property string kind: "body"  // wordmark, status, body, caption, foot, section

    readonly property var _styles: ({
        "wordmark": { size: 28, weight: Font.DemiBold, track: -0.02, lead: 1.05, color: Theme.text, opsz: 32 },
        "status": { size: 15, weight: Font.Medium, track: -0.01, lead: 1.2, color: Theme.text2, opsz: 15 },
        "body": { size: 15, weight: Font.Normal, track: -0.01, lead: 1.4, color: Theme.text, opsz: 15 },
        "button": { size: 14, weight: Font.Medium, track: -0.005, lead: 1.2, color: Theme.text, opsz: 14 },
        "caption": { size: 12, weight: Font.Medium, track: 0.01, lead: 1.3, color: Theme.text3, opsz: 12 },
        "foot": { size: 11, weight: Font.Normal, track: 0.02, lead: 1.3, color: Theme.text4, opsz: 11 },
        "section": { size: 11, weight: Font.DemiBold, track: 0.06, lead: 1.2, color: Theme.text4, opsz: 11 }
    })
    readonly property var _s: _styles[kind] || _styles.body

    font.family: Theme.family
    font.pixelSize: _s.size
    font.weight: _s.weight
    font.letterSpacing: _s.size * _s.track
    font.variableAxes: ({ "opsz": Math.max(14, Math.min(32, _s.opsz)) })
    font.capitalization: kind === "section" ? Font.AllUppercase : Font.MixedCase
    // Leading as a multiple of the size, as the type table gives it (Inter's own line height
    // is 1.21 em, so the proportional mode would make every line ~25% looser).
    lineHeightMode: Text.FixedHeight
    lineHeight: Math.round(font.pixelSize * _s.lead)
    color: _s.color
    // Windows' own text rendering is crisper than Qt's at small sizes; big text uses Qt's curves.
    renderType: _s.size >= 22 ? Text.CurveRendering : Text.NativeRendering
}
