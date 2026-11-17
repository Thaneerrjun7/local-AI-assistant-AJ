# Downloads the Phase 1 runtime and models into models\ (git-ignored).
# Re-running resumes partial files and skips finished ones.
# To free the space later, delete the whole models\ folder.
#
# -Vulkan: only the Vulkan build of the same llama.cpp release, into models\llama.cpp-vulkan\,
# so a model can run on the Radeon 780M (docs/HARDWARE_AWARE_INFERENCE_PLAN.md, Phase 0). The
# CUDA build in models\llama.cpp\ is left as it is.
param([switch]$Vulkan)
$ErrorActionPreference = "Stop"
$models = Join-Path $PSScriptRoot "..\models" | Resolve-Path -ErrorAction SilentlyContinue
if (-not $models) { $models = New-Item -ItemType Directory -Force (Join-Path $PSScriptRoot "..\models") }
$llm = New-Item -ItemType Directory -Force (Join-Path $models "llm")
$cpp = New-Item -ItemType Directory -Force (Join-Path $models "llama.cpp")

$build = "b11379"
if ($Vulkan) {
  $vk = New-Item -ItemType Directory -Force (Join-Path $models "llama.cpp-vulkan")
  $name = "llama-$build-bin-win-vulkan-x64.zip"
  $zip = Join-Path $vk $name
  Write-Host "==> $name"
  & curl.exe -L --fail --retry 3 -C - -o $zip "https://github.com/ggml-org/llama.cpp/releases/download/$build/$name"
  if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 33) { throw "download failed: $name (curl $LASTEXITCODE)" }
  Expand-Archive -Force $zip -DestinationPath $vk
  Remove-Item $zip
  & (Join-Path $vk "llama-server.exe") --version
  & (Join-Path $vk "llama-server.exe") --list-devices
  Write-Host "DONE"
  return
}
$files = @(
  @{ url = "https://github.com/ggml-org/llama.cpp/releases/download/$build/llama-$build-bin-win-cuda-13.4-x64.zip"; dir = $cpp },
  @{ url = "https://github.com/ggml-org/llama.cpp/releases/download/$build/cudart-llama-bin-win-cuda-13.4-x64.zip"; dir = $cpp },
  @{ url = "https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF/resolve/main/Qwen3-4B-Instruct-2507-Q4_K_M.gguf"; dir = $llm },
  # The thinker for hard questions (12/12 on the hard set vs Qwen's 10/12), loaded on demand.
  @{ url = "https://huggingface.co/openbmb/MiniCPM5-2B-GGUF/resolve/main/MiniCPM5-2B-Q4_K_M.gguf"; dir = $llm }
)
# Phase 1 challengers, removed after Qwen3-4B won (2026-10-04). Re-add to re-run the benchmark:
#   unsloth/Qwen3-1.7B-GGUF/Qwen3-1.7B-Q4_K_M.gguf
#   NousResearch/Hermes-3-Llama-3.2-3B-GGUF/Hermes-3-Llama-3.2-3B.Q4_K_M.gguf
#   google/gemma-4-E4B-it-qat-q4_0-gguf/gemma-4-E4B_q4_0-it.gguf

foreach ($f in $files) {
  $name = Split-Path $f.url -Leaf
  $dest = Join-Path $f.dir $name
  Write-Host "==> $name"
  & curl.exe -L --fail --retry 3 -C - -o $dest $f.url
  if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 33) { throw "download failed: $name (curl $LASTEXITCODE)" }
}

foreach ($zip in Get-ChildItem $cpp -Filter *.zip) {
  Write-Host "==> unzip $($zip.Name)"
  Expand-Archive -Force $zip.FullName -DestinationPath $cpp
  Remove-Item $zip.FullName  # unpacked; the zip is dead weight
}
& (Join-Path $cpp "llama-server.exe") --version
Write-Host "DONE"
