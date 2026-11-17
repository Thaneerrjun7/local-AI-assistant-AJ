// The neural orb in Qt Quick 3D: real 3D dots on a Fibonacci lattice (a mesh built once in
// aj/ui/orb3d.py) that the vertex shader moves, lights and fires. It takes the same real
// properties as the shader orb, so the lab can swap one for the other. Everything is drawn
// additively with no depth writes, so draw order never matters.
import QtQuick
import QtQuick3D
import QtQuick3D.Helpers
import AJ3D 1.0

Item {
    id: root
    implicitWidth: 400
    implicitHeight: 400

    property real t: 0          // s
    property real flow: 0       // the firing clock
    property real spin: 0       // rad, about the vertical axis
    property real tiltX: 0      // rad, extra tilt toward the pointer
    property real tiltY: 0
    property real coreSpin: 0
    property real ringSpin: 0
    property real band: 0       // Working: scroll phase of the latitude bands

    property real dots: 1200    // 300..3000; the mesh is rebuilt only when the rounded count changes
    property real dotSize: 0.16 // dot radius as a fraction of the mean lattice spacing
    property real dim: 1.0
    property real linkAlpha: 0.1
    property real fire: 0
    property real spark: 0.002
    property real glow: 0.7
    property real shell: 1.0
    property real assemble: 1.0
    property real coreGlow: 0.4
    property real voice: 0
    property real mic: 0
    property real wSpeak: 0
    property real wHear: 0
    property real wDictate: 0
    property real wWork: 0
    property real wLoad: 0
    property real wFail: 0
    property real wError: 0
    property real wSleep: 0
    property real ringAlpha: 0.16
    property real ringTight: 0
    property real dispersion: 0

    // Not part of the shared interface. Offscreen is the only mode with the bloom pass
    // (ExtendedSceneEnvironment's glow); Inline and Underlay draw straight into the window,
    // and Underlay cannot sit above other items.
    property alias renderMode: view.renderMode
    property bool bloom: true

    // The meshes, built once in Python (rebuilt only when the rounded dot count changes).
    FibonacciLattice {
        id: lattice
        count: Math.round(root.dots)
    }

    View3D {
        id: view
        anchors.centerIn: parent
        width: Math.min(root.width, root.height)
        height: width
        renderMode: View3D.Inline
        camera: cam
        environment: renderMode === View3D.Offscreen ? bloomEnv : plainEnv

        SceneEnvironment {
            id: plainEnv
            backgroundMode: SceneEnvironment.Transparent
            tonemapMode: SceneEnvironment.TonemapModeNone
        }
        ExtendedSceneEnvironment {
            id: bloomEnv
            backgroundMode: SceneEnvironment.Transparent
            tonemapMode: SceneEnvironment.TonemapModeNone
            glowEnabled: root.bloom
            glowBlendMode: ExtendedSceneEnvironment.GlowBlendMode.Additive
            glowLevel: ExtendedSceneEnvironment.GlowLevel.One | ExtendedSceneEnvironment.GlowLevel.Two
                       | ExtendedSceneEnvironment.GlowLevel.Three
            glowHDRMinimumValue: 0.55
            glowHDRScale: 1.5
            glowStrength: 1.0
            glowIntensity: 0.55 * root.glow
            glowBloom: 0.15
        }

        // Orthographic and axis-aligned: one scene unit is half the view, x right, y up,
        // z toward the viewer, which is what the shaders assume.
        OrthographicCamera {
            id: cam
            z: 10
            clipNear: 1
            clipFar: 20
            horizontalMagnification: view.width / 2
            verticalMagnification: view.height / 2
        }

        CustomMaterial {
            id: latticeMat
            shadingMode: CustomMaterial.Unshaded
            sourceBlend: CustomMaterial.One
            destinationBlend: CustomMaterial.One
            cullMode: Material.NoCulling
            vertexShader: "orb3d/orb3d.vert"
            fragmentShader: "orb3d/orb3d.frag"

            property real t: root.t
            property real flow: root.flow
            property real spin: root.spin
            property real tiltX: root.tiltX
            property real tiltY: root.tiltY
            property real ringSpin: root.ringSpin
            property real band: root.band
            property real dots: Math.round(root.dots)
            property real dotSize: root.dotSize
            property real dim: root.dim
            property real linkAlpha: root.linkAlpha
            property real fire: root.fire
            property real spark: root.spark
            property real glow: root.glow
            property real shell: root.shell
            property real assemble: root.assemble
            property real voice: root.voice
            property real mic: root.mic
            property real wSpeak: root.wSpeak
            property real wHear: root.wHear
            property real wDictate: root.wDictate
            property real wWork: root.wWork
            property real wLoad: root.wLoad
            property real wFail: root.wFail
            property real wError: root.wError
            property real wSleep: root.wSleep
            property real ringAlpha: root.ringAlpha
            property real ringTight: root.ringTight
            property real dispersion: root.dispersion
            property real pxPerUnit: view.width * 0.5 * Screen.devicePixelRatio
        }

        CustomMaterial {
            id: coreMat
            shadingMode: CustomMaterial.Unshaded
            sourceBlend: CustomMaterial.One
            destinationBlend: CustomMaterial.One
            cullMode: Material.NoCulling
            vertexShader: "orb3d/core.vert"
            fragmentShader: "orb3d/core.frag"

            property real coreSpin: root.coreSpin
            property real coreGlow: root.coreGlow
            property real glow: root.glow
            property real assemble: root.assemble
            property real dim: root.dim
        }

        Model {
            geometry: lattice.links
            materials: [latticeMat]
            visible: root.linkAlpha > 0.003
        }
        Model {
            geometry: lattice.dots
            materials: [latticeMat]
        }
        Model {
            geometry: lattice.rings
            materials: [latticeMat]
            visible: root.ringAlpha > 0.003
        }
        // The glass core: a camera-facing square the fragment shader turns into a sphere,
        // which keeps its edge smooth without MSAA. #Rectangle is 100 units across.
        Model {
            source: "#Rectangle"
            scale: Qt.vector3d(0.23 * 1.4 / 50, 0.23 * 1.4 / 50, 1)
            materials: [coreMat]
        }
    }
}
