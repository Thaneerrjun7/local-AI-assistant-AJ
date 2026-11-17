"""The orb and glass lab: Phase 0 of docs/UI_REDESIGN_PLAN.md.

    .venv\\Scripts\\python.exe -m aj.ui.lab               the lab
    .venv\\Scripts\\python.exe -m aj.ui.lab --shots DIR   a screenshot per status into DIR, then exit
    .venv\\Scripts\\python.exe -m aj.ui.lab --bench       frame rate, GPU and CPU per status, then exit
    .venv\\Scripts\\python.exe -m aj.ui.lab --orb 3d ...   the same with the Qt Quick 3D orb

The new look on its own, with no engine, no models and no mic: the orb in every status, the
background warp, the glass buttons and text box, and sliders to tune them. "Save tuning"
writes %APPDATA%\\AJ\\orb_tuning.json, which the real window loads at start (ui/window.py).
The window's content is the real window's own (qml/Face.qml).

Everything that moves runs in Qt's render thread and QML; Python only answers the Save button
and reads the CPU and GPU meters once a second.
"""
import argparse
import os
import sys
import threading
import time
from pathlib import Path

START = time.perf_counter()

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtProperty, pyqtSignal, pyqtSlot  # noqa: E402
from PyQt6.QtGui import QColor, QFont, QGuiApplication  # noqa: E402
from PyQt6.QtQuick import QQuickView  # noqa: E402

from aj.ui.native import (BASE, QML_DIR, TUNING, call, load_font, software_rendering,  # noqa: E402
                          tint_title_bar, windows_prefers_less)

QML = QML_DIR / "Lab.qml"
STATUSES = ["loading", "listening", "hearing", "thinking", "working", "speaking", "dictating",
            "mic off", "mic failed", "asleep", "asleep on battery", "asleep, mic off", "turning off", "error"]


class LabBridge(QObject):
    """What the lab's QML asks of Python: saving the tuning, and the meters."""

    changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._cpu = 0.0
        self._gpu = -1.0
        self.polls: list[float] = []  # when the meters were read (the stutter check lines them up)
        self.gpu_log: list[tuple[float, float]] = []
        try:
            if os.environ.get("AJ_LAB_NO_METERS"):
                raise RuntimeError("turned off by AJ_LAB_NO_METERS")
            from aj.ui.lab_metrics import ProcessMeters
            self._meters = ProcessMeters()
        except Exception as exc:  # the meters are a nicety; the lab works without them
            print(f"meters unavailable: {exc}", flush=True)
            self._meters = None
        # The meters are read on a thread of their own: Windows' performance counters can take
        # hundreds of ms now and then (312 ms measured, 2026-10-23), and on the Qt thread that
        # froze every animation for as long. The Qt thread only shows the latest reading.
        self._stop = threading.Event()
        if self._meters is not None:
            threading.Thread(target=self._read_loop, daemon=True, name="lab meters").start()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.changed.emit)
        self._timer.start(1000)

    def _read_loop(self) -> None:
        while not self._stop.wait(1.0):
            self.polls.append(time.perf_counter())
            self._cpu = self._meters.cpu_percent()
            gpu = self._meters.gpu_percent()
            self._gpu = -1.0 if gpu is None else gpu
            self.gpu_log.append((time.perf_counter(), self._gpu))

    def take_meters(self):
        """The meters, for the bench to read on its own schedule (the readout stops)."""
        self._stop.set()
        self._timer.stop()
        return self._meters

    @pyqtProperty(float, notify=changed)
    def cpu(self) -> float:
        return self._cpu

    @pyqtProperty(float, notify=changed)
    def gpu(self) -> float:
        return self._gpu

    @pyqtSlot(result=str)
    def load(self) -> str:
        try:
            return TUNING.read_text(encoding="utf-8")
        except OSError:
            return ""

    @pyqtSlot(result=bool)
    def enable3d(self) -> bool:
        """The Qt Quick 3D orb (aj/ui/orb3d.py) is only loaded when it's asked for, so the lab
        doesn't pay its startup cost otherwise. False if it can't load."""
        if not getattr(self, "_3d", False):
            started = time.perf_counter()
            try:
                from aj.ui import orb3d
                orb3d.register()
            except Exception as exc:
                print(f"the 3D orb won't load: {exc}", flush=True)
                return False
            self._3d = True
            print(f"3D orb registered in {(time.perf_counter() - started) * 1000:.0f} ms", flush=True)
        return True

    @pyqtSlot(str, result=str)
    def save(self, json_text: str) -> str:
        TUNING.parent.mkdir(parents=True, exist_ok=True)
        TUNING.write_text(json_text, encoding="utf-8")
        return str(TUNING)


