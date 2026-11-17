"""The neural orb drawn with Qt Quick 3D, the second candidate in the Phase 0 bake-off.

    .venv\\Scripts\\python.exe -m aj.ui.orb3d [--shots DIR] [--bench] [--mode inline]

The dots are real 3D points: a mesh built once here (numpy, ~10 ms) and moved entirely in
the vertex shader (qml/orb3d/orb3d.vert), so Python does nothing per frame. It is rebuilt
only when the dot count changes.

PyQt6 6.11 does not wrap QQuick3DInstancing (only QQuick3DGeometry, QQuick3DObject and
QQuick3DTextureData), so the "instance table" is a QQuick3DGeometry instead: four vertices
per dot that all carry the dot's lattice position, index and hash, and the vertex shader
spreads them into a camera-facing quad. One draw call either way, and no per-frame upload.

  --shots DIR   a PNG per status after 2 s in it, then exit
  --bench       vsync off, frames per second (and GPU ms per frame) for each status, and the
                time from process start to the first frame
  --mode        inline (default), underlay or offscreen: the View3D renderMode
  --no-glow     offscreen only: leave the bloom pass out
  --no-cache    start without the pipeline cache, as on a first launch
"""
import argparse
import math
import os
import random
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from PyQt6 import sip
from PyQt6.QtCore import (Q_ARG, QMetaObject, QObject, QTimer, QUrl, Qt, pyqtProperty,
                          pyqtSignal)
from PyQt6.QtGui import QGuiApplication, QSurfaceFormat, QVector3D
from PyQt6.QtQml import QQmlApplicationEngine, QQmlParserStatus, qmlRegisterType
# Imported for its side effect: without it PyQt6 hands back the QML Window as a bare QWindow,
# and calling grabWindow() on that overflows the stack.
from PyQt6.QtQuick import QQuickWindow  # noqa: F401
from PyQt6.QtQuick3D import QQuick3DGeometry

_T0 = time.perf_counter()
QML_DIR = Path(__file__).resolve().parent / "qml"

STATUSES = ["loading", "listening", "hearing", "thinking", "working", "speaking",
            "dictating", "mic_off", "mic_failed", "asleep", "turning_off", "error"]
VOICED = {"hearing", "speaking", "dictating"}

_STRIDE = 14 * 4  # position 3, normal 3 (a link's far end), uv0 2, uv1 2 (kind), colour 4 (ids)
_RINGS, _RING_DOTS = 3, 120
_FIBS = np.array([1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144, 233, 377, 610, 987, 1597, 2584, 4181])


def pcg_hash(i: np.ndarray) -> np.ndarray:
    """The shaders' PCG hash of integer i, as 0..1, so CPU and GPU agree on every dot."""
    v = i.astype(np.uint32)
    s = v * np.uint32(747796405) + np.uint32(2891336453)
    w = ((s >> ((s >> np.uint32(28)) + np.uint32(4))) ^ s) * np.uint32(277803737)
    return ((w >> np.uint32(22)) ^ w).astype(np.float64) / 4294967295.0


def fibonacci_sphere(n: int) -> np.ndarray:
    """n evenly spread unit vectors, y up (the spin axis), no bunching at the poles."""
    i = np.arange(n, dtype=np.float64)
    y = 1.0 - (2.0 * i + 1.0) / n
    r = np.sqrt(np.maximum(0.0, 1.0 - y * y))
    phi = 2.0 * math.pi * np.mod(i * (math.sqrt(5.0) - 1.0) / 2.0, 1.0)
    return np.stack([r * np.cos(phi), y, r * np.sin(phi)], axis=1)


def _links(q: np.ndarray, per_dot: int = 3) -> np.ndarray:
    """Each dot's few nearest neighbours as (i, j) pairs. On a Fibonacci lattice they sit at
    Fibonacci-number index offsets, so 36 candidates a dot is enough and no n² search."""
    n = len(q)
    offsets = np.concatenate([_FIBS, -_FIBS])
    i = np.arange(n)[:, None]
    j = i + offsets[None, :]
    ok = (j >= 0) & (j < n)
    jc = np.clip(j, 0, n - 1)
    d = np.linalg.norm(q[jc] - q[:, None, :], axis=2)
    d[~ok] = np.inf
    near = np.argsort(d, axis=1)[:, :per_dot]
    a = np.repeat(np.arange(n), per_dot)
    b = jc[np.arange(n)[:, None], near].ravel()
    dist = d[np.arange(n)[:, None], near].ravel()
    keep = np.isfinite(dist) & (dist < 1.6 * math.sqrt(4.0 * math.pi / n))
    pairs = np.unique(np.sort(np.stack([a[keep], b[keep]], axis=1), axis=1), axis=0)
    return pairs


