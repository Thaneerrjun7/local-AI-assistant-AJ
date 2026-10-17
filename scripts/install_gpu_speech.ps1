# Swaps the CPU build of sherpa-onnx for the CUDA 12 build and installs NVIDIA's CUDA 12
# runtime libraries (cuDNN 9, cuBLAS, cuFFT, cuRAND, NVRTC, cudart) into the venv, so Kokoro
# and Parakeet can run on the RTX 4050. ~1.7 GB download; everything stays inside .venv.
# Undo: pip uninstall the nvidia-* packages and reinstall sherpa-onnx from requirements.txt.
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$py = Join-Path $root ".venv\Scripts\python.exe"
$wheel = "https://huggingface.co/csukuangfj2/sherpa-onnx-wheels/resolve/main/cuda/1.13.8/sherpa_onnx-1.13.8+cuda12.cudnn9-cp313-cp313-win_amd64.whl"

Write-Host "==> removing CPU sherpa-onnx"
& $py -m pip uninstall -y sherpa-onnx sherpa-onnx-core
Write-Host "==> installing CUDA sherpa-onnx"
& $py -m pip install --no-deps $wheel
if ($LASTEXITCODE -ne 0) { throw "sherpa-onnx CUDA wheel failed" }
Write-Host "==> installing NVIDIA CUDA 12 libraries"
& $py -m pip install nvidia-cuda-runtime-cu12 nvidia-cublas-cu12 nvidia-cudnn-cu12 nvidia-cufft-cu12 nvidia-curand-cu12 nvidia-cuda-nvrtc-cu12
if ($LASTEXITCODE -ne 0) { throw "NVIDIA libraries failed" }
& $py -m pip freeze
Write-Host "DONE"