def make_view(bridge: LabBridge) -> QQuickView:
    view = QQuickView()
    view.setTitle("AJ · Lab")
    view.setColor(QColor(BASE))
    view.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
    view.rootContext().setContextProperty("lab", bridge)
    view.setSource(QUrl.fromLocalFile(str(QML)))
    if view.status() == QQuickView.Status.Error:
        for error in view.errors():
            print(error.toString(), flush=True)
        raise SystemExit(1)
    root = view.rootObject()
    view.resize(int(root.property("width")), int(root.property("height")))
    return view


def run(args) -> int:
    if os.environ.get("AJ_LAB_LOG_QT"):  # Qt's own messages, stamped with seconds since start
        from PyQt6.QtCore import qInstallMessageHandler
        qInstallMessageHandler(lambda kind, context, message: print(
            f"[{time.perf_counter() - START:7.2f}] {message}", flush=True))
    app = QGuiApplication(sys.argv[:1])
    app.setApplicationName("AJ Lab")
    family = load_font()
    app.setFont(QFont(family, 10))
    bridge = LabBridge()
    view = make_view(bridge)
    root = view.rootObject()
    call(root, "setFont", family)
    reduce_motion, reduce_transparency = windows_prefers_less()
    call(root, "setAccessibility", reduce_motion, reduce_transparency)
    if software_rendering():
        call(root, "setSoftware", True)
    if args.orb == "3d":
        call(root, "setOrbKind", "3d")
    first_frame = {}

    def on_frame():
        if "at" not in first_frame:
            first_frame["at"] = time.perf_counter()
            print(f"first frame {(first_frame['at'] - START) * 1000:.0f} ms after start", flush=True)
            if software_rendering(view):
                call(root, "setSoftware", True)
                print("Qt is drawing without a GPU: no shaders, so plain shapes stand in for the orb, "
                      "the liquid and the glass.", flush=True)
    view.frameSwapped.connect(on_frame)

    if reduce_motion or reduce_transparency:
        print(f"Windows asks for: reduced motion {reduce_motion}, reduced transparency {reduce_transparency}", flush=True)
    view.show()
    tint_title_bar(view, BASE)

    if args.shots:
        _shots(view, root, Path(args.shots))
    elif args.bench:
        _bench(view, root, bridge)
    elif args.bench_parts:
        _bench_parts(view, root, bridge, args.bench_parts)
    elif args.hitches:
        _hitches(view, root, bridge)
    return app.exec()