def _quads(n: int) -> np.ndarray:
    base = np.arange(n, dtype=np.uint32)[:, None] * 4
    return (base + np.array([0, 1, 2, 0, 2, 3], dtype=np.uint32)).ravel()


def build_mesh(part: str, count: int) -> tuple[bytes, bytes]:
    """Vertex and index bytes for the dots, the synapse links or the rings."""
    if part == "rings":
        k = np.repeat(np.arange(_RINGS), _RING_DOTS).astype(np.float64)
        u = np.tile(np.arange(_RING_DOTS) / _RING_DOTS, _RINGS)
        n = len(k)
        v = np.zeros((n, 4, 14))
        v[:, :, 0:3] = np.stack([np.cos(u * 2 * math.pi), 0 * u, np.sin(u * 2 * math.pi)], 1)[:, None]
        v[:, :, 6:8] = [[-1, -1], [1, -1], [1, 1], [-1, 1]]
        v[:, :, 8] = 1.0
        v[:, :, 10:14] = np.stack([k, u, pcg_hash(np.arange(n) + 9000), 0 * k], 1)[:, None]
        return v.astype(np.float32).tobytes(), _quads(n).tobytes()

    q = fibonacci_sphere(count)
    idx = np.arange(count)
    h = pcg_hash(idx)
    if part == "links":
        pairs = _links(q)
        a, b = pairs[:, 0], pairs[:, 1]
        n = len(pairs)
        v = np.zeros((n, 4, 14))
        # Two vertices at each end; the shader pushes them apart across the line, and the
        # far end's side is mirrored because its "other end" points back the other way.
        ends = [(a, b, 1.0), (a, b, -1.0), (b, a, -1.0), (b, a, 1.0)]
        for c, (me, other, side) in enumerate(ends):
            v[:, c, 0:3] = q[me]
            v[:, c, 3:6] = q[other]
            v[:, c, 7] = side
            v[:, c, 8] = 2.0
            v[:, c, 10:14] = np.stack([me, h[me], other, h[other]], 1)
        quads = np.arange(n, dtype=np.uint32)[:, None] * 4
        ind = (quads + np.array([0, 1, 2, 1, 3, 2], dtype=np.uint32)).ravel()
        return v.astype(np.float32).tobytes(), ind.tobytes()

    v = np.zeros((count, 4, 14))
    v[:, :, 0:3] = q[:, None]
    v[:, :, 3:6] = q[:, None]
    v[:, :, 6:8] = [[-1, -1], [1, -1], [1, 1], [-1, 1]]
    v[:, :, 10:14] = np.stack([idx, h, 0 * h, 0 * h], 1)[:, None]
    return v.astype(np.float32).tobytes(), _quads(count).tobytes()


class LatticeMesh(QQuick3DGeometry):
    """One part of the orb ("dots", "links" or "rings") as a static mesh."""

    def __init__(self, part: str, parent: QObject | None = None):
        super().__init__(parent)
        self.part = part

    def build(self, count: int) -> None:
        vertices, indices = build_mesh(self.part, count)
        A = QQuick3DGeometry.Attribute
        self.clear()
        self.setStride(_STRIDE)
        self.setPrimitiveType(QQuick3DGeometry.PrimitiveType.Triangles)
        self.addAttribute(A.Semantic.PositionSemantic, 0, A.ComponentType.F32Type)
        self.addAttribute(A.Semantic.NormalSemantic, 12, A.ComponentType.F32Type)
        self.addAttribute(A.Semantic.TexCoordSemantic, 24, A.ComponentType.F32Type)
        self.addAttribute(A.Semantic.TexCoord1Semantic, 32, A.ComponentType.F32Type)
        self.addAttribute(A.Semantic.ColorSemantic, 40, A.ComponentType.F32Type)
        self.addAttribute(A.Semantic.IndexSemantic, 0, A.ComponentType.U32Type)
        self.setVertexData(vertices)
        self.setIndexData(indices)
        # The shader moves dots up to ~1.2 x their radius and rings reach 0.95: keep culling off them.
        self.setBounds(QVector3D(-2.0, -2.0, -2.0), QVector3D(2.0, 2.0, 2.0))
        self.update()


