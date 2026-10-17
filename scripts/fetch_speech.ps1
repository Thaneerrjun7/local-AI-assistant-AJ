# Downloads the speech models into models\speech (git-ignored).
# Re-running skips models that are already extracted.
$ErrorActionPreference = "Stop"
$speech = New-Item -ItemType Directory -Force (Join-Path $PSScriptRoot "..\models\speech")
$base = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
$archives = @(
  "$base/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2",
  "$base/tts-models/kokoro-multi-lang-v1_0.tar.bz2"
)
# The int8 Kokoro pack was dropped: ~3x slower than fp32 on this CPU, and the voice runs on the GPU.

& curl.exe -L --fail --retry 3 -o (Join-Path $speech "silero_vad.onnx") "$base/asr-models/silero_vad.onnx"
if ($LASTEXITCODE -ne 0) { throw "download failed: silero_vad.onnx" }

foreach ($url in $archives) {
  $name = Split-Path $url -Leaf
  $folder = Join-Path $speech ($name -replace '\.tar\.bz2$', '')
  if (Test-Path $folder) { Write-Host "skip $name (already extracted)"; continue }
  $archive = Join-Path $speech $name
  Write-Host "==> $name"
  & curl.exe -L --fail --retry 3 -C - -o $archive $url
  if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 33) { throw "download failed: $name (curl $LASTEXITCODE)" }
  & tar.exe -xjf $archive -C $speech
  if ($LASTEXITCODE -ne 0) { throw "extract failed: $name" }
  Remove-Item $archive
}
Get-ChildItem $speech | Select-Object Name
Write-Host "DONE"
