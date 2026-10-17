# Runs the LLM benchmark on the chosen model (GPU, then CPU for reference).
# Results land in data\bench; then run scripts\bench_report.py.
# Phase 1 also ran gemma4-e4b, hermes3-3b and qwen3-1.7b-cpu; their results stay in data\bench
# and their model files were removed after Qwen3-4B won (see scripts\fetch_models.ps1).
$ErrorActionPreference = "Stop"
$root = Join-Path $PSScriptRoot ".." | Resolve-Path
$py = Join-Path $root ".venv\Scripts\python.exe"
$llm = Join-Path $root "models\llm"
Set-Location $root

$runs = @(
  @{ name = "qwen3-4b";     model = "Qwen3-4B-Instruct-2507-Q4_K_M.gguf"; ngl = 99; runs = 5 },
  @{ name = "qwen3-4b-cpu"; model = "Qwen3-4B-Instruct-2507-Q4_K_M.gguf"; ngl = 0;  runs = 3 }
)
foreach ($r in $runs) {
  Write-Host "===== $($r.name) ====="
  & $py scripts\bench_llm.py --name $r.name --model (Join-Path $llm $r.model) --ngl $r.ngl --runs $r.runs
  if ($LASTEXITCODE -ne 0) { Write-Host "!! $($r.name) failed (exit $LASTEXITCODE), continuing" }
}
& $py scripts\bench_report.py
Write-Host "DONE"