class FibonacciLattice(QObject, QQmlParserStatus):
    """The orb's three meshes, for QML: Orb3D.qml binds each Model's geometry to dots, links
    or rings. A plain QObject because PyQt6 crashes when QML itself creates a
    QQuick3DGeometry type inside a Model; geometries made here in Python are fine. Built
    once on componentComplete, and again only when count changes."""

    countChanged = pyqtSignal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._count = 1200
        self._ready = False
        # No QObject parent: giving them one while QML is creating this object overflows the
        # stack in PyQt6. They live as long as this object holds them.
        self._dots = LatticeMesh("dots")
        self._links = LatticeMesh("links")
        self._rings = LatticeMesh("rings")

    def classBegin(self) -> None:
        pass

    def componentComplete(self) -> None:
        self._ready = True
        self._dots.build(self._count)
        self._links.build(self._count)
        self._rings.build(0)

    @pyqtProperty(int, notify=countChanged)
    def count(self) -> int:
        return self._count

    @count.setter
    def count(self, n: int) -> None:
        n = max(16, min(int(n), 6000))
        if n == self._count:
            return
        self._count = n
        self.countChanged.emit()
        if self._ready:
            self._dots.build(n)
            self._links.build(n)

    @pyqtProperty(QQuick3DGeometry, constant=True)
    def dots(self) -> QQuick3DGeometry:
        return self._dots

    @pyqtProperty(QQuick3DGeometry, constant=True)
    def links(self) -> QQuick3DGeometry:
        return self._links

    @pyqtProperty(QQuick3DGeometry, constant=True)
    def rings(self) -> QQuick3DGeometry:
        return self._rings


def register() -> None:
    """Make `import AJ3D 1.0` and its FibonacciLattice type available to QML."""
    qmlRegisterType(FibonacciLattice, "AJ3D", 1, 0, "FibonacciLattice")