def _shots(view: QQuickView, root: QObject, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    queue = list(STATUSES)

    def next_status():
        if not queue:
            call(root, "setStatus", "listening")
            QTimer.singleShot(1200, swipe)
            return
        status = queue.pop(0)
        call(root, "setStatus", status)
        QTimer.singleShot(2200, lambda: grab(status))

    def save(name):
        path = out / f"{name}.png"
        view.grabWindow().save(str(path))
        print(f"saved {path}", flush=True)

    def grab(status):
        save(f"{STATUSES.index(status):02d}_{status.replace(' ', '_')}")
        next_status()

    # Then the liquid: a quick swipe across the window (real mouse events, through Qt), shot
    # while its ripples spread; then the pointer resting on the mic button.
    def swipe():
        from PyQt6.QtCore import QPoint
        from PyQt6.QtTest import QTest
        path = [(30 + i * 12, 560 + int(40 * __import__("math").sin(i * 0.3))) for i in range(34)]
        for i, (x, y) in enumerate(path):
            QTimer.singleShot(i * 14, lambda x=x, y=y: QTest.mouseMove(view, QPoint(x, y)))
        QTimer.singleShot(len(path) * 14 + 260, lambda: save("12_liquid_swipe"))
        mic = (210, 410)
        QTimer.singleShot(len(path) * 14 + 2900, lambda: QTest.mouseMove(view, QPoint(*mic)))
        QTimer.singleShot(len(path) * 14 + 2960, lambda: QTest.mouseMove(view, QPoint(mic[0] + 14, mic[1] - 12)))
        QTimer.singleShot(len(path) * 14 + 3700, lambda: (save("13_hover_mic"), os._exit(0)))

    QTimer.singleShot(2800, next_status)  # let loading finish its first assembly
    QTimer.singleShot(120_000, lambda: os._exit(2))


def _bench(view: QQuickView, root: QObject, bridge: LabBridge) -> None:
    """Each status for 3 s at the screen's own rate: frames per second, how busy the GPU's 3D
    engine was with this window, and what that makes one frame cost; and Python's CPU use.
    (With vsync off Qt paces animation on a 16 ms timer, so "uncapped" only ever showed 60.)"""
    meters = bridge.take_meters()
    queue = list(STATUSES)
    results = []
    state = {}

    def frames() -> int:
        return int(root.property("frameCount"))

    def next_status():
        if not queue:
            print("\nstatus          fps   GPU busy   GPU ms/frame   CPU (one core)", flush=True)
            for status, fps, gpu, cpu in results:
                ms = "   n/a" if gpu is None else f"{gpu / 100 * 1000 / max(fps, 1):6.2f}"
                busy = "  n/a" if gpu is None else f"{gpu:5.1f}%"
                print(f"{status:<14} {fps:5.0f}   {busy}     {ms}        {cpu:5.1f}%", flush=True)
            os._exit(0)
        status = queue.pop(0)
        call(root, "setStatus", status)
        QTimer.singleShot(800, lambda: begin(status))  # let the blend from the last status settle

    def begin(status):
        if meters is not None:
            meters.cpu_percent()
            meters.gpu_percent()
        state.update(f0=frames(), t0=time.perf_counter(), gpu=[])
        for k in (1, 2, 3):
            QTimer.singleShot(1000 * k, sample)
        QTimer.singleShot(3050, lambda: end(status))

    def sample():
        reading = meters.gpu_percent() if meters is not None else None
        if reading is not None:
            state["gpu"].append(reading)

    def end(status):
        fps = (frames() - state["f0"]) / (time.perf_counter() - state["t0"])
        gpu = sum(state["gpu"]) / len(state["gpu"]) if state["gpu"] else None
        cpu = meters.cpu_percent() if meters is not None else 0.0
        results.append((status, fps, gpu, cpu))
        next_status()

    QTimer.singleShot(1500, next_status)
    QTimer.singleShot(120_000, lambda: os._exit(2))


def _hitches(view: QQuickView, root: QObject, bridge: LabBridge, seconds: float = 20.0) -> None:
    """The stutter check: the pointer circles over the orb and the glass for `seconds` (real
    mouse events through Qt), and every frame slower than 25 ms is listed with how long it took
    and whether the meters had just been read."""
    import math
    from PyQt6.QtCore import QPoint
    from PyQt6.QtTest import QTest
    call(root, "setStatus", "listening")
    state = {"t0": 0.0, "seen": 0, "log": []}

    bursts = os.environ.get("AJ_LAB_BURSTS")  # move 0.7 s, rest 2 s, like a hand on a mouse

    def move():
        t = time.perf_counter() - state["t0"]
        if bursts and t % 2.7 > 0.7:
            return
        a = t * 2 * math.pi / 1.5
        QTest.mouseMove(view, QPoint(int(210 + 150 * math.cos(a)), int(420 + 110 * math.sin(a))))

    def watch():
        now = time.perf_counter()
        frames, drops = int(root.property("frameCount")), int(root.property("dropCount"))
        if now - state["bucket_at"] >= 0.25:
            span = now - state["bucket_at"]
            state["buckets"].append((now - state["t0"], (frames - state["bucket_f"]) / span, drops - state["bucket_d"]))
            state.update(bucket_at=now, bucket_f=frames, bucket_d=drops)
        count = int(root.property("hitchCount"))
        if count > state["seen"]:
            now = time.perf_counter()
            near = any(0 <= now - p < 0.15 for p in bridge.polls)
            state["log"].append((now - state["t0"], count - state["seen"], float(root.property("lastHitchMs")), near))
            state["seen"] = count

    def finish():
        mover.stop()
        watcher.stop()
        frames = int(root.property("frameCount")) - state["f0"]
        print(f"\n{frames / seconds:.0f} fps over {seconds:.0f} s of pointer motion; "
              f"{sum(n for _, n, _, _ in state['log'])} frames over 25 ms", flush=True)
        for at, n, ms, near in state["log"]:
            print(f"  {at:6.2f} s  {n} slow, worst {ms:5.1f} ms{'   (meters read just before)' if near else ''}", flush=True)
        print("frame rate per quarter second (and frames that missed a refresh):", flush=True)
        line = []
        for at, fps, drops in state["buckets"]:
            line.append(f"{at:5.2f}s {fps:3.0f}{'*' * min(drops, 9)}")
            if len(line) == 6:
                print("  " + "   ".join(line), flush=True)
                line = []
        if line:
            print("  " + "   ".join(line), flush=True)
        polls = [p - state["t0"] for p in bridge.polls if p >= state["t0"]]
        print("meters read at: " + ", ".join(f"{p:.2f}" for p in polls[:25]), flush=True)
        print("GPU busy each second: " + ", ".join(f"{g:.0f}%" for at, g in bridge.gpu_log if at >= state["t0"]), flush=True)
        os._exit(0)

    def start():
        state["t0"] = time.perf_counter()
        print(f"[{state['t0'] - START:7.2f}] pointer motion starts", flush=True)
        state["seen"] = int(root.property("hitchCount"))
        state["f0"] = int(root.property("frameCount"))
        state.update(buckets=[], bucket_at=state["t0"], bucket_f=state["f0"], bucket_d=int(root.property("dropCount")))
        mover.start(8)
        watcher.start(50)
        QTimer.singleShot(int(seconds * 1000), finish)

    mover = QTimer()
    mover.timeout.connect(move)
    watcher = QTimer()
    watcher.timeout.connect(watch)
    view._keep = (mover, watcher)
    QTimer.singleShot(3500, start)  # past loading


PARTS = [("everything", [], False), ("everything, no caching", [], True), ("without the orb", ["orb"], False),
         ("without the glass", ["glass"], False), ("without the background", ["background"], False),
         ("the orb alone", ["glass", "background"], False), ("nothing", ["orb", "glass", "background"], False)]


def _bench_parts(view: QQuickView, root: QObject, bridge: LabBridge, status: str) -> None:
    """What each layer costs: the same measurement as --bench, in one status, with parts hidden."""
    meters = bridge.take_meters()
    queue = list(PARTS)
    results = []
    state = {}
    call(root, "setStatus", status)

    def next_part():
        if not queue:
            print(f"\n{status}: {'':24} fps   GPU ms/frame   GPU busy", flush=True)
            for name, fps, gpu in results:
                ms = "   n/a" if gpu is None else f"{gpu / 100 * 1000 / max(fps, 1):6.2f}"
                print(f"  {name:<32} {fps:4.0f}   {ms}       {gpu or 0:5.1f}%", flush=True)
            os._exit(0)
        name, hidden, no_cache = queue.pop(0)
        call(root, "setHidden", hidden, no_cache)
        QTimer.singleShot(700, lambda: begin(name))

    def begin(name):
        if meters is not None:
            meters.gpu_percent()
        state.update(f0=int(root.property("frameCount")), t0=time.perf_counter(), gpu=[])
        for k in (1, 2, 3):
            QTimer.singleShot(1000 * k, lambda: state["gpu"].append(meters.gpu_percent() if meters else None))
        QTimer.singleShot(3050, lambda: end(name))

    def end(name):
        fps = (int(root.property("frameCount")) - state["f0"]) / (time.perf_counter() - state["t0"])
        readings = [g for g in state["gpu"] if g is not None]
        results.append((name, fps, sum(readings) / len(readings) if readings else None))
        next_part()

    QTimer.singleShot(3000, next_part)
    QTimer.singleShot(120_000, lambda: os._exit(2))


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m aj.ui.lab")
    parser.add_argument("--shots", metavar="DIR", help="save a screenshot per status, then exit")
    parser.add_argument("--bench", action="store_true", help="frame rate, GPU and CPU per status, then exit")
    parser.add_argument("--bench-parts", metavar="STATUS", help="what the orb, glass and background each cost")
    parser.add_argument("--hitches", action="store_true", help="list slow frames while the pointer moves")
    parser.add_argument("--orb", choices=["shader", "3d"], default="shader", help="which orb to draw")
    return run(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
