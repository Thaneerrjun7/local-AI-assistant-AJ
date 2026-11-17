"""Compile the window's GLSL shaders (aj/ui/shaders/*.frag, *.vert) to Qt's .qsb format.

    .venv\\Scripts\\python.exe scripts\\build_shaders.py

The PyQt6 wheel has no shader compiler, so this uses qsb.exe from a tools-only venv
(.venv-shaders, PySide6 pinned to the same Qt version as PyQt6-Qt6; qsb.exe itself comes in
PySide6-Addons). The app never imports PySide6: it only reads the .qsb files, which are committed.

    "C:\\...\\Python313\\python.exe" -m venv .venv-shaders
    .venv-shaders\\Scripts\\python.exe -m pip install PySide6-Essentials==6.11.2 PySide6-Addons==6.11.2

A line `#include "name.glsl"` is replaced by that file's text before compiling (qsb has no
include path of its own), so the background, the glass and the orb share one copy of the code
that draws the background and the liquid.

Each pack also gets Direct3D bytecode (DXBC), compiled here with the d3dcompiler_47.dll that
ships with PyQt6. Without it Qt compiles the HLSL when the window first draws, which measured
857 ms for the glass shader alone (2026-10-23): this keeps that off the startup path. (qsb's own
-c option would need fxc.exe from the Windows SDK, which isn't installed.)
"""
import ctypes
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHADERS = ROOT / "aj" / "ui" / "shaders"
QSB = ROOT / ".venv-shaders" / "Lib" / "site-packages" / "PySide6" / "qsb.exe"
D3DCOMPILER = ROOT / ".venv" / "Lib" / "site-packages" / "PyQt6" / "Qt6" / "bin" / "d3dcompiler_47.dll"
TARGETS = ["--glsl", "330", "--hlsl", "50"]  # Direct3D 11 is what Qt Quick uses here; OpenGL as a spare
_PROFILES = {".frag": b"ps_5_0", ".vert": b"vs_5_0"}
_OPTIMIZE = 1 << 15  # D3DCOMPILE_OPTIMIZATION_LEVEL3
_INCLUDE = re.compile(r'^\s*#include\s+"([^"]+)"\s*$', re.MULTILINE)


def expand(path: Path, seen: tuple[Path, ...] = ()) -> str:
    if path in seen:
        raise ValueError(f"{path.name} includes itself")
    text = path.read_text(encoding="utf-8")
    return _INCLUDE.sub(lambda m: expand(path.parent / m.group(1), seen + (path,)), text)


def _blob(pointer: ctypes.c_void_p) -> bytes:
    """The bytes of an ID3DBlob (GetBufferPointer and GetBufferSize are vtable slots 3 and 4)."""
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    data = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(vtable[3])(pointer)
    size = ctypes.WINFUNCTYPE(ctypes.c_size_t, ctypes.c_void_p)(vtable[4])(pointer)
    return ctypes.string_at(data, size)


def compile_hlsl(hlsl: bytes, profile: bytes, name: str) -> bytes:
    compiler = ctypes.WinDLL(str(D3DCOMPILER))
    code, errors = ctypes.c_void_p(), ctypes.c_void_p()
    hr = compiler.D3DCompile(hlsl, len(hlsl), name.encode(), None, None, b"main", profile, _OPTIMIZE, 0,
                             ctypes.byref(code), ctypes.byref(errors))
    if hr != 0:
        detail = _blob(errors).decode(errors="replace") if errors.value else ""
        sys.exit(f"{name}: the Direct3D compile failed\n{detail}")
    return _blob(code)


def _qsb(*args: str, expanded: Path | None = None) -> None:
    result = subprocess.run([str(QSB), *args], capture_output=True, text=True)
    if result.returncode != 0:
        where = ""
        if expanded is not None:  # its line numbers are the expanded file's: keep a copy to read
            kept = Path(tempfile.gettempdir()) / "aj_shaders" / expanded.name
            kept.parent.mkdir(exist_ok=True)
            kept.write_text(expanded.read_text(encoding="utf-8"), encoding="utf-8")
            where = f"(line numbers are in {kept}, the source with its includes filled in)\n"
        sys.exit(f"qsb {' '.join(args)}:\n{where}{result.stdout}{result.stderr}")


def build(source: Path) -> None:
    out = source.with_name(source.name + ".qsb")
    with tempfile.TemporaryDirectory() as tmp:
        expanded = Path(tmp) / source.name
        expanded.write_text(expand(source), encoding="utf-8")
        _qsb(*TARGETS, "-o", str(out), str(expanded), expanded=expanded)
        hlsl = Path(tmp) / "shader.hlsl"
        _qsb("-x", "hlsl,50", "-o", str(hlsl), str(out))
        dxbc = Path(tmp) / "shader.dxbc"
        dxbc.write_bytes(compile_hlsl(hlsl.read_bytes(), _PROFILES[source.suffix], source.name))
        _qsb("-r", f"dxbc,50,{dxbc}", str(out))  # Qt's Direct3D 11 backend takes DXBC over HLSL
    print(f"{source.name} -> {out.name} (with Direct3D bytecode)")


def main() -> int:
    for tool in (QSB, D3DCOMPILER):
        if not tool.exists():
            sys.exit(f"Missing {tool}; see this script's docstring.")
    for source in sorted([*SHADERS.glob("*.frag"), *SHADERS.glob("*.vert")]):
        build(source)
    return 0


if __name__ == "__main__":
    sys.exit(main())