# The runner: the orb on the ember background, cycling statuses with a fake voice level.
# Each status is a set of targets the orb springs to; t, flow, spin and the other clocks
# are integrated here from per-status rates, as the lab will do.
_RUNNER_QML = """
import QtQuick
import QtQuick3D

Window {
    id: win
    width: 420; height: 520
    color: "#1a0806"
    visible: false
    title: "AJ orb, Qt Quick 3D"

    property string status: "loading"
    property real level: 0          // the fake voice, set 30 times a second from Python
    property bool glowOn: %GLOW%
    property real since: 0          // seconds in this status

    Orb3D {
        id: orb
        x: 10; y: 16; width: 400; height: 400
        renderMode: View3D.%MODE%
        bloom: win.glowOn
    }
    Text {
        anchors.horizontalCenter: parent.horizontalCenter
        y: 452
        color: "#8a8a92"
        font.pixelSize: 15
        text: win.status.replace("_", " ")
    }

    readonly property var base: ({
        dim: 0.9, linkAlpha: 0.10, fire: 0.0, spark: 0.002, glow: 0.7, shell: 1.0,
        coreGlow: 0.4, ringAlpha: 0.16, ringTight: 0.0, dispersion: 0.0, dotSize: 0.16,
        wSpeak: 0, wHear: 0, wDictate: 0, wWork: 0, wLoad: 0, wFail: 0, wError: 0, wSleep: 0,
        flowRate: 0.6, spinRate: 0.10, tiltRate: 0.0, coreSpinRate: 0.3, ringSpinRate: 0.15,
        bandRate: 0.0
    })
    readonly property var looks: ({
        loading:     { wLoad: 1, dim: 0.85, spark: 0.004, coreGlow: 0.7, ringAlpha: 0.22,
                       flowRate: 1.0, spinRate: 0.35, coreSpinRate: 1.0, linkAlpha: 0.04 },
        listening:   { dim: 0.85, spark: 0.0015, spinRate: 0.08 },
        hearing:     { wHear: 1, dim: 0.95, spark: 0.003, coreGlow: 0.55, linkAlpha: 0.12 },
        thinking:    { fire: 1, spark: 0.012, flowRate: 1.5, spinRate: 0.45, coreSpinRate: 2.2,
                       coreGlow: 1.1, linkAlpha: 0.28, glow: 1.1, ringAlpha: 0.3,
                       ringSpinRate: 0.5 },
        working:     { wWork: 1, ringTight: 1, ringAlpha: 0.75, ringSpinRate: 1.6, bandRate: 1.2,
                       fire: 0.25, flowRate: 1.0, spinRate: 0.2, coreGlow: 0.9,
                       coreSpinRate: 1.5, linkAlpha: 0.06 },
        speaking:    { wSpeak: 1, dim: 1.0, spark: 0.004, coreGlow: 0.5,
                       spinRate: 0.12, flowRate: 0.9, linkAlpha: 0.14 },
        dictating:   { wDictate: 1, dim: 0.95, coreGlow: 0.3, spinRate: 0.06, linkAlpha: 0.05 },
        mic_off:     { shell: 0.94, dim: 0.35, spark: 0, spinRate: 0, flowRate: 0,
                       coreSpinRate: 0, ringSpinRate: 0, coreGlow: 0.15, ringAlpha: 0.06,
                       linkAlpha: 0.04, glow: 0.3 },
        mic_failed:  { wFail: 1, dim: 0.8, spark: 0, spinRate: 0.05, coreGlow: 0.3 },
        asleep:      { wSleep: 1, dim: 0.3, spinRate: 0.02, flowRate: 0.2, spark: 0.0005,
                       coreGlow: 0.2, ringAlpha: 0.05, glow: 0.3, linkAlpha: 0.03 },
        turning_off: { dim: 0.6, coreGlow: 0.6, spinRate: 0.3, ringAlpha: 0.0, spark: 0,
                       linkAlpha: 0.04 },
        error:       { wError: 1, spinRate: 0, flowRate: 0, coreSpinRate: 0, ringSpinRate: 0,
                       dispersion: 0.5, dim: 0.6, coreGlow: 0.2, linkAlpha: 0.03,
                       ringAlpha: 0.08, spark: 0 }
    })
    readonly property var direct: ["dim", "linkAlpha", "fire", "spark", "glow", "shell",
        "coreGlow", "ringAlpha", "ringTight", "dispersion", "dotSize", "wSpeak", "wHear",
        "wDictate", "wWork", "wLoad", "wFail", "wError", "wSleep"]
    property var cur: ({})

    onStatusChanged: {
        since = 0
        if (status === "loading")
            orb.assemble = 0
    }

    FrameAnimation {
        running: !%DRIVEN%   // the bench calls step() itself, once per swapped frame
        onTriggered: win.step(Math.min(frameTime, 0.05))
    }

    function follow(x, target, dt) {
        // Rises in ~15 ms, falls in ~120 ms: follows syllables without jitter.
        const tau = target > x ? 0.015 : 0.12
        return x + (target - x) * (1 - Math.exp(-dt / tau))
    }

    function step(dt) {
        since += dt
        const look = Object.assign({}, base, looks[status] || {})
        const k = 1 - Math.exp(-dt * 4.0)
        for (const key in look)
            cur[key] = cur[key] === undefined ? look[key] : cur[key] + (look[key] - cur[key]) * k
        orb.t += dt
        orb.flow += cur.flowRate * dt
        orb.spin += cur.spinRate * dt
        orb.coreSpin += cur.coreSpinRate * dt
        orb.ringSpin += cur.ringSpinRate * dt
        orb.band += cur.bandRate * dt
        for (const key of direct)
            orb[key] = cur[key]
        orb.voice = follow(orb.voice, status === "speaking" ? level : 0, dt)
        orb.mic = follow(orb.mic, status === "hearing" || status === "dictating" ? level : 0, dt)
        if (status === "speaking")
            orb.coreGlow = cur.coreGlow + 0.7 * orb.voice
        else if (status === "dictating")  // blinks like a text cursor
            orb.coreGlow = cur.coreGlow + 0.8 * (Math.sin(orb.t * Math.PI * 1.6) > 0 ? 1 : 0)
        else if (status === "mic_failed")  // a slow pulse every 2 s, no flashing alarm
            orb.coreGlow = cur.coreGlow + 0.8 * Math.pow(0.5 + 0.5 * Math.cos(orb.t * Math.PI), 6)
        if (status === "loading")
            orb.assemble = Math.min(1, orb.assemble + dt * 0.45)
        else if (status === "turning_off")
            orb.assemble = Math.max(0, orb.assemble - dt * 0.45)
        else
            orb.assemble = Math.min(1, orb.assemble + dt * 1.5)
    }
}
"""


