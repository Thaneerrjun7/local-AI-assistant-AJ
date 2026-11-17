"""The orb and glass lab (aj/ui/lab.py, Phase 0 of docs/UI_REDESIGN_PLAN.md), offscreen: its
QML loads with no errors, every status it offers has a look and a label, and each shader pack
carries Direct3D bytecode, so nothing is compiled when the window first draws (the glass alone
took 857 ms to compile at run time, 2026-10-23)."""
import os
import unittest
import zlib
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"  # before any QApplication exists: never show a real window

try:
    from PyQt6.QtCore import QUrl, qInstallMessageHandler
    from PyQt6.QtQml import QJSEngine
    from PyQt6.QtQuick import QQuickView
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - the window is optional for the console test modes
    QApplication = None

UI = Path(__file__).resolve().parent.parent / "aj" / "ui"
QML = UI / "qml"
SHADERS = UI / "shaders"
# What a broken binding or script prints; the offscreen platform's missing-fonts note isn't one.
_PROBLEMS = ("Error", "TypeError", "ReferenceError", "is not a function", "Cannot assign",
             "Unable to assign", "Binding loop", "binding loop", "is not defined")


def orb_states():
    """OrbStates.js evaluated as plain JavaScript: (order, looks, labels, defaults)."""
    engine = QJSEngine()
    source = (QML / "OrbStates.js").read_text(encoding="utf-8").replace(".pragma library", "")
    result = engine.evaluate(source + "\n;({order: order, looks: looks, labels: labels, defaults: defaults, "
                                      "thinking: targets('thinking'), listening: targets('listening')})")
    if result.isError():
        raise AssertionError(result.toString())
    return result.toVariant()


@unittest.skipIf(QApplication is None, "PyQt6 is not installed")
class LabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_the_lab_loads_and_runs_without_qml_errors(self):
        messages = []
        qInstallMessageHandler(lambda kind, context, message: messages.append(message))
        try:
            view = QQuickView()
            view.setSource(QUrl.fromLocalFile(str(QML / "Lab.qml")))
            self.assertEqual(view.status(), QQuickView.Status.Ready, [e.toString() for e in view.errors()])
            view.show()
            QTest.qWait(600)  # let the clock tick, the springs move and the loading status run
            view.close()
        finally:
            qInstallMessageHandler(None)
        problems = [m for m in messages if any(p in m for p in _PROBLEMS)]
        self.assertEqual(problems, [])

    def test_a_spring_destroyed_mid_flight_doesnt_stop_the_others(self):
        """The review of 2026-10-23: a dead spring left in the clock's list threw every frame,
        and with it every animation in the window stopped."""
        from PyQt6.QtCore import QByteArray
        from PyQt6.QtQml import QQmlComponent, QQmlEngine
        engine = QQmlEngine()
        component = QQmlComponent(engine)
        component.setData(QByteArray(b"""
            import QtQuick
            Item {
                id: root
                property real kept: survivor.value
                Spring { id: survivor; response: 0.15 }
                Component { id: maker; Spring { response: 2.0 } }
                function start() {
                    const doomed = maker.createObject(root)
                    doomed.target = 1
                    doomed.destroy()
                    survivor.target = 1
                }
            }"""), QUrl.fromLocalFile(str(QML / "SpringProbe.qml")))  # the URL puts Spring and Motion in scope
        messages = []
        qInstallMessageHandler(lambda kind, context, message: messages.append(message))
        try:
            item = component.create()
            self.assertIsNotNone(item, component.errorString())
            QTest.qWait(50)
            from PyQt6.QtCore import QMetaObject
            QMetaObject.invokeMethod(item, "start")
            QTest.qWait(700)
            self.assertAlmostEqual(item.property("kept"), 1.0, places=2)
        finally:
            qInstallMessageHandler(None)
        self.assertEqual([m for m in messages if "TypeError" in m], [])

    def test_every_status_has_a_look_and_a_label(self):
        states = orb_states()
        for status in states["order"]:
            self.assertIn(status, states["looks"], status)
            self.assertIn(status, states["labels"], status)

    def test_every_status_the_engine_reports_has_a_look(self):
        """The window hands the orb the engine's Status by its value; one without a look would
        quietly fall back to listening's."""
        from aj.core.pipeline import Status
        states = orb_states()
        for status in Status:
            self.assertIn(status.value, states["looks"], status)
        self.assertIn("working", states["looks"])  # a tool running (Phase 2)
        self.assertIn("error", states["looks"])    # the engine died

    def test_looks_only_set_values_the_orb_has(self):
        states = orb_states()
        known = set(states["defaults"])
        for status, look in states["looks"].items():
            self.assertLessEqual(set(look), known, f"{status}: a typo in a key would be ignored silently")

    def test_a_status_is_its_own_values_over_the_defaults(self):
        states = orb_states()
        self.assertEqual(states["thinking"]["fire"], 1)
        self.assertEqual(states["thinking"]["shell"], states["defaults"]["shell"])
        self.assertEqual(states["listening"], states["defaults"])


ROOT = UI.parent.parent
QSB = ROOT / ".venv-shaders" / "Lib" / "site-packages" / "PySide6" / "qsb.exe"


class ShaderPackTests(unittest.TestCase):
    @unittest.skipUnless(QSB.exists(), "the shader tools venv isn't set up (see scripts/build_shaders.py)")
    def test_every_pack_is_built_from_the_current_source(self):
        """A shader edited without running build_shaders.py would leave the old one in the window."""
        import subprocess
        import sys
        import tempfile
        sys.path.insert(0, str(ROOT / "scripts"))
        from build_shaders import TARGETS, expand
        for source in sorted(SHADERS.glob("*.frag")):
            with tempfile.TemporaryDirectory() as tmp:
                expanded = Path(tmp) / source.name
                expanded.write_text(expand(source), encoding="utf-8")
                fresh = Path(tmp) / "fresh.qsb"
                subprocess.run([str(QSB), *TARGETS, "-o", str(fresh), str(expanded)], check=True, capture_output=True)
                spirv = []
                for pack in (fresh, source.with_name(source.name + ".qsb")):
                    out = Path(tmp) / (pack.stem + ".spv")
                    subprocess.run([str(QSB), "-x", "spirv,100", "-o", str(out), str(pack)], check=True, capture_output=True)
                    spirv.append(out.read_bytes())
                self.assertEqual(spirv[0], spirv[1], f"{source.name} changed: run scripts\\build_shaders.py")

    def test_every_shader_has_a_pack_with_direct3d_bytecode(self):
        sources = sorted(SHADERS.glob("*.frag"))
        self.assertTrue(sources)
        for source in sources:
            pack = source.with_name(source.name + ".qsb")
            self.assertTrue(pack.exists(), f"{pack.name} missing: run scripts\\build_shaders.py")
            data = zlib.decompress(pack.read_bytes()[4:])  # a .qsb is qCompress'd: 4-byte size, then zlib
            self.assertIn(b"DXBC", data, f"{pack.name} has no DXBC: run scripts\\build_shaders.py")


if __name__ == "__main__":
    unittest.main()
