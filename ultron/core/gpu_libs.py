"""Make NVIDIA's pip-installed CUDA 12 DLLs (nvidia-*-cu12 packages) findable on Windows.

Must run before sherpa_onnx is imported. Both add_dll_directory and PATH are set:
onnxruntime loads its CUDA provider and cuDNN with LoadLibrary, which doesn't
always honour add_dll_directory alone.
"""
import os
import sys
from pathlib import Path

_added: list[str] = []


def add_cuda_dll_dirs() -> list[str]:
    if _added:
        return _added
    root = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    for bin_dir in sorted(root.glob("*/bin")) if root.exists() else []:
        os.add_dll_directory(str(bin_dir))
        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
        _added.append(str(bin_dir))
    return _added