class Syllables:
    """A fake voice level: syllables of 120-250 ms at 0.4-1.0 with short gaps between them,
    and a ~0.7 s pause after every 5-12 (a sentence)."""

    def __init__(self, seed: int = 7):
        self.rng = random.Random(seed)
        self.until = time.perf_counter()
        self.on = False
        self.value = 0.0
        self.started = self.until
        self.left = self.rng.randint(5, 12)

    def level(self, now: float) -> float:
        while now >= self.until:
            r = self.rng
            if self.on:
                self.on, self.value = False, 0.0
                self.left -= 1
                if self.left <= 0:
                    self.left = r.randint(5, 12)
                    self.until += r.uniform(0.55, 0.85)
                else:
                    self.until += r.uniform(0.04, 0.10)
            else:
                self.on, self.value = True, r.uniform(0.4, 1.0)
                self.started = self.until
                self.until += r.uniform(0.12, 0.25)
        return self.value


def _since_process_start() -> float:
    """Seconds since Windows created this process, interpreter start-up included (since this
    module was imported, elsewhere)."""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
    except (AttributeError, OSError):
        return time.perf_counter() - _T0
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    times = [wintypes.FILETIME() for _ in range(4)]
    now = wintypes.FILETIME()
    if not k32.GetProcessTimes(k32.GetCurrentProcess(), *(ctypes.byref(f) for f in times)):
        return time.perf_counter() - _T0
    k32.GetSystemTimePreciseAsFileTime(ctypes.byref(now))

    def ticks(f):
        return (f.dwHighDateTime << 32) | f.dwLowDateTime
    return (ticks(now) - ticks(times[0])) / 1e7


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m aj.ui.orb3d")
    ap.add_argument("--shots", metavar="DIR", help="save a PNG per status, then exit")
    ap.add_argument("--bench", action="store_true", help="vsync off; print fps per status")
    ap.add_argument("--mode", choices=["inline", "underlay", "offscreen"], default="inline")
    ap.add_argument("--no-glow", action="store_true", help="offscreen: leave the bloom pass out")
    ap.add_argument("--no-cache", action="store_true", help="no pipeline cache: a cold start")
    args = ap.parse_args(argv)

    if args.bench:  # must be the default before any window exists
        fmt = QSurfaceFormat.defaultFormat()
        fmt.setSwapInterval(0)
        QSurfaceFormat.setDefaultFormat(fmt)

    app = QGuiApplication(sys.argv[:1])
    register()
    engine = QQmlApplicationEngine()
    qml = (_RUNNER_QML.replace("%MODE%", args.mode.capitalize())
           .replace("%GLOW%", "false" if args.no_glow else "true")
           .replace("%DRIVEN%", "true" if args.bench else "false"))
    engine.loadData(qml.encode(), QUrl.fromLocalFile(str(QML_DIR / "orb3d_runner.qml")))
    if not engine.rootObjects():
        print("QML failed to load", file=sys.stderr)
        return 1
    win = engine.rootObjects()[0]

    # Quick 3D generates and compiles its shaders at run time (~0.3-0.7 s on the first frame);
    # the pipeline cache keeps the compiled ones for the next start.
    # Qt keeps one of its own too (%LOCALAPPDATA%\<app>\cache), so --no-cache turns that off.
    cache = Path(tempfile.gettempdir()) / f"aj_orb3d_{args.mode}.pipelinecache"
    warm = cache.exists() and not args.no_cache
    cfg = win.graphicsConfiguration()
    if args.no_cache:
        cfg.setAutomaticPipelineCache(False)
    else:
        cfg.setPipelineCacheSaveFile(str(cache))
        if warm:
            cfg.setPipelineCacheLoadFile(str(cache))
    win.setGraphicsConfiguration(cfg)

    first = []
    frames = [0]

    def swapped():
        if not first:
            first.append(_since_process_start())
        frames[0] += 1

    win.frameSwapped.connect(swapped, Qt.ConnectionType.DirectConnection)

    meters = None
    if args.bench:
        # With vsync off the animation driver still ticks only once per refresh, so a frame
        # it did not tick would redraw an unchanged scene (and Offscreen would skip the 3D
        # pass). Instead each swapped frame advances the orb, which asks for the next frame.
        last = [time.perf_counter()]

        def drive():
            now = time.perf_counter()
            QMetaObject.invokeMethod(win, "step", Q_ARG("QVariant", min(now - last[0], 0.05)))
            last[0] = now

        win.frameSwapped.connect(drive, Qt.ConnectionType.QueuedConnection)
        try:  # the lab's GPU meter, if it is there: GPU busy share -> ms per frame
            from aj.ui.lab_metrics import ProcessMeters
            meters = ProcessMeters()
        except Exception:
            meters = None

    voice = Syllables()
    state = {"i": 0, "entered": time.perf_counter()}
    results: list[tuple[str, float, float | None, float | None]] = []

    def enter(i: int) -> None:
        state["i"] = i
        state["entered"] = time.perf_counter()
        state["mark"] = None
        win.setProperty("status", STATUSES[i])

    def finish(code: int = 0) -> None:
        sys.stdout.flush()
        win.close()
        sip.delete(engine)  # deleting the window is what writes the pipeline cache
        os._exit(code)

    def tick() -> None:
        now = time.perf_counter()
        lvl = voice.level(now)
        win.setProperty("level", lvl)
        i, age = state["i"], now - state["entered"]
        name = STATUSES[i]
        if args.shots:
            # Voiced statuses wait (up to 1.5 s more) for a syllable, so the shot shows it.
            mid_syllable = voice.on and lvl >= 0.6 and now - voice.started > 0.05
            if age >= 2.0 and (name not in VOICED or mid_syllable or age >= 3.5):
                out = Path(args.shots)
                out.mkdir(parents=True, exist_ok=True)
                path = out / f"{i:02d}_{name}.png"
                win.grabWindow().save(str(path))
                print(f"{name:12s} {path}", flush=True)
                if i + 1 == len(STATUSES):
                    finish()
                enter(i + 1)
        elif args.bench:
            if age >= 0.5 and state["mark"] is None:
                state["mark"] = (now, frames[0])
                if meters is not None:
                    meters.gpu_percent()
                    meters.cpu_percent()
            if age >= 3.0:
                t0, f0 = state["mark"]
                fps = (frames[0] - f0) / (now - t0)
                gpu = meters.gpu_percent() if meters is not None else None
                cpu = meters.cpu_percent() if meters is not None else None
                results.append((name, fps, gpu, cpu))
                if i + 1 == len(STATUSES):
                    print(f"mode {args.mode}{' no-glow' if args.no_glow else ''}, vsync off")
                    print(f"first frame {first[0] * 1000:.0f} ms after process start "
                          f"({'warm' if warm else 'cold'} pipeline cache)")
                    print("status           fps   GPU busy  GPU ms/frame  CPU (one core)")
                    for n, fps, gpu, cpu in results:
                        busy = "   n/a" if gpu is None else f"{gpu:5.1f}%"
                        ms = "   n/a" if gpu is None else f"{gpu / 100 * 1000 / max(fps, 1):6.3f}"
                        cpu_s = "  n/a" if cpu is None else f"{cpu:5.1f}%"
                        print(f"{n:12s} {fps:7.0f}   {busy}     {ms}       {cpu_s}")
                    finish()
                enter(i + 1)
        elif age >= 3.0:
            enter((i + 1) % len(STATUSES))

    timer = QTimer()
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    timer.timeout.connect(tick)
    timer.start(33)
    enter(0)

    if args.shots or args.bench:
        win.setOpacity(0.0)  # on screen but invisible: an off-screen window never renders
        QTimer.singleShot(int(len(STATUSES) * 3.6 * 1000) + 10000, lambda: finish(3))
    win.show()
    code = app.exec()
    sip.delete(engine)
    return code


if __name__ == "__main__":
    sys.exit(main())
